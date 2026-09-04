"""In-process double for the cdesktop client seam, built for fault injection.

``FakeCdesktop`` implements exactly the surface `sdk.py` and `durable.py` call
on ``CdesktopClient`` (see ``src/sightmesh/cdesktop.py``), plus a small
fault-injection API the simulator scenarios use to stand in for the failure
modes a real executor can produce: a process crash mid-call
(:meth:`kill_after`), a duplicated network delivery of the same request
(:meth:`duplicate_call`), slow responses (:meth:`latency`), and a typed rate
limit (:meth:`rate_limit_after`).

Every call is appended to ``call_log`` so scenarios can assert on *what* the
kernel asked the executor to do, not just on the return value - this is what
lets S11 prove ``show()`` performs zero fleet scans.

The native launch and mailbox behaviors mirror the contract this fake stands
in for: ``managed_launch`` is a create-or-return keyed on ``(task_id,
epoch)`` (docs/kernel-contract.md, "Executor seam"), and ``send`` collapses
repeated calls that share a ``dedupe_key`` (docs/kernel-contract.md,
"Mailbox"). Getting these two invariants right in the fake is what makes a
red scenario mean "SightMesh has a real bug" rather than "the fake forgot to
be idempotent".
"""

from __future__ import annotations

from sightmesh.fence import assert_external_io_allowed

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sightmesh.cdesktop import CdesktopError, CdesktopRejectedError
from sightmesh.runtime_lock import RUNTIME_LOCK


class SimulatedCrash(RuntimeError):
    """Stands in for a real process crash at a named step.

    A real crash never returns control to the caller; raising here is the
    closest a same-process test double can get. Scenarios that need to
    observe "the write committed but the caller never found out" arrange
    that invariant at the call site (see ``test_scenarios.py`` S1/S2), not by
    asking this exception to carry extra meaning.
    """


@dataclass
class _FaultPlan:
    kill_steps: set[str] = field(default_factory=set)
    duplicate_steps: set[str] = field(default_factory=set)
    latency_steps: dict[str, float] = field(default_factory=dict)
    #: step -> (status, retry_after seconds or None), consumed once.
    rejection_steps: dict[str, tuple[int, float | None]] = field(default_factory=dict)
    #: Untyped launch failures, raised one per call in order.
    launch_errors: list[Exception] = field(default_factory=list)


