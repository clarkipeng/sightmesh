from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from . import execution_routing, wakes
from .cdesktop import (
    PROCESS_FAILURE_STATUSES,
    CdesktopClient,
    CdesktopError,
    CdesktopInterruptedError,
    CdesktopPendingError,
    CdesktopRejectedError,
    is_effect_not_found,
    latest_execution_process,
    process_failure_reason,
    process_provider_outcome,
)
from .effects import (
    Effect,
    EffectBusy,
    EffectJournal,
    new_owner_instance,
    request_hash,
)
from .escalation import CDESKTOP_SESSION_ENV, EscalationStore, LauncherIdentity
from .execution_routing import ExecutionRoutingError
from .liveness import Budget, resolve_policy, trusted_policy
from .pool.core import PoolError
from .profiles import ProfileStore, validate_provider
from .succession import (
    COMMAND_TERMINAL_STATES,
    REROUTE_OUTCOMES,
    SWEEPABLE_OUTCOMES,
    OwnershipStore,
    SuccessionError,
    advance_route_after_outcome,
    cool_provider_outcome,
    routing_outcome,
    transfer_ownership,
)
from .task_store import (
    StaleTransition,
    TaskFence,
    TaskRecord,
    TaskStore,
    TaskStoreError,
)

LOGGER = logging.getLogger("sightmesh.sdk")

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
MAX_ATTEMPTS = 3
#: A task id no ``uuid5`` derivation can produce, reserved for the launch
#: contract probe so the lookup can only ever answer "not found".
CONTRACT_PROBE_TASK_ID = "00000000-0000-0000-0000-000000000000"
#: Spec keys that describe *how this run was resolved* rather than *what was
#: asked for*, and so must stay out of the identity fingerprint ``start()``
#: compares. All three are environment- or upgrade-dependent; including any of
#: them makes an unchanged ``start()`` call fail after a config change.
_SPEC_FINGERPRINT_EXCLUSIONS = frozenset({"target", "setup_script", "detection"})
T = TypeVar("T")


class SightMeshError(RuntimeError):
    pass


class BatchError(SightMeshError):
    def __init__(self, message: str, result: BatchResult[Any]) -> None:
        self.result = result
        super().__init__(message)


PERMISSION_POLICIES = frozenset(
    {"BYPASS_PERMISSIONS", "ACCEPT_EDITS", "PLAN", "SUPERVISED"}
)


@dataclass(frozen=True)
class WorkerSpec:
    key: str
    prompt: str
    repo: str
    base: str = "main"
    profile: str | None = None
    executor: str | None = None
    model: str | None = None
    reasoning: str | None = None
    permission: str = "BYPASS_PERMISSIONS"
    children: int = 0
    #: Explicit route class override. ``None`` lets scope and risk decide.
    route_class: str | None = None
    #: Liveness detection settings (docs/liveness-spec.md, "WorkerSpec
    #: additions"). ``None`` means "inherit the trusted floor". These are
    #: requests, not grants: ``liveness.resolve_policy`` takes the stricter of
    #: the request and the manager's configured floor on every axis, so a
    #: worker can tighten its own detection but never weaken it.
    progress_timeout: float | None = None
    approval_timeout: float | None = None
    budget: Budget | None = None


@dataclass(frozen=True)
class Command:
    worker: str
    prompt: str
    _operation_id: str = field(
        default_factory=lambda: uuid.uuid4().hex, init=False, repr=False, compare=False
    )


