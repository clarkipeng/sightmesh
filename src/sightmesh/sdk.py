from __future__ import annotations

import hashlib
import logging
import time
import os
import re
import subprocess
import tempfile
import tomllib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from . import execution_routing, wakes
from .cdesktop import (
    CdesktopClient,
    CdesktopError,
    CdesktopInterruptedError,
    CdesktopPendingError,
    CdesktopRejectedError,
    is_effect_not_found,
)
from .effects import (
    Effect,
    EffectBusy,
    EffectJournal,
    new_owner_instance,
    request_hash,
)
from .escalation import CDESKTOP_SESSION_ENV, EscalationStore, LauncherIdentity
from .liveness import Budget, resolve_policy, trusted_policy
from .profiles import ProfileStore, validate_provider
from .succession import (
    REROUTE_OUTCOMES,
    OwnershipStore,
    advance_route_after_outcome,
    cool_provider_outcome,
    transfer_ownership,
)
from .task_store import StaleTransition, TaskRecord, TaskStore, TaskStoreError

LOGGER = logging.getLogger("sightmesh.sdk")

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
MAX_ATTEMPTS = 3
#: A task id no ``uuid5`` derivation can produce, reserved for the launch
#: contract probe so the lookup can only ever answer "not found".
CONTRACT_PROBE_TASK_ID = "00000000-0000-0000-0000-000000000000"
_SPEC_FINGERPRINT_EXCLUSIONS = frozenset({"target", "setup_script", "detection"})
T = TypeVar("T")


class SightMeshError(RuntimeError):
    pass


class BatchError(SightMeshError):
    def __init__(self, message: str, result: BatchResult[Any]) -> None:
        self.result = result
        super().__init__(message)