class FakeCdesktop:
    """A minimal, thread-safe, idempotent stand-in for ``CdesktopClient``."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        self.call_log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.sent: list[tuple[Any, ...]] = []
        self.stopped: list[str] = []
        self.repo_rows: list[dict[str, Any]] | None = None
        self.processes: dict[str, list[dict[str, Any]]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, list[dict[str, Any]]] = {}
        self.queued_mail: dict[str, int] = {}

        self._effects: dict[tuple[str, int], dict[str, Any]] = {}
        self._effect_errors: list[Exception] = []
        self._effects_lock = threading.Lock()
        self._sent_dedupe: dict[str, Any] = {}
        self._sent_lock = threading.Lock()
        self._faults = _FaultPlan()
        self._faults_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Fault injection API (spec: "Simulator" section of docs/kernel-spec.md)
    # ------------------------------------------------------------------
    def kill_after(self, step: str) -> None:
        """Raise :class:`SimulatedCrash` the next time ``step`` executes."""
        with self._faults_lock:
            self._faults.kill_steps.add(step)

    def duplicate_call(self, step: str) -> None:
        """Replay ``step`` a second time in-process the next time it runs.

        Models a network-level duplicate delivery of the same request (e.g.
        a client retry racing an already-successful response) landing at the
        executor twice.
        """
        with self._faults_lock:
            self._faults.duplicate_steps.add(step)

    def latency(self, step: str, seconds: float) -> None:
        """Sleep ``seconds`` every time ``step`` executes (sticky, not one-shot)."""
        with self._faults_lock:
            self._faults.latency_steps[step] = seconds

    def rate_limit_after(self, step: str, retry_after: float | None = None) -> None:
        """Raise a typed HTTP 429 the next time ``step`` executes."""
        self.reject_after(step, 429, retry_after)

    def reject_after(
        self, step: str, status: int, retry_after: float | None = None
    ) -> None:
        """Raise a typed rejection with this status the next time ``step`` runs.

        The status is the whole point: routing reads the typed discriminator,
        so a scenario injecting a 401 or a 503 must produce exactly what the
        real client produces for one, down to the optional ``Retry-After``.
        """
        with self._faults_lock:
            self._faults.rejection_steps[step] = (int(status), retry_after)

    def fail_launch(self, error: Exception) -> None:
        """Raise ``error`` from the next ``managed_launch``, verbatim.

        Distinct from :meth:`reject_after`, which produces a *typed* rejection.
        This one stands in for an executor call that failed in a way carrying
        no provider meaning at all - a local 5xx, a timeout, an unreachable
        service - so a scenario can prove that such a failure never becomes a
        provider outcome.
        """
        with self._faults_lock:
            self._faults.launch_errors.append(error)

    def fail_process(
        self,
        session_id: str,
        *,
        outcome_class: str | None = None,
        retry_after: float | None = None,
        status: str = "failed",
        exit_code: int | None = 1,
    ) -> str:
        """Record that this session's coding agent process stopped.

        Injects the *typed* process record the real seam returns - status, exit
        code, and cdesktop's normalized outcome class - and nothing else. A
        scenario cannot make this fake carry transcript text into the routing
        decision, because the observer does not read any, which is exactly the
        property the routing lane exists to hold.
        """
        process_id = f"process-{session_id}-{len(self.processes.get(session_id, []))}"
        record: dict[str, Any] = {
            "id": process_id,
            "session_id": session_id,
            "run_reason": "codingagent",
            "status": status,
            "exit_code": exit_code,
        }
        if outcome_class is not None:
            record["outcome"] = {
                "class": outcome_class,
                "safe_message": "provider refused the request",
                **({} if retry_after is None else {"retry_after_seconds": retry_after}),
            }
        self.processes.setdefault(session_id, []).append(record)
        return process_id

    def _consume_duplicate(self, step: str) -> bool:
        with self._faults_lock:
            if step in self._faults.duplicate_steps:
                self._faults.duplicate_steps.discard(step)
                return True
            return False

    def _hook(self, step: str) -> None:
        with self._faults_lock:
            delay = self._faults.latency_steps.get(step)
            rejection = self._faults.rejection_steps.pop(step, None)
            error = (
                self._faults.launch_errors.pop(0)
                if step == "launch" and self._faults.launch_errors
                else None
            )
            killed = step in self._faults.kill_steps
            if killed:
                self._faults.kill_steps.discard(step)
        if delay:
            time.sleep(delay)
        if error is not None:
            raise error
        if rejection is not None:
            status, retry_after = rejection
            raise CdesktopRejectedError(
                f"managed launch step {step!r}: HTTP {status}",
                status=status,
                retry_at=None if retry_after is None else time.time() + retry_after,
            )
        if killed:
            raise SimulatedCrash(f"simulated crash at step {step!r}")

    def _log(self, _call_name: str, *args: Any, **kwargs: Any) -> None:
        self.call_log.append((_call_name, args, kwargs))

    # ------------------------------------------------------------------
    # Contract surface used by sdk.py
    # ------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("info")
        return {
            # A version the durable reconciler accepts, so a scenario can run
            # the *whole* bridge tick - kernel pass and session pass - rather
            # than only the half the detector lives in. The no-kill negatives
            # are worthless if the pass that used to do the killing is gated
            # off behind a missing version string.
            "version": f"cdesktop/{RUNTIME_LOCK.cdesktop.compatibility.durable_recovery}",
            "service_capabilities": {"managed_task_launch": 1},
        }

    def repos(self) -> list[dict[str, Any]]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("repos")
        return self.repo_rows or [
            {"id": "repo-1", "name": "project", "path": str(self.repo_path)}
        ]

    def providers(self) -> list[dict[str, Any]]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("providers")
        return [
            {
                "id": "default-provider",
                "name": "Default",
                "kind": "Default",
                "enabled": True,
            }
        ]

    def register_repo(self, path: Path, **_kwargs: Any) -> dict[str, Any]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("register_repo", path)
        assert Path(path).resolve() == self.repo_path.resolve()
        return self.repos()[0]

    def workspace(self, workspace_id: str) -> dict[str, Any]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("workspace", workspace_id)
        container = self.repo_path.parent / "worktrees" / workspace_id
        (container / "project").mkdir(parents=True, exist_ok=True)
        return {"id": workspace_id, "container_ref": str(container)}

    def workspace_launch_request(self, **kwargs: Any) -> dict[str, Any]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("workspace_launch_request", **kwargs)
        return {"workspace": kwargs}

    @staticmethod
    def session_launch_request(**kwargs: Any) -> dict[str, Any]:
        return {"session": kwargs}

    def managed_launch(
        self, task_id: str, epoch: int, launch: dict[str, Any]
    ) -> dict[str, Any]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("managed_launch", task_id, epoch, launch)
        result = self._do_managed_launch(task_id, epoch, launch)
        if self._consume_duplicate("launch"):
            # The same (task, epoch) PUT lands a second time; a
            # correctly-idempotent executor (and kernel) resolve to the
            # identical effect rather than a second native session.
            result = self._do_managed_launch(task_id, epoch, launch)
        return result

    def _do_managed_launch(
        self, task_id: str, epoch: int, _launch: dict[str, Any]
    ) -> dict[str, Any]:
        self._hook("launch")
        key = (str(task_id), int(epoch))
        with self._effects_lock:
            effect = self._effects.get(key)
            if effect is None:
                workspace_id = f"workspace-{task_id}"
                session_id = f"session-{task_id}-{epoch}"
                effect = {
                    "state": "active",
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                }
                self._effects[key] = effect
        # A real crash can land here too: the native launch already
        # happened (the row above is set) but the caller never persisted
        # the activation. That is exactly the gap S3 probes.
        self._hook("activate")
        return dict(effect)

    def fail_managed_effect(self, *errors: Exception) -> None:
        """Queue transient ``managed_effect`` failures, one per subsequent call.

        Stands in for an executor that cannot answer the adopt-or-lose probe -
        a 5xx, a ``URLError``, a timeout - as opposed to a definitive 404. Each
        queued error is raised once, in order, so a scenario can make one tick
        unknowable and let the next tick see the live session (G3 / S27).
        """
        with self._effects_lock:
            self._effect_errors.extend(errors)

    def managed_effect(self, task_id: str, epoch: int) -> dict[str, Any]:
        """Look up the native effect behind a task epoch, or 404 if absent.

        Mirrors ``CdesktopClient.managed_effect`` (a ``GET`` that raises on a
        not-found response) so F3's adopt-or-lose expiry can distinguish a live
        native session from one that is genuinely gone.
        """
        self._log("managed_effect", task_id, epoch)
        key = (str(task_id), int(epoch))
        with self._effects_lock:
            if self._effect_errors:
                # An executor that cannot confirm or deny the session this tick;
                # not a 404, so the caller must not treat it as absence.
                raise self._effect_errors.pop(0)
            effect = self._effects.get(key)
        if effect is None:
            raise CdesktopError(
                f"GET managed effect {task_id}/{epoch} failed: HTTP 404: not found"
            )
        return dict(effect)

    def create_native_session(
        self, task_id: str, epoch: int, *, workspace_id: str, session_id: str
    ) -> None:
        """Seed a native session with no kernel activation behind it.

        Stands in for the ordinary "session created but ``mark_launched`` never
        ran" window a 15s launch timeout opens (F3 / S20).
        """
        with self._effects_lock:
            self._effects[(str(task_id), int(epoch))] = {
                "state": "active",
                "workspace_id": workspace_id,
                "session_id": session_id,
            }

    def send(
        self,
        session_id: str,
        prompt: str,
        sender_session: str | None = None,
        *,
        dedupe_key: str | None = None,
        intent: str = "continue",
    ) -> Any:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log(
            "send",
            session_id,
            prompt,
            sender_session,
            dedupe_key=dedupe_key,
            intent=intent,
        )
        self._hook("notify")
        with self._sent_lock:
            if dedupe_key is not None and dedupe_key in self._sent_dedupe:
                return self._sent_dedupe[dedupe_key]
            row = (session_id, prompt, sender_session, dedupe_key, intent)
            self.sent.append(row)
            result = {"queued": True}
            if dedupe_key is not None:
                self._sent_dedupe[dedupe_key] = result
            return result

    def stop_workspace(self, workspace_id: str) -> Any:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("stop_workspace", workspace_id)
        self.stopped.append(workspace_id)

    def session_commands(self, session_id: str) -> list[dict[str, Any]]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("session_commands", session_id)
        return self.commands.get(session_id, [])

    def execution_processes(self, session_id: str) -> list[dict[str, Any]]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("execution_processes", session_id)
        return self.processes.get(session_id, [])

    def normalized_snapshot(self, process_id: str) -> dict[str, Any]:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("normalized_snapshot", process_id)
        return self.snapshots[process_id]

    def queue_status(self, session_id: str) -> dict[str, Any]:
        self._log("queue_status", session_id)
        return {"pending": self.queued_mail.get(session_id, 0)}

    # ------------------------------------------------------------------
    # Progress-evidence controls (spec: docs/liveness-spec.md, "Progress
    # evidence"). Each one stands for a signal a real executor emits, so a
    # liveness scenario configures the *world* and lets the real detector
    # draw its own conclusion.
    # ------------------------------------------------------------------
    def run_process(
        self,
        session_id: str,
        *,
        process_id: str | None = None,
        last_activity: float,
        run_reason: str = "codingagent",
        output_bytes: int | None = None,
        stream_alive: bool = True,
        **snapshot: Any,
    ) -> str:
        """Give a session one running execution with a chosen last-activity time.

        ``last_activity`` is an epoch timestamp, not an age, so a scenario can
        place a task exactly N seconds into silence without sleeping.

        ``output_bytes`` defaults to *absent*, not zero, because that is the
        shape every real cdesktop process row has: the field does not exist.
        A fake that invented it gave the detector a progress source production
        does not have, and hid the fact that the timestamp path - the only one
        that actually runs out there - was never under test.
        """
        pid = process_id or f"proc-{session_id}-{len(self.processes.get(session_id, []))}"
        row: dict[str, Any] = {
            "id": pid,
            "status": "running",
            "run_reason": run_reason,
            "updated_at": last_activity,
        }
        if output_bytes is not None:
            row["output_bytes"] = output_bytes
        self.processes.setdefault(session_id, []).append(row)
        self.snapshots[pid] = {"entries": [], "stream_alive": stream_alive, **snapshot}
        return pid

    def set_last_activity(self, process_id: str, when: float) -> None:
        """Move one execution's last-activity timestamp; the detector reads it."""
        for rows in self.processes.values():
            for row in rows:
                if str(row["id"]) == process_id:
                    row["updated_at"] = when

    def feed_output(self, process_id: str, extra_bytes: int) -> None:
        """Grow a running command's output. Bytes are progress, however slow."""
        for rows in self.processes.values():
            for row in rows:
                if str(row["id"]) == process_id:
                    row["output_bytes"] = int(row.get("output_bytes") or 0) + extra_bytes

    def mark_snapshot(self, process_id: str, **markers: Any) -> None:
        """Set typed executor markers (``turn_ended``, ``parked``, ...).

        These are the Phase 4 seam's signals. A scenario that sets them is
        asserting the *post*-degraded-mode behavior; one that leaves them off
        exercises what the current 0.2.x client can actually see.
        """
        self.snapshots.setdefault(process_id, {"entries": [], "stream_alive": True})
        self.snapshots[process_id].update(markers)

    def kill_process(self, process_id: str, *, exit_reason: str = "restart") -> None:
        """Kill an execution with a typed attribution, as a restart marker would.

        Typed on purpose: without an ``exit_reason`` the detector must refuse
        to attribute the loss at all, and a scenario that wants a `lost`
        outcome has to say which kind of loss actually happened.
        """
        for rows in self.processes.values():
            for row in rows:
                if str(row["id"]) == process_id:
                    row["status"] = "killed"
                    row["exit_reason"] = exit_reason

    def queue_mail(self, session_id: str, pending: int) -> None:
        """Set a session's queued-mail depth; queued work is not silence."""
        self.queued_mail[session_id] = pending

    def dispatch_queued(self, session_id: str) -> Any:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("dispatch_queued", session_id)
        return {"dispatched": 0}

    def stop_execution(self, execution_process_id: str, **kwargs: Any) -> Any:
        assert_external_io_allowed()  # the real client does HTTP here
        self._log("stop_execution", execution_process_id, **kwargs)
        return {"stopped": True}

    def probe_connectivity(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------
    def calls(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        """All logged calls to ``name``, in order, for call-log assertions."""
        return [(args, kwargs) for logged, args, kwargs in self.call_log if logged == name]

    def distinct_effects(self) -> set[tuple[str, str]]:
        """Every unique (workspace_id, session_id) pair ever handed out."""
        return {
            (str(effect["workspace_id"]), str(effect["session_id"]))
            for effect in self._effects.values()
        }
