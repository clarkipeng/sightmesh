"""The single in-service gap closer for durable cdesktop commands.

SightMesh does not persist execution state here.  cdesktop owns the command
rows; this module only translates observations of native processes into the
native command lifecycle and queues parent notifications through the same
follow-up path.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import liveness as liveness_detector
from . import wakes
from .cdesktop import CdesktopClient, CdesktopError
from .effects import EffectJournal
from .external_runs import ExternalRunReconciler, ExternalRunStore
from .escalation import EscalationStore, escalate
from .liveness import DetectionPolicy, DetectionPolicyError
from .runtime_lock import RUNTIME_LOCK
from .succession import COMMAND_TERMINAL_STATES, OwnershipStore, resolve_live_successor
from .task_store import (
    LIVE_STATES,
    STALL_LIVENESS_STATES,
    TaskRecord,
    TaskStore,
    TaskStoreError,
)

LOGGER = logging.getLogger("sightmesh.durable")

LIFECYCLE_NOTIFICATION_KEY_PREFIXES = (
    "child-command:",
    "child-terminal:",
    "signal-policy:",
)
#: Task states the detector reads evidence for. Only ``active``: ``blocked``
#: has already explained itself; ``reserved`` has no session to read, so an
#: aged reservation goes through :meth:`_scan_reserved` instead; and
#: ``replacing`` still points at the *predecessor's* dying session, so reading
#: it would classify a successor from its predecessor's corpse - and ``lost``
#: is a legal terminal from ``replacing``, which would kill the replacement
#: mid-handoff.
DETECTABLE_STATES = ("active",)
#: How many tasks one detector pass may classify. The pass is synchronous
#: inside the two-second bridge tick and costs up to two executor round-trips
#: per task, so an unbounded fleet scan is how the detector starves wake
#: delivery and reservation expiry behind it. A durable cursor carries the
#: scan forward, so a large fleet is covered across consecutive ticks instead
#: of one tick trying - and failing - to cover all of it.
LIVENESS_TASKS_PER_PASS = 25
#: Wall-clock ceiling on one pass, for when the executor is slow rather than
#: the fleet large. Whatever is left resumes from the cursor next tick.
LIVENESS_PASS_BUDGET_SECONDS = 1.0


def supports_durable_recovery(version: object) -> bool:
    """Return whether cdesktop exposes the process-scoped recovery API."""
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", str(version or ""))
    return bool(
        match
        and tuple(int(part) for part in match.groups())
        >= RUNTIME_LOCK.cdesktop.compatibility.durable_recovery_tuple
    )


@dataclass(frozen=True)
class DurableCommand:
    id: str
    session_id: str
    body: str
    state: str
    dedupe_key: str | None = None
    execution_process_id: str | None = None

    def delivery_state(self, process: dict[str, Any] | None) -> str:
        """Project cdesktop facts onto the delivery lifecycle without storing it."""
        if self.state == "pending":
            return "queued"
        if self.state == "claimed":
            if process is None:
                return "claimed"
            return "running" if process.get("status") == "running" else "observed"
        if self.state in {"done", "failed"}:
            return "terminal"
        if self.state == "cancelled":
            return "rejected"
        return "observed"


class NativeCommandQueue:
    """Thin adapter around cdesktop's already durable command machinery."""

    def __init__(self, client: CdesktopClient) -> None:
        self.client = client

    def commands(self, session_id: str) -> list[DurableCommand]:
        if not hasattr(self.client, "session_commands"):
            return []
        rows = self.client.session_commands(session_id)
        return [DurableCommand(**_command_fields(row, session_id)) for row in rows]

    def requeue(self, command: DurableCommand) -> None:
        if not command.execution_process_id:
            raise CdesktopError(f"Command {command.id} has no execution to requeue")
        self.client.requeue_execution_commands(
            command.session_id, command.execution_process_id
        )

    def interrupt(self, command: DurableCommand) -> None:
        # cdesktop has no per-command cancel endpoint yet. Prefer one when the
        # runtime (or a test double) offers it; otherwise stop the command's
        # execution, which is replay-safe, and rely on the reconciler never
        # requeueing or dispatching for a quarantined session.
        if hasattr(self.client, "interrupt_command"):
            self.client.interrupt_command(command.id)
            return
        if command.execution_process_id:
            self.client.stop_execution(
                command.execution_process_id,
                dedupe_key=f"quarantine:{command.id}",
            )

    def notify_parent(
        self, parent_session_id: str, child_session_id: str, message: str, key: str
    ) -> None:
        self.client.send(
            parent_session_id,
            message,
            child_session_id,
            dedupe_key=key,
            intent="continue",
        )