PERMISSION_POLICIES = frozenset({"BYPASS_PERMISSIONS", "ACCEPT_EDITS", "PLAN", "SUPERVISED"})


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
    progress_timeout: float | None = None
    approval_timeout: float | None = None
    budget: Budget | None = None
    #: Explicit route class override. ``None`` lets scope and risk decide.
    route_class: str | None = None


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
        return Worker.from_record(self._finish(task, "completed", summary))

    def blocked(self, reason: str, worker: str | None = None) -> Worker:
        if not reason.strip():
            raise SightMeshError("Blocked reason must not be empty")
        task = self._current() if worker is None else self._find(worker)
        return Worker.from_record(self._finish(task, "blocked", reason))

    def cancel(self, worker: str) -> Worker:
        task = self._find(worker)
        if task.workspace_id:
            self.client.stop_workspace(task.workspace_id)
        return Worker.from_record(self._finish(task, "cancelled", None))

    def replace(self, worker: str, prompt: str | None = None) -> Worker:
        task = self._find(worker)
        if not task.workspace_id or not task.holder_session_id:
            raise SightMeshError(f"Task {worker!r} has no session to replace")
        replacement_prompt = prompt or self._read_checkpoint(task)
        if not replacement_prompt or not replacement_prompt.strip():
            raise SightMeshError("Replacement requires a prompt or saved checkpoint")
        self._require_contract()
        target = {**task.spec["target"]}
        target.pop("recovery", None)
        # A task already in ``replacing`` is a crashed replacement, not a new
        # one: resume the epoch that was prepared instead of burning another.
        prepared = (
            task
            if task.state == "replacing"
            else self.store.prepare_replacement(
                task.task_id, target=target, expect_version=task.version
            )
        )
        return Worker.from_record(self._replace_prepared(prepared, replacement_prompt))

    def _finish(self, task: TaskRecord, state: str, result: str | None) -> TaskRecord:
        """Terminate one task, record its parent's wake, then try to deliver."""
        try:
            updated, _created = wakes.finish_with_wake(
                self.store, task.task_id, state, result
            )
        except StaleTransition as exc:
            if exc.current.state == state:
                # A duplicate lifecycle call for the state already reached is
                # the caller repeating itself, not a conflict.
                return exc.current
            raise
        self.wakes.pump()
        return updated

    def reconcile_provider_outcome(self, session_id: str) -> Worker | None:
        """Advance the task holding this session past a typed provider outcome."""
        task = self.store.get_by_session(session_id)
        return self._advance_past_outcome(task) if task is not None else None

    def reconcile_provider_outcomes(self) -> list[Worker]:
        """Advance every task whose current epoch ended on a typed outcome.

        A launch rejected before the task ever activated holds no session, so
        no session-keyed sweep can ever reach it. This one is keyed on the
        journal instead, which is where the outcome actually lives.
        """
        advanced: list[Worker] = []
        for effect in self.journal.with_outcomes(REROUTE_OUTCOMES):
            task = self.store.get_by_id(effect.task_id)
            if task is None:
                continue
            # Each task settles on its own: one whose replacement launch is
            # itself rejected must not stop the sweep from reaching the rest.
            try:
                worker = self._advance_past_outcome(task, effect)
            except (CdesktopError, SightMeshError, TaskStoreError) as exc:
                LOGGER.info("Cannot advance task %s this tick: %s", task.key, exc)
                continue
            if worker is not None:
                advanced.append(worker)
        return advanced

    def _advance_past_outcome(
        self, task: TaskRecord, effect: Effect | None = None
    ) -> Worker | None:
        """Move one task past a typed capacity, auth, or provider outcome.

        The typed outcome on the effect journal is the only trigger. A task
        that failed to build, failed its tests, or blocked itself carries no
        provider outcome at all, so it can never enter this path - however
        much its transcript may look like a rate limit.
        """
        target = task.spec.get("target", {})
        if task.state == "replacing" and target.get("recovery") in REROUTE_OUTCOMES:
            # A crashed replacement: resume the epoch already prepared rather
            # than burn another one.
            self._require_contract()
            return Worker.from_record(
                self._launch_prepared(task, str(task.spec["prompt"]))
            )
        if task.state not in {"active", "blocked"}:
            return None
        if effect is None:
            effect = self.journal.get(task.task_id, task.epoch)
        if (
            effect is None
            or effect.state != "terminal"
            or effect.outcome not in REROUTE_OUTCOMES
        ):
            return None

        route_id = target.get("route_id")
        route_class = target.get("route_class") or execution_routing.DEFAULT_ROUTE_CLASS
        if target.get("failover") == "pinned":
            # The operator pinned this task to one target and switched
            # automatic failover off. It blocks with the reason rather than
            # silently going nowhere, so `replace()` remains a human's call.
            return self._block_unroutable(
                task,
                f"{effect.outcome} on {route_id}; automatic failover is off",
            )

        selection = advance_route_after_outcome(
            execution_routing.ExecutionRoutingStore().load(),
            outcome=str(effect.outcome),
            route_class=route_class,
            failed_binding_id=target.get("auth_binding_id"),
        )
        if selection.status != "resolved" or selection.target is None:
            return self._block_unroutable(
                task,
                f"{effect.outcome} on route {route_id} "
                f"({route_class}); {selection.reason}",
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
            "recovery": effect.outcome,
        }
        self._require_contract()
        prepared = self.store.prepare_replacement(
            task.task_id, target=next_target, expect_version=task.version
        )
        return Worker.from_record(
            self._launch_prepared(prepared, str(task.spec["prompt"]))
        )

    def _block_unroutable(self, task: TaskRecord, reason: str) -> Worker | None:
        """Block a task that cannot advance, and report only a real change.

        A task blocked at launch already carries its typed outcome as its
        result, and a terminal effect stays in the sweep's view forever - so
        re-blocking it every tick would report the same non-event over and
        over. The reason is recorded when the task still had somewhere to move
        from; after that, silence is accurate.
        """
        if task.state == "blocked":
            return None
        return Worker.from_record(self._finish(task, "blocked", reason))

    def _record_provider_outcome(
        self, task: TaskRecord, outcome: str, retry_at: float | None
    ) -> str:
        """Mark this epoch terminal and cool what the outcome condemns, once.

        The cooldown belongs with the write that records the outcome, not with
        the reroute that later reads it: the first terminal write wins, so an
        account is cooled exactly once however many times reconcile re-reads
        the same outcome.
        """
        self.journal.mark_terminal(task.task_id, task.epoch, outcome, retry_at)
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
        return outcome

    def _launch_prepared(
        self, prepared: TaskRecord, replacement_prompt: str
    ) -> TaskRecord:
        """Fill the epoch ``prepare_replacement`` opened.

        A task that already holds a session hands ownership to its successor.
        One whose launch was rejected before it ever activated has no session
        to transfer and no workspace to reuse, so its new epoch opens a
        workspace exactly as the first attempt would have.
        """
        if prepared.workspace_id and prepared.holder_session_id:
            return self._replace_prepared(prepared, replacement_prompt)
        workspace_id, session_id = self._journaled_launch(
            prepared,
            {
                "kind": "workspace",
                "request": self._workspace_request(prepared, replacement_prompt),
            },
        )
        active = self.store.activate(
            prepared.task_id, workspace_id=workspace_id, session_id=session_id
        )
        self._record_launcher(active)
        return active

    def _replace_prepared(
        self, prepared: TaskRecord, replacement_prompt: str
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
            return self._journaled_launch(prepared, launch)[1]

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
        if task.state != "reserved":
            return task
        request = self._workspace_request(task, str(task.spec["prompt"]))
        self._wait_for_launch_capacity(task)
        workspace_id, session_id = self._journaled_launch(
            task, {"kind": "workspace", "request": request}
        )
        active = self.store.activate(
            task.task_id, workspace_id=workspace_id, session_id=session_id
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
        deadline = time.monotonic() + float(os.environ.get("SIGHTMESH_LAUNCH_WAIT_SECONDS", "90"))
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
        self, task: TaskRecord, launch: Mapping[str, Any]
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
                    task.task_id, task.epoch, request_hash(launch), self.owner_instance
                )
                break
            except EffectBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        if effect.state == "launched" and effect.workspace_id and effect.session_id:
            return effect.workspace_id, effect.session_id
        try:
            native = self.client.managed_launch(task.task_id, task.epoch, dict(launch))
        except CdesktopRejectedError as exc:
            # 424/425 mean the outcome is unknowable or still owned; the
            # reservation must stay adoptable for a retry. Anything else is a
            # definitive rejection: record the typed outcome on this epoch's
            # effect so callers never have to grep error text, and block the
            # task so it is not left `reserved` and relaunchable - a retry is an
            # explicit new epoch via replace().
            if not isinstance(exc, (CdesktopInterruptedError, CdesktopPendingError)):
                outcome = self._record_provider_outcome(
                    task, _rejection_outcome(exc.status), exc.retry_at
                )
                self._finish(task, "blocked", f"launch rejected: {outcome}")
            raise
        workspace_id, session_id = self._effect_ids(task, native)
        self.journal.mark_launched(task.task_id, task.epoch, workspace_id, session_id)
        return workspace_id, session_id

    def _effect_ids(
        self, task: TaskRecord, effect: Mapping[str, Any]
    ) -> tuple[str, str]:
        state = str(effect.get("state") or "")
        if state == "lost":
            reason = str(effect.get("reason") or "lost")
            self.journal.mark_terminal(task.task_id, task.epoch, f"lost:{reason}")
            self._finish(task, "lost", reason)
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
                permission=spec.permission,
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
        if not decision.validation.valid:
            # The explicit fail-closed gate: no epoch, no effect row, and no
            # native call happen for a class that has nowhere to run. Reached
            # only when no class is usable, since a promoted class that is
            # empty was already demoted onto the default chain above.
            raise SightMeshError(
                f"Route class {route_class!r} cannot start {spec.key!r}: "
                f"{decision.validation.reason}"
            )
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

    Three names advance a route chain: capacity, auth, and a provider that is
    down. Everything else stays a definitive rejection that blocks, because
    retrying it on another account would only fail the same way.
    """
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if status is not None and 500 <= status < 600:
        # A 5xx is the provider failing, not this account.
        return "provider_down"
    return f"rejected:{status if status is not None else 'unknown'}"