@dataclass(frozen=True)
class Worker:
    key: str
    state: str
    repo: str
    base: str
    workspace_id: str | None
    session_id: str | None
    attempts: int
    max_attempts: int
    child_limit: int
    checkpoint: str | None
    result: str | None

    @classmethod
    def from_record(cls, record: TaskRecord) -> Worker:
        return cls(
            key=record.key,
            state=record.state,
            repo=str(record.spec["repo"]),
            base=str(record.spec["base"]),
            workspace_id=record.workspace_id,
            session_id=record.holder_session_id,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            child_limit=record.child_limit,
            checkpoint=record.checkpoint,
            result=record.result,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class BatchResult(Generic[T]):
    items: Mapping[str, T]
    errors: Mapping[str, str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "items": {
                key: value.to_dict() if hasattr(value, "to_dict") else value
                for key, value in self.items.items()
            },
            "errors": dict(self.errors),
        }


class SightMesh:
    """Human-first task API over SightMesh policy and cdesktop native state."""

    def __init__(
        self,
        *,
        url: str | None = None,
        client: CdesktopClient | None = None,
        store: TaskStore | None = None,
        ownership: OwnershipStore | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.client = client or CdesktopClient(url)
        self.store = store or TaskStore()
        self.ownership = ownership or OwnershipStore()
        self.environment = environment if environment is not None else os.environ
        self.owner_instance = new_owner_instance()
        self.journal = EffectJournal(self.store)
        self.wakes = wakes.WakeDelivery(self.client, self.store, self.ownership)
        self.contract_probe: str | None = None

    def start(self, spec: WorkerSpec | None = None, **kwargs: Any) -> Worker:
        requested = spec or WorkerSpec(**kwargs)
        result = self.start_all([requested])
        if not result.ok:
            raise BatchError(result.errors[requested.key], result)
        return result.items[requested.key]

    ensure = start

    def start_all(self, specs: Iterable[WorkerSpec]) -> BatchResult[Worker]:
        requested = list(specs)
        scope, parent = self._context()
        if parent is not None and parent.state != "active":
            raise SightMeshError(
                f"Task {parent.key!r} cannot start children while {parent.state}"
            )
        prepared = [
            self._prepare_spec(scope, spec, top_level=parent is None)
            for spec in requested
        ]
        self._require_contract()
        reservations = self.store.reserve_all(
            scope=scope,
            parent_task_id=parent.task_id if parent else None,
            specs=prepared,
            max_attempts=MAX_ATTEMPTS,
        )
        workers: dict[str, Worker] = {}
        errors: dict[str, str] = {}
        for record, _inserted in reservations:
            try:
                active = self._start_reserved(record)
                workers[record.key] = Worker.from_record(active)
            except (CdesktopError, SightMeshError, TaskStoreError, ValueError) as exc:
                errors[record.key] = str(exc)
        return BatchResult(workers, errors)

    ensure_all = start_all

    def send(self, worker: str, prompt: str) -> Any:
        result = self.send_all([Command(worker, prompt)])
        if not result.ok:
            raise BatchError(result.errors[worker], result)
        return result.items[worker]

    def send_all(
        self, commands: Iterable[Command] | Mapping[str, str]
    ) -> BatchResult[Any]:
        entries = (
            [Command(key, value) for key, value in commands.items()]
            if isinstance(commands, Mapping)
            else list(commands)
        )
        keys = [entry.worker for entry in entries]
        if len(keys) != len(set(keys)):
            raise SightMeshError("A command batch cannot target a worker twice")
        targets: list[tuple[Command, TaskRecord]] = []
        for entry in entries:
            if not entry.prompt.strip():
                raise SightMeshError(f"Command for {entry.worker!r} must not be empty")
            task = self._find(entry.worker)
            if task.state not in {"active", "blocked"} or not task.holder_session_id:
                raise SightMeshError(
                    f"Task {entry.worker!r} has no active session ({task.state})"
                )
            self.ownership.assert_deliverable(task.holder_session_id)
            targets.append((entry, task))

        sender = self.environment.get(CDESKTOP_SESSION_ENV)
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for entry, task in targets:
            dedupe_key = (
                f"task-command:{task.task_id}:{task.epoch}:{entry._operation_id}"
            )
            try:
                results[entry.worker] = self.client.send(
                    task.holder_session_id,
                    entry.prompt,
                    sender,
                    dedupe_key=dedupe_key,
                    intent="continue",
                )
            except CdesktopError as exc:
                errors[entry.worker] = str(exc)
        return BatchResult(results, errors)

    def show(self, worker: str | None = None) -> Worker:
        record = self._current() if worker is None else self._find(worker)
        return Worker.from_record(record)

    def list(self) -> list[Worker]:
        scope, _parent = self._context()
        return [Worker.from_record(item) for item in self.store.list_scope(scope)]

    def checkpoint(self, text: str, worker: str | None = None) -> Worker:
        if not text.strip():
            raise SightMeshError("Checkpoint must not be empty")
        task = self._current() if worker is None else self._find(worker)
        path = self._checkpoint_path(task, hashlib.sha256(text.encode()).hexdigest())
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise SightMeshError(f"Checkpoint digest collision at {path}")
        if not path.exists():
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            os.replace(temporary, path)
        reference = str(path.relative_to(self._task_repo_path(task)))
        return Worker.from_record(self.store.checkpoint(task.task_id, reference))

    def complete(self, summary: str | None = None, worker: str | None = None) -> Worker:
        task = self._current() if worker is None else self._find(worker)
        return Worker.from_record(self._finish_locked(task, "completed", summary))

    def blocked(self, reason: str, worker: str | None = None) -> Worker:
        if not reason.strip():
            raise SightMeshError("Blocked reason must not be empty")
        task = self._current() if worker is None else self._find(worker)
        return Worker.from_record(self._finish_locked(task, "blocked", reason))

    def cancel(self, worker: str) -> Worker:
        task = self._find(worker)
        return Worker.from_record(
            self._finish_locked(task, "cancelled", None, stop_workspace=True)
        )

    def replace(self, worker: str, prompt: str | None = None) -> Worker:
        task = self._find(worker)
        with self.store.task_lock(task.task_id) as fence:
            # The snapshot used to find the task may have waited behind an
            # automatic recovery. Reloading under the same lock makes this
            # replacement and that recovery one indivisible choice.
            task = self._find(worker)
            if not task.workspace_id or not task.holder_session_id:
                raise SightMeshError(f"Task {worker!r} has no session to replace")
            replacement_prompt = prompt or self._read_checkpoint(task)
            if not replacement_prompt or not replacement_prompt.strip():
                raise SightMeshError(
                    "Replacement requires a prompt or saved checkpoint"
                )
            self._require_contract()
            target = {**task.spec["target"]}
            automatic_recovery = routing_outcome(target.get("recovery"))
            target.pop("recovery", None)
            prepared = (
                self.store.transition(
                    task.task_id,
                    expect_states=frozenset({"replacing"}),
                    expect_version=task.version,
                    assign=(
                        "epoch = epoch + 1, attempts = attempts + 1, spec_json = ?"
                        if automatic_recovery is not None
                        else "spec_json = ?"
                    ),
                    values=(
                        json.dumps(
                            {**task.spec, "target": target},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                    attempted="manual replacement resume",
                    fence=fence,
                )
                if task.state == "replacing"
                else self.store.prepare_replacement(
                    task.task_id,
                    target=target,
                    expect_version=task.version,
                    fence=fence,
                )
            )
            return Worker.from_record(
                self._replace_prepared(prepared, replacement_prompt, fence)
            )

    def _finish(
        self, task: TaskRecord, state: str, result: str | None, fence: TaskFence
    ) -> TaskRecord:
        """Make the durable terminal decision and enqueue its parent wake."""
        try:
            updated, _created = wakes.finish_with_wake(
                self.store, task.task_id, state, result, fence=fence
            )
        except StaleTransition as exc:
            if exc.current.state == state:
                # A duplicate lifecycle call for the state already reached is
                # the caller repeating itself, not a conflict.
                return exc.current
            raise
        return updated

    def _finish_locked(
        self,
        task: TaskRecord,
        state: str,
        result: str | None,
        *,
        stop_workspace: bool = False,
    ) -> TaskRecord:
        """Finish one task without racing the epoch it may be launching.

        The lock protects only the durable decision. The cdesktop stop and wake
        delivery are intentionally outside it: a slow HTTP request cannot hold
        a task's lifecycle gate. Re-entry fences delivery against a newer row.
        """
        with self.store.task_lock(task.task_id) as fence:
            current = self.store.get_by_id(task.task_id)
            if current is None:
                raise SightMeshError(f"Task {task.key!r} no longer exists")
            updated = self._finish(current, state, result, fence)
            workspace_id = current.workspace_id if stop_workspace else None

        if workspace_id:
            self.client.stop_workspace(workspace_id)

        with self.store.task_lock(task.task_id):
            current = self.store.get_by_id(task.task_id)
            if current is None or current.version != updated.version:
                return current or updated
        self.wakes.pump()
        return updated

    def reconcile_provider_outcome(self, session_id: str) -> Worker | None:
        """Settle the task holding this session against what its worker did.

        Two things can have happened since the last tick. The session's own
        process may have stopped - the only place a *mid-run* provider refusal
        is ever visible, since a task that launched fine and was cut off hours
        later has no launch rejection to read - or a typed outcome may already
        be on the journal, waiting to be advanced past.
        """
        task = self.store.get_by_session(session_id)
        if task is None:
            return None
        blocked = self._observe_session_failure(task, session_id)
        if blocked is not None:
            return blocked
        return self._advance_past_outcome(self.store.get_by_id(task.task_id) or task)

    def reconcile_provider_outcomes(self) -> list[Worker]:
        """Advance every task the journal shows is stuck, session or no session.

        A launch rejected before the task ever activated holds no session, so
        no session-keyed sweep can ever reach it, and a task mid-replacement
        holds no *live* session either. This one is keyed on durable state
        instead - the journal for typed outcomes, the task's own state for a
        replacement that never got filled - which is where both actually live.
        """
        advanced: list[Worker] = []
        # A crash after an epoch becomes terminal but before it blocks leaves
        # one task in both sources. Task identity, rather than that transient
        # state, is the invariant: settle each task at most once per sweep.
        pending_by_id: dict[str, TaskRecord] = {
            task.task_id: task
            for task in (
                self.store.get_by_id(effect.task_id)
                for effect in self.journal.with_outcomes(SWEEPABLE_OUTCOMES)
            )
            if task is not None
        }
        for task in self.store.list_state("replacing"):
            pending_by_id.setdefault(task.task_id, task)
        pending = list(pending_by_id.values())
        for task in pending:
            # Each task settles on its own: one whose replacement launch is
            # itself rejected must not stop the sweep from reaching the rest.
            # The isolation covers every error the advance can raise, since
            # SuccessionError, ExecutionRoutingError, and PoolError are plain
            # RuntimeErrors that would otherwise escape and skip the remainder.
            try:
                worker = self._advance_past_outcome(task)
            except (
                CdesktopError,
                ExecutionRoutingError,
                PoolError,
                SightMeshError,
                SuccessionError,
                TaskStoreError,
            ) as exc:
                LOGGER.info("Cannot advance task %s this tick: %s", task.key, exc)
                continue
            if worker is not None:
                advanced.append(worker)
        return advanced

    def _observe_session_failure(
        self, task: TaskRecord, session_id: str
    ) -> Worker | None:
        """Record what a stopped worker process reports, so a live run can move.

        Without this, only a *launch* rejection could ever reroute: a task that
        started fine and hit a rate limit an hour in has no rejection anywhere,
        its effect stays ``launched`` forever, and the task hangs silently.

        Only typed fields are read - the process status and cdesktop's own
        outcome classification - never the transcript. A capacity or auth
        signal becomes a typed outcome on the journal, which the ordinary
        advance path then acts on. A process that failed with no such signal
        failed at its work, so the task blocks with the process's typed reason
        and wakes its manager, rather than hanging or being rerouted on a
        guess.
        """
        with self.store.task_lock(task.task_id) as fence:
            current = self.store.get_by_id(task.task_id)
            if current is None:
                return None
            return self._observe_session_failure_locked(current, session_id, fence)

    def _observe_session_failure_locked(
        self, task: TaskRecord, session_id: str, fence: TaskFence
    ) -> Worker | None:
        if task.state != "active":
            return None
        effect = self.journal.get(task.task_id, task.epoch)
        if effect is None or effect.state != "launched":
            # Nothing launched under this epoch, or its outcome is already
            # recorded; either way the process tells us nothing new.
            return None
        process = latest_execution_process(
            [
                item
                for item in self.client.execution_processes(session_id)
                if item.get("run_reason") == "codingagent"
            ]
        )
        if (
            process is None
            or str(process.get("status") or "") not in PROCESS_FAILURE_STATUSES
        ):
            return None
        outcome, retry_at = process_provider_outcome(process)
        if outcome is not None:
            # A provider refusal is not something a retry fixes, so it is
            # recorded whatever else the session has queued: the reroute
            # fences this epoch and quarantines the session, which cancels
            # that queue on the way out.
            self._record_provider_outcome(task, outcome, retry_at)
            return None
        if self._queue_still_owns(session_id):
            # cdesktop's own durable recovery requeues a claimed command whose
            # execution died, so this failure is one it is about to retry.
            # Blocking here would strand a task that is still being worked -
            # and `blocked` is not a legal predecessor of `active`, so nothing
            # could put it back.
            return None
        return self._block_unroutable(task, process_failure_reason(process), fence)

    def _queue_still_owns(self, session_id: str) -> bool:
        """Whether the native command queue has unfinished work for a session."""
        return any(
            str(row.get("state") or row.get("status") or "pending")
            not in COMMAND_TERMINAL_STATES
            for row in self.client.session_commands(session_id)
        )

    def _advance_past_outcome(self, task: TaskRecord) -> Worker | None:
        """Move one task past a typed capacity, auth, or provider outcome.

        The typed outcome on the effect journal is the only trigger. A task
        that failed to build, failed its tests, or blocked itself carries no
        provider outcome at all, so it can never enter this path - however
        much its transcript may look like a rate limit.

        The effect is read here, from the task row this call was handed, rather
        than accepted from the caller. A caller that found the effect first
        holds a task row that may since have advanced an epoch, and acting on a
        superseded epoch's outcome reroutes a run that already moved on; taking
        no effect as an argument makes that pairing impossible to get wrong.
        """
        with self.store.task_lock(task.task_id) as fence:
            current = self.store.get_by_id(task.task_id)
            if current is None:
                return None
            return self._advance_past_outcome_locked(current, fence)

    def _advance_past_outcome_locked(
        self, task: TaskRecord, fence: TaskFence
    ) -> Worker | None:
        """Advance a fresh task snapshot while its launch intent is locked."""
        target = task.spec.get("target", {})
        effect = self.journal.get(task.task_id, task.epoch)
        if task.state == "replacing":
            # Only a replacement this path opened is one it can finish. A
            # manual ``replace()`` carries a prompt that lives nowhere but its
            # caller's hands, so resuming it here would quietly re-run the
            # original work instead - and its failure is already visible to the
            # human who invoked it. ``replace()`` clears the marker, so the two
            # can never be confused.
            recovery = routing_outcome(target.get("recovery"))
            if recovery is None:
                return None
            return self._resume_replacement(task, effect, fence)
        if task.state not in {"active", "blocked"}:
            return None
        outcome = (
            routing_outcome(effect.outcome)
            if effect is not None and effect.state == "terminal"
            else None
        )
        if outcome is None:
            return None
        if task.attempts >= task.max_attempts:
            # The circuit breaker has tripped. Saying so once is the whole
            # difference between a stopped task and a hung one.
            return self._block_unroutable(
                task,
                f"{outcome} on route {target.get('route_id')}; "
                f"{task.max_attempts}-attempt circuit breaker tripped",
                fence,
            )

        route_id = target.get("route_id")
        route_class = target.get("route_class") or execution_routing.DEFAULT_ROUTE_CLASS
        if target.get("failover") == "pinned":
            # The operator pinned this task to one target and switched
            # automatic failover off. It blocks with the reason rather than
            # silently going nowhere, so `replace()` remains a human's call.
            return self._block_unroutable(
                task,
                f"{outcome} on {route_id}; automatic failover is off",
                fence,
            )

        selection = advance_route_after_outcome(
            execution_routing.ExecutionRoutingStore().load(),
            outcome=outcome,
            route_class=route_class,
            failed_binding_id=target.get("auth_binding_id"),
        )
        if selection.status != "resolved" or selection.target is None:
            return self._block_unroutable(
                task,
                f"{outcome} on route {route_id} ({route_class}); {selection.reason}",
                fence,
            )

        selected = selection.target
        next_target = {
            "executor": selected.executor,
            "model": selected.model,
            "reasoning": task.spec.get("reasoning"),
            "provider_id": self._default_provider_id(),
            "auth_binding_id": selected.auth_binding_id,
            "route_class": selected.route_class,
            "route_id": selected.route_id,
            "billing_class": selected.billing_class,
            "failover": "auto",
            "recovery": outcome,
        }
        self._require_contract()
        prepared = self.store.prepare_replacement(
            task.task_id, target=next_target, expect_version=task.version, fence=fence
        )
        return Worker.from_record(
            self._launch_prepared(prepared, str(task.spec["prompt"]), fence)
        )

    def _resume_replacement(
        self, task: TaskRecord, effect: Effect | None, fence: TaskFence
    ) -> Worker | None:
        """Settle a task whose replacement epoch was opened but never filled.

        ``prepare_replacement`` and the launch that fills it are two steps, so
        anything between them - a crash, an unreachable executor, a rejected
        replacement launch - leaves the task in ``replacing``. Neither
        reconciler used to look at that state, so such a task was invisible to
        both and simply stopped.

        The already-open epoch's effect says which of the two things to do, and
        nothing here needs to know how the task got here: an epoch that is
        merely unfilled gets filled, and one that already ended blocks with its
        outcome. Blocking is the whole settlement even for a typed outcome,
        because a *blocked* task with a terminal outcome is exactly what the
        ordinary advance path handles - so the chain moves on the next tick
        through one code path rather than two.
        """
        if effect is not None and effect.state == "terminal":
            return self._block_unroutable(
                task, f"replacement epoch ended: {effect.outcome}", fence
            )
        self._require_contract()
        return Worker.from_record(
            self._launch_prepared(task, str(task.spec["prompt"]), fence)
        )

    def _block_unroutable(
        self, task: TaskRecord, reason: str, fence: TaskFence
    ) -> Worker | None:
        """Block a task that cannot advance, and report only a real change.

        A task blocked at launch already carries its typed outcome as its
        result, and a terminal effect stays in the sweep's view forever - so
        re-blocking it every tick would report the same non-event over and
        over. The reason is recorded when the task still had somewhere to move
        from; after that, silence is accurate.
        """
        if task.state == "blocked":
            return None
        return Worker.from_record(self._finish(task, "blocked", reason, fence))

    def _record_provider_outcome(
        self, task: TaskRecord, outcome: str, retry_at: float | None
    ) -> str:
        """Cool what the outcome condemns, then mark this epoch terminal.

        Cooling goes first because pool cooling is monotonic and therefore
        idempotent: cooling an account that a later crash makes us cool again
        costs nothing, while marking terminal first and crashing before the
        cool leaves an exhausted account eligible forever - the reconcile that
        reads the outcome sees a terminal effect and moves on without ever
        looking at whether the binding was cooled.
        """
        if outcome in REROUTE_OUTCOMES:
            target = task.spec.get("target", {})
            binding_id = target.get("auth_binding_id")
            # A free route owns no account, so its shared sentinel names no
            # binding to cool. Writing one would put a phantom account into
            # pool state - the single source of account truth.
            if binding_id and binding_id != execution_routing.FREE_AUTH_BINDING:
                cool_provider_outcome(
                    execution_routing.ExecutionRoutingStore().load(),
                    outcome=outcome,
                    binding_id=str(binding_id),
                    route_class=target.get("route_class"),
                    route_id=target.get("route_id"),
                    retry_at=retry_at,
                )
        self.journal.mark_terminal(task.task_id, task.epoch, outcome, retry_at)
        return outcome

    def _launch_prepared(
        self, prepared: TaskRecord, replacement_prompt: str, fence: TaskFence
    ) -> TaskRecord:
        """Fill the epoch ``prepare_replacement`` opened.

        A task that already holds a session hands ownership to its successor.
        One whose launch was rejected before it ever activated has no session
        to transfer and no workspace to reuse, so its new epoch opens a
        workspace exactly as the first attempt would have.
        """
        if prepared.workspace_id and prepared.holder_session_id:
            return self._replace_prepared(prepared, replacement_prompt, fence)
        workspace_id, session_id = self._journaled_launch(
            prepared,
            {
                "kind": "workspace",
                "request": self._workspace_request(prepared, replacement_prompt),
            },
            fence,
        )
        active = self.store.activate(
            prepared.task_id,
            workspace_id=workspace_id,
            session_id=session_id,
            fence=fence,
        )
        self._record_launcher(active)
        return active

    def _replace_prepared(
        self, prepared: TaskRecord, replacement_prompt: str, fence: TaskFence
    ) -> TaskRecord:
        target = prepared.spec["target"]
        request = self.client.session_launch_request(
            name=prepared.key,
            prompt=replacement_prompt,
            executor=target["executor"],
            permission_policy=prepared.spec["permission"],
            model=target.get("model"),
            reasoning=target.get("reasoning"),
            provider_id=target.get("provider_id"),
            auth_binding_id=target.get("auth_binding_id"),
        )

        launch = {
            "kind": "session",
            "workspace_id": prepared.workspace_id,
            "caller_session_id": prepared.holder_session_id,
            "request": request,
        }

        def spawn() -> str:
            return self._journaled_launch(prepared, launch, fence)[1]

        transfer = transfer_ownership(
            self.client,
            self.ownership,
            source_session_id=prepared.holder_session_id,
            spawn=spawn,
            reason="managed task replacement",
            logical_key=f"task:{prepared.task_id}:epoch:{prepared.epoch}",
        )
        active = self.store.activate(
            prepared.task_id,
            workspace_id=prepared.workspace_id,
            session_id=transfer.successor_session_id,
            fence=fence,
        )
        self._record_launcher(active)
        return active

    def _workspace_request(self, task: TaskRecord, prompt: str) -> dict[str, Any]:
        target = task.spec["target"]
        return self.client.workspace_launch_request(
            name=task.key,
            repo_path=Path(task.spec["repo_path"]),
            target_branch=task.spec["base"],
            executor=target["executor"],
            prompt=prompt,
            use_worktree=True,
            permission_policy=task.spec["permission"],
            model=target.get("model"),
            reasoning=target.get("reasoning"),
            provider_id=target.get("provider_id"),
            setup_script=task.spec.get("setup_script"),
            auth_binding_id=target.get("auth_binding_id"),
        )

    def _start_reserved(self, task: TaskRecord) -> TaskRecord:
        """Launch a reservation while sharing terminal writers' task fence."""
        with self.store.task_lock(task.task_id) as fence:
            current = self.store.get_by_id(task.task_id)
            if current is None:
                raise SightMeshError(f"Task {task.key!r} no longer exists")
            return self._start_reserved_locked(current, fence)

    def _start_reserved_locked(self, task: TaskRecord, fence: TaskFence) -> TaskRecord:
        if task.state != "reserved":
            return task
        request = self._workspace_request(task, str(task.spec["prompt"]))
        self._wait_for_launch_capacity(task)
        workspace_id, session_id = self._journaled_launch(
            task, {"kind": "workspace", "request": request}, fence
        )
        active = self.store.activate(
            task.task_id, workspace_id=workspace_id, session_id=session_id, fence=fence
        )
        self._record_launcher(active)
        return active

    def _wait_for_launch_capacity(self, task: TaskRecord) -> None:
        """Kernel-side admission (#88): direct launches bypass cdesktop's queued
        dispatch cap, so an unbounded fan-out starves every queued wake and
        resume. Wait (bounded) for managed concurrency to drop under the cap,
        then launch; past the deadline refuse with a typed, retryable error
        instead of stampeding the host.
        """
        cap = int(os.environ.get("SIGHTMESH_MAX_ACTIVE_WORKERS", "4"))
        deadline = time.monotonic() + float(
            os.environ.get("SIGHTMESH_LAUNCH_WAIT_SECONDS", "90")
        )
        delay = 0.5
        while True:
            running = self.store.count_running()
            if running < cap:
                return
            if time.monotonic() >= deadline:
                raise SightMeshError(
                    f"capacity: {running} managed workers running >= cap {cap}; "
                    f"{task.key!r} stays reserved - retry start (it adopts the reservation)"
                )
            time.sleep(delay)
            delay = min(delay * 2, 5.0)

    def _journaled_launch(
        self, task: TaskRecord, launch: Mapping[str, Any], fence: TaskFence
    ) -> tuple[str, str]:
        """Reserve, launch once, and record the native identifiers.

        The journal row is written before the native call and advanced after
        it, so a crash anywhere in between is resolved on the next run by
        adopting the reserved epoch instead of forking a second session.
        """
        # A concurrent starter that loses the reservation race waits for the
        # winner and adopts its launch: duplicate start() converges on one
        # worker instead of erroring (contract: "duplicate insert returns the
        # existing effect"). The wait is bounded well under the reservation
        # lease so a crashed winner is recovered by retry, not by takeover here.
        deadline = time.monotonic() + ADOPT_TIMEOUT_SECONDS
        delay = 0.05
        while True:
            try:
                effect, _took_over = self.journal.reserve(
                    task.task_id,
                    task.epoch,
                    request_hash(launch),
                    self.owner_instance,
                )
                break
            except EffectBusy:
                if time.monotonic() >= deadline:
                    raise
                # The owner that made the native call must reacquire the fence
                # to publish it.  Waiting here while holding it deadlocks a
                # duplicate starter behind that owner.
                with fence.external_io():
                    time.sleep(delay)
                delay = min(delay * 2, 0.5)
        if effect.state == "launched" and effect.workspace_id and effect.session_id:
            return effect.workspace_id, effect.session_id
        try:
            with fence.external_io():
                native = self.client.managed_launch(
                    task.task_id, task.epoch, dict(launch)
                )
        except CdesktopRejectedError as exc:
            # 424/425 mean the outcome is unknowable or still owned; the
            # reservation must stay adoptable for a retry. Anything else is a
            # definitive rejection: record the typed outcome on this epoch's
            # effect so callers never have to grep error text, and block the
            # task so it is not left `reserved` and relaunchable - a retry is an
            # explicit new epoch via replace().
            current = self.store.get_by_id(task.task_id)
            if current is None or (current.epoch, current.version) != (task.epoch, task.version):
                self.journal.mark_terminal(task.task_id, task.epoch, "superseded")
            elif not isinstance(exc, (CdesktopInterruptedError, CdesktopPendingError)):
                outcome = self._record_provider_outcome(
                    task, _rejection_outcome(exc.status), exc.retry_at
                )
                self._finish(task, "blocked", f"launch rejected: {outcome}", fence)
            raise
        workspace_id, session_id = self._effect_ids(task, native, fence)
        current = self.store.get_by_id(task.task_id)
        if current is None or (current.epoch, current.version) != (task.epoch, task.version):
            self.journal.mark_terminal(task.task_id, task.epoch, "superseded")
            self.client.stop_workspace(workspace_id)
            raise SightMeshError(f"Native launch for {task.key!r} was superseded")
        self.journal.mark_launched(task.task_id, task.epoch, workspace_id, session_id)
        return workspace_id, session_id

    def _effect_ids(
        self, task: TaskRecord, effect: Mapping[str, Any], fence: TaskFence
    ) -> tuple[str, str]:
        state = str(effect.get("state") or "")
        if state == "lost":
            reason = str(effect.get("reason") or "lost")
            self.journal.mark_terminal(task.task_id, task.epoch, f"lost:{reason}")
            self._finish(task, "lost", reason, fence)
            raise SightMeshError(f"Native launch for {task.key!r} was lost")
        workspace_id = effect.get("workspace_id")
        session_id = effect.get("session_id")
        if state != "active" or not workspace_id or not session_id:
            raise SightMeshError(
                f"Native launch for {task.key!r} is {state or 'invalid'}"
            )
        return str(workspace_id), str(session_id)

    def _prepare_spec(
        self, scope: str, requested: WorkerSpec, *, top_level: bool = True
    ) -> dict[str, Any]:
        self._validate_spec(requested)
        repo = self._resolve_repo(requested.repo)
        repo_path = Path(str(repo["path"])).expanduser().resolve()
        self._reject_ephemeral(repo_path)
        public = {
            "key": requested.key,
            "prompt": requested.prompt,
            "repo": str(repo.get("name") or repo_path.name),
            "repo_id": str(repo["id"]),
            "repo_path": str(repo_path),
            "base": requested.base,
            "profile": requested.profile,
            "executor": requested.executor,
            "model": requested.model,
            "reasoning": requested.reasoning,
            "permission": requested.permission,
            "children": requested.children,
            "route_class": requested.route_class,
        }
        existing = self.store.get(scope, requested.key)
        if existing is not None:
            old_public = {
                key: value
                for key, value in existing.spec.items()
                if key not in _SPEC_FINGERPRINT_EXCLUSIONS
            }
            # Compare only the fields the stored spec actually carries. A field
            # this version added is one the running task's version could not
            # have recorded, and its absence describes no disagreement about
            # the work - so an upgrade mid-flight must not read as one.
            if old_public != {
                key: value for key, value in public.items() if key in old_public
            }:
                raise SightMeshError(
                    f"Task {requested.key!r} already exists with a different specification"
                )
            return existing.spec
        return {
            **public,
            "setup_script": self._setup_script(repo_path, requested.base),
            "target": self._select_target(requested, top_level=top_level),
            # Resolved once, at reserve time, so the detector reads a settled
            # policy off the durable row instead of re-deriving it - and so a
            # worker cannot loosen its detection later by re-specifying.
            #
            # Stored *beside* the public spec rather than inside it, because
            # the public spec is the identity fingerprint `start()` compares
            # for idempotence. A resolved policy depends on the manager's
            # environment and on the shipped defaults, so folding it in made
            # an unchanged `start()` call raise "already exists with a
            # different specification" after any upgrade or config change.
            "detection": resolve_policy(
                progress_timeout=requested.progress_timeout,
                approval_timeout=requested.approval_timeout,
                budget=requested.budget,
                trusted=trusted_policy(dict(self.environment)),
            ).to_dict(),
        }

    def _select_target(
        self, spec: WorkerSpec, *, top_level: bool = True
    ) -> dict[str, Any]:
        """Choose the route class, prove it is usable, then bind the first hop.

        The class is decided once, here, and frozen into the target: every
        later failover walks the same chain rather than re-deciding what kind
        of work this is. An explicit profile or executor still names the first
        hop, but it records that class too, so it stays recoverable instead of
        being a dead end the reconciler cannot advance.
        """
        settings = execution_routing.ExecutionRoutingStore().load()
        decision = execution_routing.resolve_class(
            settings,
            execution_routing.ScopeRisk(
                route_class=spec.route_class,
                top_level=top_level,
                children=spec.children,
            ),
        )
        route_class = decision.route_class
        if decision.demoted_from:
            # Scope and risk wanted a stronger chain than this install has
            # configured. Refusing the work would make an upgrade that adds a
            # class break every dispatch that class now claims, so the work
            # runs on the default chain and says so.
            LOGGER.warning(
                "Route class %r has no usable chain for %r; falling back to %r",
                decision.demoted_from,
                spec.key,
                route_class,
            )
        if not decision.validation.valid:
            # This gate applies to overrides too. An override names the first
            # hop, not a waiver of the explicitly requested route class.
            raise SightMeshError(
                f"Route class {route_class!r} cannot start {spec.key!r}: "
                f"{decision.validation.reason}"
            )
        if spec.profile:
            profile = ProfileStore().get(spec.profile)
            validate_provider(profile, self.client.providers())
            if spec.executor and spec.executor != profile.executor:
                raise SightMeshError("executor cannot override a profile")
            return {
                "executor": profile.executor,
                "model": spec.model or profile.model,
                "reasoning": spec.reasoning or profile.reasoning,
                "provider_id": profile.provider_id,
                "auth_binding_id": None,
                "route_class": route_class,
                "route_id": f"profile:{profile.name}",
                "failover": "auto" if profile.automatic_failover else "pinned",
            }
        if spec.executor:
            return {
                "executor": spec.executor,
                "model": spec.model,
                "reasoning": spec.reasoning,
                "provider_id": None,
                "auth_binding_id": None,
                "route_class": route_class,
                "route_id": f"executor:{spec.executor}",
                "failover": "auto",
            }
        selection = execution_routing.select_route(
            settings, route_class=route_class, preferred_model=spec.model
        )
        if selection.status != "resolved" or selection.target is None:
            raise SightMeshError(
                f"Execution routing could not start {spec.key!r}: {selection.reason}"
            )
        target = selection.target
        return {
            "executor": target.executor,
            "model": target.model,
            "reasoning": spec.reasoning,
            "provider_id": self._default_provider_id(),
            "auth_binding_id": target.auth_binding_id,
            "route_class": target.route_class,
            "route_id": target.route_id,
            "billing_class": target.billing_class,
            "failover": "auto",
        }

    def _default_provider_id(self) -> str:
        providers = [
            provider
            for provider in self.client.providers()
            if provider.get("kind") == "Default"
            and provider.get("enabled")
            and provider.get("id")
        ]
        if len(providers) != 1:
            raise SightMeshError(
                "Execution routing requires one enabled cdesktop Default provider"
            )
        return str(providers[0]["id"])

    def _resolve_repo(self, handle: str) -> dict[str, Any]:
        candidate = Path(handle).expanduser()
        if candidate.is_dir():
            return self.client.register_repo(candidate)
        folded = handle.casefold()
        matches = []
        for repo in self.client.repos():
            path = Path(str(repo.get("path") or ""))
            names = {
                str(repo.get("id") or "").casefold(),
                str(repo.get("name") or "").casefold(),
                str(repo.get("display_name") or "").casefold(),
                path.name.casefold(),
            }
            if folded in names:
                matches.append(repo)
        canonical = [
            repo
            for repo in matches
            if repo.get("path")
            and not self._is_ephemeral_repo_path(Path(str(repo["path"])))
        ]
        if len(canonical) == 1:
            return canonical[0]
        if len(canonical) > 1:
            matches = canonical
        if len(matches) != 1:
            detail = "not registered" if not matches else "ambiguous"
            raise SightMeshError(
                f"Repository handle {handle!r} is {detail} in cdesktop"
            )
        return matches[0]

    def _context(self) -> tuple[str, TaskRecord | None]:
        session_id = self.environment.get(CDESKTOP_SESSION_ENV)
        parent = self.store.get_by_session(session_id) if session_id else None
        if parent:
            return parent.task_id, parent
        return (f"session:{session_id}" if session_id else "operator"), None

    def _find(self, key: str) -> TaskRecord:
        scope, _parent = self._context()
        task = self.store.get(scope, key)
        if task is None:
            raise SightMeshError(f"Unknown task: {key}")
        return task

    def _current(self) -> TaskRecord:
        session_id = self.environment.get(CDESKTOP_SESSION_ENV)
        task = self.store.get_by_session(session_id) if session_id else None
        if task is None:
            raise SightMeshError("This process is not running inside a managed task")
        return task

    def _record_launcher(self, task: TaskRecord) -> None:
        if not task.holder_session_id:
            return
        EscalationStore(self.store.path).record_launcher(
            session_id=task.holder_session_id,
            workspace_id=task.workspace_id,
            identity=LauncherIdentity(launcher="cdesktop", detail="sightmesh-task"),
        )

    def _task_repo_path(self, task: TaskRecord) -> Path:
        if not task.workspace_id:
            raise SightMeshError(f"Task {task.key!r} has no workspace")
        workspace = self.client.workspace(task.workspace_id)
        container = workspace.get("container_ref")
        if not container:
            raise SightMeshError(f"Task {task.key!r} has no worktree path")
        return Path(str(container)).expanduser().resolve() / str(task.spec["repo"])

    def _checkpoint_path(self, task: TaskRecord, digest: str) -> Path:
        return (
            self._task_repo_path(task)
            / ".context"
            / "sightmesh"
            / "checkpoints"
            / f"{digest}.md"
        )

    def _read_checkpoint(self, task: TaskRecord) -> str | None:
        if not task.checkpoint:
            return None
        root = self._task_repo_path(task)
        path = (root / task.checkpoint).resolve()
        if root not in path.parents:
            raise SightMeshError("Checkpoint reference escapes the task worktree")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SightMeshError(f"Cannot read checkpoint {path}: {exc}") from exc

    def _require_contract(self) -> str:
        """Probe the managed-launch seam instead of trusting the advertisement.

        An advertised capability with no working lookup is exactly the state
        that lets a launch path fail late, so the probe runs once per instance
        and its outcome is reported by ``doctor``.
        """
        if self.contract_probe is not None:
            return self.contract_probe
        info = self.client.info()
        capabilities = info.get("service_capabilities", {})
        if int(capabilities.get("managed_task_launch", 0)) < 1:
            raise SightMeshError(
                "cdesktop does not support the managed task launch contract"
            )
        self.contract_probe = self._probe_managed_launch()
        return self.contract_probe

    def _probe_managed_launch(self) -> str:
        lookup = getattr(self.client, "managed_effect", None)
        if lookup is None:
            # An advertised capability with no executable lookup is exactly the
            # state that lets a launch fail late. There is no "take it at its
            # word" answer: either the probe runs or the launch does not.
            raise SightMeshError(
                "cdesktop advertises the managed task launch contract but "
                "exposes no effect lookup to probe it"
            )
        try:
            effect = lookup(CONTRACT_PROBE_TASK_ID, 1)
        except CdesktopError as exc:
            if not is_effect_not_found(exc):
                raise SightMeshError(
                    f"cdesktop managed task launch probe failed: {exc}"
                ) from exc
            return "lookup"
        if not isinstance(effect, Mapping):
            raise SightMeshError(
                "cdesktop managed task launch probe returned a malformed effect"
            )
        if effect.get("state") not in {None, "", "missing", "not_found"}:
            raise SightMeshError(
                "cdesktop managed task launch probe found a reserved sentinel effect"
            )
        return "lookup"

    @staticmethod
    def _validate_spec(spec: WorkerSpec) -> None:
        if not KEY_PATTERN.fullmatch(spec.key):
            raise SightMeshError(
                "Task keys use 1 to 24 lowercase letters, numbers, hyphens, or underscores"
            )
        if not spec.prompt.strip():
            raise SightMeshError(f"Prompt for {spec.key!r} must not be empty")
        if not spec.base.strip():
            raise SightMeshError(f"Base branch for {spec.key!r} must not be empty")
        if not 0 <= spec.children <= 1000:
            raise SightMeshError("children must be between 0 and 1000")
        if spec.permission not in PERMISSION_POLICIES:
            raise SightMeshError(
                f"Unknown permission policy {spec.permission!r}; expected one of {sorted(PERMISSION_POLICIES)}"
            )
        if (
            spec.route_class is not None
            and spec.route_class not in execution_routing.ROUTE_CLASSES
        ):
            raise SightMeshError(
                "route_class must be one of "
                f"{', '.join(execution_routing.ROUTE_CLASSES)}"
            )

    @staticmethod
    def _reject_ephemeral(repo_path: Path) -> None:
        if SightMesh._is_ephemeral_repo_path(repo_path):
            raise SightMeshError(
                f"Repository {repo_path} is an ephemeral worktree; register its canonical checkout"
            )

    @staticmethod
    def _is_ephemeral_repo_path(repo_path: Path) -> bool:
        value = repo_path.expanduser().resolve().as_posix()
        return "/conductor/workspaces/" in value or "/.cdesktop-workspaces/" in value

    @staticmethod
    def _setup_script(repo_path: Path, base: str) -> str | None:
        result = subprocess.run(
            ["git", "show", f"origin/{base}:.conductor/settings.toml"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            return None
        data = tomllib.loads(result.stdout)
        scripts = data.get("scripts")
        setup = scripts.get("setup") if isinstance(scripts, dict) else None
        if setup is not None and not isinstance(setup, str):
            raise SightMeshError(
                ".conductor/settings.toml scripts.setup must be a string"
            )
        return setup.strip() if setup and setup.strip() else None


ADOPT_TIMEOUT_SECONDS = 30.0


def _rejection_outcome(status: int | None) -> str:
    """Name what the provider did, as a type routing can act on.

    Only the statuses cdesktop passes through from the provider are named:
    capacity and auth. Everything else stays a definitive rejection that
    blocks, because retrying it on another account would only fail the same
    way.

    ``provider_down`` is deliberately not produced here. The status on a
    rejection is the one SightMesh's own localhost call to cdesktop returned,
    so a 5xx describes the local service, not the model provider - mapping it
    would cool an entire account pool for a cdesktop restart. The outcome and
    everything that handles it stay in place for the day the seam reports an
    upstream provider signal of its own; cdesktop 0.2.7 exposes none.
    """
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    return f"rejected:{status if status is not None else 'unknown'}"