def _command_fields(row: dict[str, Any], session_id: str) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row.get("session_id") or session_id),
        "body": str(row.get("body") or row.get("prompt") or ""),
        "state": str(row.get("state") or row.get("status") or "queued"),
        "dedupe_key": row.get("dedupe_key"),
        "execution_process_id": row.get("execution_process_id"),
    }


def _context_pressure(snapshot: dict[str, Any]) -> float | None:
    for wrapped in snapshot.get("entries", []):
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        entry = content.get("entry_type") if isinstance(content, dict) else None
        if isinstance(entry, dict) and entry.get("type") == "token_usage_info":
            used, window = entry.get("total_tokens"), entry.get("model_context_window")
            if (
                isinstance(used, (int, float))
                and isinstance(window, (int, float))
                and window
            ):
                return float(used) / float(window)
    return None


def _idle_seconds(processes: Iterable[dict[str, Any]], now: float) -> float | None:
    timestamps: list[float] = []
    for process in processes:
        for name in ("updated_at", "started_at", "created_at"):
            value = process.get(name)
            if isinstance(value, (int, float)):
                timestamps.append(float(value))
                break
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                timestamps.append(
                    parsed.replace(tzinfo=UTC).timestamp()
                    if parsed.tzinfo is None
                    else parsed.timestamp()
                )
                break
    return now - max(timestamps) if timestamps else None


