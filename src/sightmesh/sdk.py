from __future__ import annotations

import hashlib
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

from . import execution_routing
from .cdesktop import CdesktopClient, CdesktopError
from .escalation import CDESKTOP_SESSION_ENV, EscalationStore, LauncherIdentity
from .profiles import ProfileStore, validate_provider
from .succession import OwnershipStore, transfer_ownership
from .task_store import TaskRecord, TaskStore, TaskStoreError

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
MAX_ATTEMPTS = 3
T = TypeVar("T")


class SightMeshError(RuntimeError):
    pass


class BatchError(SightMeshError):
    def __init__(self, message: str, result: BatchResult[Any]) -> None:
        self.result = result
        super().__init__(message)


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
    permission: str = "SUPERVISED"
    children: int = 0


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
        self._contract_checked = False

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
        prepared = [self._prepare_spec(scope, spec) for spec in requested]
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
        updated = self.store.finish(task.task_id, "completed", summary)
        self._notify_parent(updated, "completed", summary)
        return Worker.from_record(updated)

    def blocked(self, reason: str, worker: str | None = None) -> Worker:
        if not reason.strip():
            raise SightMeshError("Blocked reason must not be empty")
        task = self._current() if worker is None else self._find(worker)
        updated = self.store.finish(task.task_id, "blocked", reason)
        self._notify_parent(updated, "blocked", reason)
        return Worker.from_record(updated)

    def cancel(self, worker: str) -> Worker:
        task = self._find(worker)
        if task.workspace_id:
            self.client.stop_workspace(task.workspace_id)
        return Worker.from_record(self.store.finish(task.task_id, "cancelled"))

    def replace(self, worker: str, prompt: str | None = None) -> Worker:
        task = self._find(worker)
        if not task.workspace_id or not task.holder_session_id:
            raise SightMeshError(f"Task {worker!r} has no session to replace")
        replacement_prompt = prompt or self._read_checkpoint(task)
        if not replacement_prompt or not replacement_prompt.strip():
            raise SightMeshError("Replacement requires a prompt or saved checkpoint")
        self._require_contract()
        prepared = self.store.prepare_replacement(task.task_id)
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

        def spawn() -> str:
            effect = self.client.managed_launch(
                prepared.task_id,
                prepared.epoch,
                {
                    "kind": "session",
                    "workspace_id": prepared.workspace_id,
                    "caller_session_id": prepared.holder_session_id,
                    "request": request,
                },
            )
            return self._effect_ids(prepared, effect)[1]

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
        return Worker.from_record(active)

    def _start_reserved(self, task: TaskRecord) -> TaskRecord:
        if task.state != "reserved":
            return task
        target = task.spec["target"]
        request = self.client.workspace_launch_request(
            name=task.key,
            repo_path=Path(task.spec["repo_path"]),
            target_branch=task.spec["base"],
            executor=target["executor"],
            prompt=task.spec["prompt"],
            use_worktree=True,
            permission_policy=task.spec["permission"],
            model=target.get("model"),
            reasoning=target.get("reasoning"),
            provider_id=target.get("provider_id"),
            setup_script=task.spec.get("setup_script"),
            auth_binding_id=target.get("auth_binding_id"),
        )
        effect = self.client.managed_launch(
            task.task_id, task.epoch, {"kind": "workspace", "request": request}
        )
        workspace_id, session_id = self._effect_ids(task, effect)
        active = self.store.activate(
            task.task_id, workspace_id=workspace_id, session_id=session_id
        )
        self._record_launcher(active)
        return active

    def _effect_ids(
        self, task: TaskRecord, effect: Mapping[str, Any]
    ) -> tuple[str, str]:
        state = str(effect.get("state") or "")
        if state == "lost":
            self.store.finish(task.task_id, "lost", str(effect.get("reason") or "lost"))
            raise SightMeshError(f"Native launch for {task.key!r} was lost")
        workspace_id = effect.get("workspace_id")
        session_id = effect.get("session_id")
        if state != "active" or not workspace_id or not session_id:
            raise SightMeshError(
                f"Native launch for {task.key!r} is {state or 'invalid'}"
            )
        return str(workspace_id), str(session_id)

    def _prepare_spec(self, scope: str, requested: WorkerSpec) -> dict[str, Any]:
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
        }
        existing = self.store.get(scope, requested.key)
        if existing is not None:
            old_public = {
                key: value
                for key, value in existing.spec.items()
                if key not in {"target", "setup_script"}
            }
            if old_public != public:
                raise SightMeshError(
                    f"Task {requested.key!r} already exists with a different specification"
                )
            return existing.spec
        return {
            **public,
            "setup_script": self._setup_script(repo_path, requested.base),
            "target": self._select_target(requested),
        }

    def _select_target(self, spec: WorkerSpec) -> dict[str, Any]:
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
            }
        if spec.executor:
            return {
                "executor": spec.executor,
                "model": spec.model,
                "reasoning": spec.reasoning,
                "provider_id": None,
                "auth_binding_id": None,
            }
        settings = execution_routing.ExecutionRoutingStore().load()
        selection = execution_routing.select_route(settings, preferred_model=spec.model)
        if selection.status != "resolved" or selection.target is None:
            raise SightMeshError(
                f"Execution routing could not start {spec.key!r}: {selection.reason}"
            )
        target = selection.target
        return {
            "executor": target.executor,
            "model": target.model,
            "reasoning": spec.reasoning,
            "provider_id": None,
            "auth_binding_id": target.auth_binding_id,
            "route_id": target.route_id,
        }

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

    def _notify_parent(self, task: TaskRecord, state: str, detail: str | None) -> None:
        if not task.parent_task_id:
            return
        parent = self.store.get_by_id(task.parent_task_id)
        if (
            parent is None
            or not parent.holder_session_id
            or parent.holder_session_id == task.holder_session_id
        ):
            return
        message = f"{state.upper()}: {task.key}"
        if detail:
            message += f"\n{detail}"
        self.client.send(
            parent.holder_session_id,
            message,
            task.holder_session_id,
            dedupe_key=f"task-{state}:{task.task_id}",
            intent="continue" if state == "completed" else "replace",
        )

    def _require_contract(self) -> None:
        if self._contract_checked:
            return
        info = self.client.info()
        capabilities = info.get("service_capabilities", {})
        if int(capabilities.get("managed_task_launch", 0)) < 1:
            raise SightMeshError(
                "cdesktop does not support the managed task launch contract"
            )
        self._contract_checked = True

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
        if spec.permission not in {"SUPERVISED", "PLAN", "ACCEPT_EDITS"}:
            raise SightMeshError("Managed tasks require a supervised permission policy")

    @staticmethod
    def _reject_ephemeral(repo_path: Path) -> None:
        value = repo_path.as_posix()
        if "/conductor/workspaces/" in value or "/.cdesktop-workspaces/" in value:
            raise SightMeshError(
                f"Repository {repo_path} is an ephemeral worktree; register its canonical checkout"
            )

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