class DurableExecutionReconciler:
    """Reconcile durable intent and live processes; safe to run repeatedly.

    This class observes and records. It has no path that stops a process on a
    liveness signal, and it must never grow one: every wall-clock reaper in
    the incident record - the 30-minute approval SIGKILL, the idle-timer stop
    that read its own kill back as a worker death - lived exactly here. The
    only stops left in this module are explicit commands: a human interrupt, a
    quarantine cancel, a succession handoff.
    """

    def __init__(
        self,
        client: CdesktopClient,
        queue: NativeCommandQueue | None = None,
        *,
        probe: Callable[[], bool] | None = None,
        ownership: OwnershipStore | None = None,
        signal_store: EscalationStore | None = None,
        task_store: TaskStore | None = None,
        clock: Callable[[], float] | None = None,
        environment: Mapping[str, str] | None = None,
        tasks_per_pass: int = LIVENESS_TASKS_PER_PASS,
        pass_budget_seconds: float = LIVENESS_PASS_BUDGET_SECONDS,
    ) -> None:
        self.client = client
        self.queue = queue or NativeCommandQueue(client)
        self.probe = probe or getattr(client, "probe_connectivity", lambda: True)
        self.ownership = ownership or OwnershipStore()
        self.signal_store = signal_store or EscalationStore()
        self._task_store = task_store
        self.clock = clock or time.time
        self.environment = environment if environment is not None else os.environ
        self.tasks_per_pass = tasks_per_pass
        self.pass_budget_seconds = pass_budget_seconds
        self._requeued: set[str] = set()
        self._cancelled: set[str] = set()
        self._notified: set[str] = set()
        #: Last observed output-byte total per task, recorded only from a
        #: complete, successful read. A failed read reports no bytes at all,
        #: and letting that overwrite the baseline manufactured "growth" on the
        #: next successful read - which reads as progress, closes the episode,
        #: and resets the wake count, latching a wedged child into permanent
        #: apparent health behind a flapping executor.
        self._output_bytes: dict[str, int] = {}
        #: Where the last capped detector pass stopped, so a fleet larger than
        #: one pass is covered across ticks instead of only ever its first N.
        self._liveness_cursor = ""
        self._offline_until = 0.0
        self._backoff = 1.0
        self._durable_supported: bool | None = None

    def _supports_durable_recovery(self) -> bool:
        if self._durable_supported is not None:
            return self._durable_supported
        if not hasattr(self.client, "info"):
            # Protocol test doubles and older embedders predate version discovery.
            self._durable_supported = True
            return True
        info = self.client.info()
        version = info.get("version") if isinstance(info, dict) else None
        self._durable_supported = supports_durable_recovery(version)
        if not self._durable_supported:
            minimum = RUNTIME_LOCK.cdesktop.compatibility.durable_recovery
            LOGGER.warning(
                "Durable recovery is disabled: cdesktop %s or newer is required "
                "(found %s). Normal bridging remains available.",
                minimum,
                version or "unknown version",
            )
        return self._durable_supported

    @property
    def task_store(self) -> TaskStore:
        """Share the escalation store's database; two views cannot disagree."""
        if self._task_store is None:
            self._task_store = TaskStore(self.signal_store.path)
        return self._task_store

    def reconcile_kernel(self) -> dict[str, int]:
        """Close the gaps a crash can leave in the task kernel.

        Scans durable task state rather than commands, so a crash between a
        child's terminal write and its parent's wake, a wake claimed by a dead
        pump, and a reservation whose owner never reached the native call all
        heal on the next tick instead of waiting for a human.

        Every stage is isolated. They are independent repairs sharing a tick,
        not a pipeline: a detector pass that dies on one hostile payload used
        to take wake delivery and reservation expiry down with it, so the
        manager was never told about anything and reserved effects leaked for
        as long as the bad row survived. Nothing here may raise.
        """
        store = self.task_store
        repaired = {
            "wakes_inserted": 0,
            "wakes_delivered": 0,
            "effects_expired": 0,
            "liveness_findings": 0,
        }

        def stage(name: str, run: Callable[[], None]) -> None:
            try:
                run()
            except Exception as exc:  # noqa: BLE001 - one stage never fails the tick
                LOGGER.warning("Cannot %s during kernel reconcile: %s", name, exc)

        def cohort_wakes() -> None:
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                parents = [
                    str(row["parent_task_id"])
                    for row in conn.execute(
                        "SELECT DISTINCT parent_task_id FROM managed_tasks "
                        "WHERE parent_task_id IS NOT NULL"
                    ).fetchall()
                ]
                for parent_task_id in parents:
                    repaired["wakes_inserted"] += len(
                        wakes.record_wakes(conn, parent_task_id)
                    )
                conn.execute("COMMIT")

        def detect() -> None:
            # The stall detector is one more pass over the same durable rows,
            # not a second daemon: liveness findings arm into the same outbox
            # the pump below drains, so there is exactly one delivery path.
            detected = self.detect_liveness()
            repaired["liveness_findings"] = detected["findings"]
            repaired["wakes_inserted"] += detected["wakes_inserted"]

        def deliver() -> None:
            repaired["wakes_delivered"] = wakes.WakeDelivery(
                self.client, store, self.ownership
            ).pump()

        def expire() -> None:
            repaired["effects_expired"] = len(
                EffectJournal(store).expire_reservations(self.client)
            )

        def external_runs() -> None:
            ExternalRunReconciler(
                self.client, ExternalRunStore(self.signal_store.path)
            ).reconcile()

        stage("record cohort wakes", cohort_wakes)
        stage("detect liveness", detect)
        stage("deliver wakes", deliver)
        stage("expire effect reservations", expire)
        stage("reconcile external runs", external_runs)
        return repaired

    def detect_liveness(self) -> dict[str, int]:
        """Classify every live task's progress evidence and arm what it satisfies.

        The whole pass is observe-classify-record-arm. There is no branch in
        it that stops a process, kills a tree, or launches a replacement:
        every outcome is a durable observation plus, at most, a wake for the
        owning manager. That is the contract's central promise ("the kernel
        never kills and never respawns on a liveness signal") held by
        construction rather than by review.
        """
        store = self.task_store
        counts = {"findings": 0, "wakes_inserted": 0}
        selected, watched = self._detection_slice(store)
        self._evict_baselines(watched)
        deadline = time.monotonic() + self.pass_budget_seconds
        for task in selected:
            # Per-task isolation. Evidence comes from an untrusted executor
            # payload and a durable row a human can edit, so any one task can
            # produce any exception; letting one escape used to cost every
            # other task in the fleet its pass.
            try:
                counts_for_task = self._detect_one(store, task)
            except Exception as exc:  # noqa: BLE001 - one task never fails the pass
                LOGGER.warning("Cannot classify liveness for %s: %s", task.key, exc)
                continue
            counts["findings"] += counts_for_task[0]
            counts["wakes_inserted"] += counts_for_task[1]
            if time.monotonic() >= deadline:
                self._liveness_cursor = task.task_id
                break
        self._scan_reserved(store)
        return counts

    def _detect_one(self, store: TaskStore, task: TaskRecord) -> tuple[int, int]:
        """Classify one task and apply the finding; returns (findings, armed)."""
        try:
            policy = self.detection_policy(task)
        except DetectionPolicyError as exc:
            # A malformed policy makes every classification meaningless, so
            # the task gets no finding at all - and a human gets told, because
            # a task nobody can watch is exactly the silent hole this lane is
            # about.
            self._raise_attention(
                task,
                f"BLOCKED: {task.key} has an unusable detection policy and cannot "
                f"be watched: {exc}",
                f"liveness:policy:{task.task_id}:{task.epoch}",
            )
            return (0, 0)
        evidence = liveness_detector.gather_evidence(
            self.client,
            str(task.holder_session_id),
            now=self.clock(),
            checkpoint_at=task.checkpoint_at,
            previous_output_bytes=self._output_bytes.get(task.task_id),
        )
        if evidence.observed and evidence.output_bytes is not None:
            # Only a complete, successful read updates the baseline.
            self._output_bytes[task.task_id] = evidence.output_bytes
        now = self.clock()
        reason = liveness_detector.classify(evidence, now=now, policy=policy)
        if reason in {"idle_unreported", "stalled"} and self._yielding(task):
            # Cause 1 nuance, extended to `stalled` for the same reason: a
            # manager waiting on running children produces no evidence
            # precisely because it is behaving correctly.
            reason = "live"
        finding = liveness_detector.Finding(
            reason=reason,
            evidence=evidence,
            now=now,
            over_budget=liveness_detector.over_budget(evidence, policy.budget),
        )
        findings = 1 if finding.actionable or finding.over_budget else 0
        return (findings, self._apply_finding(store, task, finding, policy))

    def detection_policy(self, task: TaskRecord) -> DetectionPolicy:
        """Resolve one task's detection settings, never weaker than the floor.

        The floor comes from the reconciler's *injected* environment, not from
        ``os.environ``: an embedder that hands SightMesh a configuration and
        then watches the detector ignore it has no way to tell, and the SDK
        side already reads the injected mapping.

        Raises :class:`DetectionPolicyError` rather than returning a guess. A
        stored policy that cannot be read is a per-task fault the caller turns
        into an attention item; it must never raise through the fleet pass.
        """
        spec = task.spec if isinstance(task.spec, dict) else {}
        trusted = liveness_detector.trusted_policy(dict(self.environment))
        stored = spec.get("detection")
        if stored is not None:
            return DetectionPolicy.from_dict(stored)
        # Rows written before the resolved policy moved out of the public spec
        # still carry the three settings inline.
        try:
            budget = liveness_detector.Budget.from_dict(spec.get("budget"))
        except liveness_detector.BudgetError as exc:
            raise DetectionPolicyError(str(exc)) from exc
        return liveness_detector.resolve_policy(
            progress_timeout=spec.get("progress_timeout"),
            approval_timeout=spec.get("approval_timeout"),
            budget=budget,
            trusted=trusted,
        )

    def _detection_slice(self, store: TaskStore) -> tuple[list[TaskRecord], set[str]]:
        """The tasks this pass will classify, resuming from the cursor.

        Returns the capped slice plus the *full* watched set. Baseline
        eviction needs the latter: a fleet bigger than one pass never has a
        "complete" tick, so evicting against the slice alone would either
        leak every terminal task's baseline forever or throw away the
        baselines of tasks that were simply not reached this tick.
        """
        states = ", ".join("?" for _ in DETECTABLE_STATES)
        try:
            with store.connect() as conn:
                rows = conn.execute(
                    f"SELECT task_id FROM managed_tasks WHERE state IN ({states}) "
                    "AND holder_session_id IS NOT NULL ORDER BY task_id",
                    DETECTABLE_STATES,
                ).fetchall()
        except TaskStoreError as exc:
            LOGGER.warning("Cannot scan tasks for liveness: %s", exc)
            return ([], set(self._output_bytes))
        ordered = [str(row["task_id"]) for row in rows]
        start = next(
            (
                index
                for index, task_id in enumerate(ordered)
                if task_id > self._liveness_cursor
            ),
            0,
        )
        rotated = ordered[start:] + ordered[:start]
        complete = len(rotated) <= self.tasks_per_pass
        chosen = rotated if complete else rotated[: self.tasks_per_pass]
        self._liveness_cursor = chosen[-1] if chosen and not complete else ""
        tasks = [store.get_by_id(task_id) for task_id in chosen]
        return ([task for task in tasks if task is not None], set(ordered))

    def _evict_baselines(self, live_task_ids: set[str]) -> None:
        """Forget output baselines for tasks that are no longer being watched.

        A terminal task's baseline is dead weight that outlives the task, and
        a replaced task's baseline belongs to a predecessor whose bytes have
        nothing to do with the successor's.
        """
        for task_id in set(self._output_bytes) - live_task_ids:
            self._output_bytes.pop(task_id, None)

    def _scan_reserved(self, store: TaskStore) -> None:
        """Park an attention item for a reservation that never became a task.

        A reserved task has no session, so there is no evidence to read and
        nothing to classify - which is exactly why it used to be invisible.
        It is also no longer a reason to exempt its manager from detection
        (``YIELD_STATES``), so the pair of changes turns "manager wedged
        behind a child that never launched" from a silent deadlock into two
        signals: the manager's own stall finding, and this.
        """
        now = self.clock()
        try:
            with store.connect() as conn:
                rows = conn.execute(
                    "SELECT task_id FROM managed_tasks WHERE state = 'reserved'"
                ).fetchall()
            for row in rows:
                task = store.get_by_id(str(row["task_id"]))
                if task is None:
                    continue
                try:
                    policy = self.detection_policy(task)
                except DetectionPolicyError:
                    policy = DetectionPolicy()
                if now - task.created_at < policy.progress_timeout:
                    continue
                self._raise_attention(
                    task,
                    f"BLOCKED: {task.key} has been reserved without ever launching "
                    f"for {now - task.created_at:.0f}s.",
                    f"liveness:reserved:{task.task_id}:{task.epoch}",
                    # A reservation has no session yet - that is the whole
                    # complaint - so the task id is the only stable subject
                    # the attention item can be filed against.
                    session_id=task.holder_session_id or task.task_id,
                )
        except TaskStoreError as exc:
            LOGGER.warning("Cannot scan reserved tasks: %s", exc)

    def _yielding(self, task: TaskRecord) -> bool:
        with self.task_store.connect() as conn:
            return wakes.has_live_wait_predicate(conn, task.task_id)

    def _apply_finding(
        self,
        store: TaskStore,
        task: TaskRecord,
        finding: liveness_detector.Finding,
        policy: DetectionPolicy,
    ) -> int:
        """Persist one finding and arm whatever predicate it now satisfies."""
        payload = finding.payload()
        if finding.reason == "lost":
            return self._record_loss(store, task, finding, payload)
        if finding.reason == "unknown" and not finding.over_budget:
            # Nothing was observed either way. Leave the row exactly as found:
            # an unread task must not drift toward a verdict.
            return 0
        try:
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if finding.reason != "unknown":
                    store.record_liveness(
                        task.task_id,
                        finding.reason,
                        evidence=payload if finding.reason != "live" else None,
                        now=finding.now,
                        conn=conn,
                    )
                if finding.over_budget:
                    store.record_over_budget(task.task_id, conn=conn)
                armed = wakes.record_liveness_wakes(
                    conn,
                    task.task_id,
                    now=finding.now,
                    progress_timeout=policy.progress_timeout,
                )
                conn.execute("COMMIT")
        except TaskStoreError as exc:
            LOGGER.warning("Cannot record liveness for %s: %s", task.key, exc)
            return 0
        self._after_finding(store, task, finding, policy)
        return len(armed)

    def _after_finding(
        self,
        store: TaskStore,
        task: TaskRecord,
        finding: liveness_detector.Finding,
        policy: DetectionPolicy,
    ) -> None:
        """Run the human-facing consequences a finding can have."""
        current = store.get_by_id(task.task_id)
        if current is None:
            return
        if finding.reason == "parked":
            self._resolve_parked_approval(store, current, finding, policy)
            return
        if current.liveness not in STALL_LIVENESS_STATES:
            return
        if current.parent_task_id is None:
            # A root task reports to nobody: there is no manager row to wake,
            # so ``record_liveness_wakes`` arms nothing, the wake counter never
            # moves, and the exhaustion path below can never be reached. A
            # wedged root used to be the one incident the kernel could see
            # perfectly and tell absolutely no one about.
            self._raise_attention(
                current,
                f"BLOCKED: {current.key} is {current.liveness} and has no manager "
                f"to wake. {finding.payload()}",
                f"liveness:unmanaged:{current.task_id}:{current.epoch}:"
                f"{current.liveness_episode}",
            )
            return
        # Two conditions, because the wake counter is not a reliable proxy for
        # "the incident persisted". The counter is the ordinary path; the
        # elapsed-phase check is the guarantee, and it holds even when arming
        # the escalation was suppressed - a manager whose session is
        # unreachable is exactly when a human most needs to hear about this.
        persisted = (
            current.liveness_wakes >= 1
            and current.liveness_since is not None
            and finding.now - current.liveness_since >= policy.progress_timeout
        )
        if wakes.episode_is_exhausted(current) or persisted:
            self._raise_attention(
                current,
                f"BLOCKED: {current.key} is {current.liveness} after two manager "
                f"wakes; no progress evidence. {finding.payload()}",
                f"liveness:escalation:{current.task_id}:{current.epoch}:"
                f"{current.liveness_episode}",
            )

    def _resolve_parked_approval(
        self,
        store: TaskStore,
        task: TaskRecord,
        finding: liveness_detector.Finding,
        policy: DetectionPolicy,
    ) -> None:
        """Time out a parked approval into ``blocked(approval)``, never a kill.

        The historical incident this replaces is a 30-minute SIGKILL of a
        process that was doing nothing wrong except waiting for a human. The
        process keeps running; only the task's recorded state changes, and the
        human gets an attention item.
        """
        since = task.liveness_since
        if since is None or finding.now - since < policy.effective_approval_timeout:
            return
        try:
            wakes.finish_with_wake(store, task.task_id, "blocked", "approval")
        except TaskStoreError as exc:
            LOGGER.warning("Cannot block %s on its approval timeout: %s", task.key, exc)
            return
        self._raise_attention(
            task,
            f"BLOCKED: {task.key} is blocked(approval) after "
            f"{policy.effective_approval_timeout:g}s parked. {finding.payload()}",
            f"liveness:approval:{task.task_id}:{task.epoch}",
        )

    def _record_loss(
        self,
        store: TaskStore,
        task: TaskRecord,
        finding: liveness_detector.Finding,
        payload: str,
    ) -> int:
        """Record a typed loss the executor already owns, and wake immediately.

        Writing ``lost`` here is not a kill: the classifier only reaches this
        branch when the executor supplied a typed loss marker over a process
        that is not running, so the process is already gone and the kernel is
        recording an attribution rather than choosing one. ``finish_with_wake``
        arms ``any_child_lost``, so the manager is told without waiting for the
        cohort.

        The evidence and the terminal are one transaction. As two they could
        interleave: a crash between them left evidence for a loss that was
        never recorded, and a competing writer could finish the task in the
        gap so the evidence landed on somebody else's outcome. The evidence
        write is guarded by ``LIVE_STATES`` for the same reason - a late tick
        must not be able to annotate a task that already ended.
        """
        reason = f"lost:{finding.evidence.lost_reason}"
        states = sorted(LIVE_STATES)
        placeholders = ", ".join("?" for _ in states)
        try:
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE managed_tasks SET liveness_evidence = ? "
                    f"WHERE task_id = ? AND state IN ({placeholders})",
                    (payload, task.task_id, *states),
                )
                _record, armed = wakes.finish_with_wake(
                    store, task.task_id, "lost", reason, conn=conn
                )
                conn.execute("COMMIT")
        except TaskStoreError as exc:
            LOGGER.warning("Cannot record loss for %s: %s", task.key, exc)
            return 0
        self._output_bytes.pop(task.task_id, None)
        self._raise_attention(
            task,
            f"BLOCKED: {task.key} is {reason}. {payload}",
            f"liveness:lost:{task.task_id}:{task.epoch}",
        )
        return len(armed)

    def _raise_attention(
        self,
        task: TaskRecord,
        message: str,
        key: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Park one durable attention item for a human; idempotent by key.

        Parked with ``no_parent`` because that is precisely the situation: the
        item exists because no machine recipient is left to handle it. The
        manager's session is still recorded, so the trail from incident to
        owner survives.
        """
        subject = session_id or task.holder_session_id
        if not subject:
            return
        try:
            self.signal_store.park(
                child_session_id=subject,
                child_workspace_id=task.workspace_id,
                recorded_parent_session_id=None,
                reason="no_parent",
                message=message,
                dedupe_key=key,
            )
        except Exception as exc:  # noqa: BLE001 - attention must never break the pass
            LOGGER.warning("Cannot park attention item %s: %s", key, exc)

    def reconcile_sessions(self, sessions: Iterable[dict[str, Any]]) -> None:
        """Reconcile all sessions in one writer, tolerating partial reads."""
        if not self._supports_durable_recovery():
            return
        for session in sessions:
            try:
                self.reconcile_session(session)
            except CdesktopError as exc:
                LOGGER.warning(
                    "Cannot reconcile durable session %s: %s", session.get("id"), exc
                )

    def reconcile_session(self, session: dict[str, Any]) -> None:
        session_id = str(session["id"])
        commands = self.queue.commands(session_id)
        if self.ownership.is_quarantined(session_id):
            # An explicit retired/superseded ownership transition is the only
            # quarantine trigger.  Queued delivery must never auto-resume the
            # session into a shared worktree: cancel, never requeue, never
            # dispatch.  Ordinary completed or failed turns take the normal
            # path below and stay resumable.
            self._cancel_quarantined(commands)
            return
        processes = self.client.execution_processes(session_id)
        by_process = {str(item.get("id")): item for item in processes}
        self._wake_parent_for_terminal_commands(session, commands)
        self._reconcile_signal_policy(session, commands, processes)
        for command in commands:
            if command.state != "claimed":
                continue
            process = by_process.get(str(command.execution_process_id))
            if process is None:
                # Absence is not terminal evidence. A partial read must never
                # release a claim while its execution may still be running.
                continue
            if process.get("status") == "running":
                # A running execution is the executor's to own. There is
                # deliberately no branch here that stops it: the two that used
                # to exist - a dead stream, and a wall-clock idle timer - both
                # ran in the same tick as the wake-only detector, so the kernel
                # woke a manager about limbo and then killed the process out
                # from under it, then read its own kill back as `lost`. Wall-
                # clock kill timers are banned by liveness-spec.md; the manager
                # holds every replace and cancel decision.
                continue
            self.reconcile_child_terminal(
                session, status=str(process.get("status") or "terminal")
            )
            self._interrupt_and_requeue(command)

        # The native dispatcher remains the only claimant.  The gate prevents
        # a reconnect storm when cdesktop is reachable but the model is not.
        if hasattr(self.client, "dispatch_queued") and self._online():
            self.client.dispatch_queued(session_id)

    def _reconcile_signal_policy(
        self,
        session: dict[str, Any],
        commands: Iterable[DurableCommand],
        processes: Iterable[dict[str, Any]],
    ) -> None:
        """Turn opt-in observable conditions into one durable parent follow-up."""
        policy = self.signal_store.signal_policy(str(session["id"]))
        if not policy.conditions:
            return
        process_rows = list(processes)
        command_rows = list(commands)
        terminal = any(
            process.get("status") not in {None, "running"} for process in process_rows
        )
        pressure = max(
            (
                value
                for process in process_rows
                for value in [
                    _context_pressure(
                        self.client.normalized_snapshot(str(process["id"]))
                    )
                ]
                if value is not None
            ),
            default=None,
        )
        active = any(
            command.delivery_state(None) not in {"terminal", "rejected"}
            for command in command_rows
        )
        idle_seconds = _idle_seconds(process_rows, self.clock()) if active else None
        for condition in policy.conditions:
            triggered = condition == "terminal" and terminal
            if condition.startswith("context-pressure:") and pressure is not None:
                triggered = pressure >= float(condition.split(":", 1)[1])
            if condition.startswith("idle:") and idle_seconds is not None:
                triggered = idle_seconds > int(condition.split(":", 1)[1])
            if not triggered:
                continue
            session_id = str(session["id"])
            key = f"signal-policy:{session_id}:{condition}"
            if self.signal_store.has_dedupe_key(key):
                continue
            escalate(
                self.client,
                child_session_id=session_id,
                child_workspace_id=str(session.get("workspace_id") or "") or None,
                parent_session_id=(
                    str(session["parent_session_id"])
                    if session.get("parent_session_id")
                    else None
                ),
                message=f"STATUS: signal policy triggered: {condition}",
                store=self.signal_store,
                dedupe_key=key,
            )

    def _online(self) -> bool:
        now = time.monotonic()
        if now < self._offline_until:
            return False
        if self.probe():
            self._backoff = 1.0
            self._offline_until = 0.0
            return True
        self._offline_until = now + self._backoff
        self._backoff = min(self._backoff * 2.0, 30.0)
        return False

    def _cancel_quarantined(self, commands: Iterable[DurableCommand]) -> None:
        for command in commands:
            if command.state in COMMAND_TERMINAL_STATES:
                continue
            if command.id in self._cancelled:
                continue
            self.queue.interrupt(command)
            self._cancelled.add(command.id)

    def reconcile_child_terminal(
        self,
        child_session: dict[str, Any],
        *,
        status: str,
    ) -> None:
        parent = child_session.get("parent_session_id")
        if not parent:
            return
        # The key stays bound to the child and status, not the destination, so
        # a redirected notification is still one logical command.
        key = f"child-terminal:{child_session['id']}:{status}"
        self._notify_live_parent(
            parent_session_id=str(parent),
            child_session_id=str(child_session["id"]),
            message=f"CHILD_TERMINAL: {child_session['id']} {status}",
            key=key,
        )

    def _wake_parent_for_terminal_commands(
        self, child_session: dict[str, Any], commands: Iterable[DurableCommand]
    ) -> None:
        """Wake once per native terminal transition; cdesktop owns the dedupe fence."""
        parent = child_session.get("parent_session_id")
        if not parent:
            return
        child_id = str(child_session["id"])
        for command in commands:
            # A lifecycle notification is an output of this reconciler, never
            # a new child event. Treating it as input creates a fresh command
            # id and defeats the original dedupe key on every hop.
            if command.dedupe_key and command.dedupe_key.startswith(
                LIFECYCLE_NOTIFICATION_KEY_PREFIXES
            ):
                continue
            state = command.delivery_state(None)
            if state not in {"terminal", "rejected"}:
                continue
            key = f"child-command:{command.id}:{command.state}"
            self._notify_live_parent(
                parent_session_id=str(parent),
                child_session_id=child_id,
                message=f"CHILD_DELIVERY: {child_id} {command.id} {command.state}",
                key=key,
            )

    def _notify_live_parent(
        self,
        *,
        parent_session_id: str,
        child_session_id: str,
        message: str,
        key: str,
    ) -> None:
        """Deliver once to a live successor, or park the wake in the inbox.

        A quarantined parent may still have a successor linked later, so this
        stays retryable. Parking is what makes the gap visible in the
        meantime: a warning alone left the child's terminal signal invisible
        for as long as - possibly forever - no successor appeared. The shared
        dedupe key keeps repeated ticks to one parked record and lets a later
        delivery resolve it.
        """
        if key in self._notified or self.signal_store.has_terminal_dedupe_key(key):
            return
        if parent_session_id == child_session_id:
            LOGGER.error(
                "Refusing lifecycle notification %s because session %s is its own parent",
                key,
                child_session_id,
            )
            self._reject_invalid_route(
                child_session_id, parent_session_id, message, key
            )
            return
        destination = resolve_live_successor(self.ownership, parent_session_id)
        if destination is None:
            self.signal_store.park(
                child_session_id=child_session_id,
                child_workspace_id=None,
                recorded_parent_session_id=parent_session_id,
                reason="parent_unreachable",
                message=message,
                dedupe_key=key,
            )
            return
        if destination == child_session_id:
            LOGGER.error(
                "Refusing lifecycle notification %s because parent succession resolves "
                "back to child session %s",
                key,
                child_session_id,
            )
            self._reject_invalid_route(
                child_session_id, parent_session_id, message, key
            )
            return
        self.queue.notify_parent(destination, child_session_id, message, key)
        self._notified.add(key)
        self.signal_store.resolve_dedupe_key(key)

    def _reject_invalid_route(
        self,
        child_session_id: str,
        parent_session_id: str,
        message: str,
        key: str,
    ) -> None:
        parked = self.signal_store.park(
            child_session_id=child_session_id,
            child_workspace_id=None,
            recorded_parent_session_id=parent_session_id,
            reason="parent_unreachable",
            message=message,
            dedupe_key=key,
        )
        if parked.status == "parked":
            self.signal_store.resolve(parked.escalation_id)
        self._notified.add(key)

    def _interrupt_and_requeue(self, command: DurableCommand) -> None:
        # Lifecycle writes are idempotent in cdesktop; duplicate ticks cannot
        # manufacture a second command because requeue retains dedupe_key.
        if command.id in self._requeued:
            return
        self.queue.requeue(command)
        self._requeued.add(command.id)
