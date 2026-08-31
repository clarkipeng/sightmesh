from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as websocket_connect

from .service import DEFAULT_PORT, is_healthy, service_url


class CdesktopError(RuntimeError):
    pass


class CdesktopRejectedError(CdesktopError):
    """A cdesktop server response definitively rejected the requested action."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CdesktopNotFoundError(CdesktopRejectedError):
    """A versioned cdesktop resource does not exist (HTTP 404)."""


class CdesktopInterruptedError(CdesktopRejectedError):
    """cdesktop cannot establish whether the keyed side effect ran (HTTP 424)."""


class CdesktopPendingError(CdesktopRejectedError):
    """A keyed operation is still owned by another cdesktop request (HTTP 425)."""


TASK_LAUNCH_CONTRACT_VERSION = 1
TASK_LAUNCH_OUTCOMES = {
    "completed",
    "failed",
    "quota_exhausted",
    "approval_timeout",
    "storage_refused",
    "lost",
}


@dataclass(frozen=True)
class TaskLaunchResult:
    task_id: str
    incarnation_generation: int
    attempt_id: str
    idempotency_key: str
    launch_fingerprint: str
    phase: str
    effect: str
    workspace_id: str | None
    session_id: str | None
    outcome: dict[str, Any] | None
    history_ref: str | None


class CdesktopClient:
    def __init__(self, base_url: str | None = None) -> None:
        configured = base_url or os.environ.get("SIGHTMESH_CDESKTOP_URL")
        self.base_url = (configured or self._discover_url()).rstrip("/")

    @staticmethod
    def _discover_url() -> str:
        if is_healthy(DEFAULT_PORT):
            return service_url(DEFAULT_PORT)
        port_file = Path(tempfile.gettempdir()) / "cdesktop" / "cdesktop.port"
        try:
            raw = port_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CdesktopError(
                f"Cannot read {port_file}. Start cdesktop or set "
                "SIGHTMESH_CDESKTOP_URL."
            ) from exc

        try:
            parsed = json.loads(raw)
            port = parsed["main_port"] if isinstance(parsed, dict) else int(parsed)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CdesktopError(f"Invalid cdesktop port file: {raw!r}") from exc
        return f"http://127.0.0.1:{int(port)}"

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}/api{path}"
        if query:
            encoded_query = {
                key: str(value).lower() if isinstance(value, bool) else value
                for key, value in query.items()
            }
            url = f"{url}?{urlencode(encoded_query)}"
        body = None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error_type = {
                404: CdesktopNotFoundError,
                409: CdesktopRejectedError,
                424: CdesktopInterruptedError,
                425: CdesktopPendingError,
            }.get(exc.code)
            message = f"{method} {path} failed: HTTP {exc.code}: {detail}"
            if error_type is None:
                raise CdesktopError(message) from exc
            raise error_type(message, status=exc.code) from exc
        except URLError as exc:
            raise CdesktopError(
                f"Cannot reach cdesktop at {self.base_url}: {exc}"
            ) from exc

        data = json.loads(raw) if raw else None
        if isinstance(data, dict) and data.get("success") is False:
            raise CdesktopRejectedError(str(data.get("message") or data))
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def info(self) -> dict[str, Any]:
        return self.request("GET", "/info")

    def require_task_launch_contract(self) -> None:
        """Fail closed unless cdesktop advertises the complete v1 contract."""
        info = self.info()
        capabilities = info.get("capabilities") if isinstance(info, dict) else None
        capability = (
            capabilities.get("task_launch")
            if isinstance(capabilities, dict)
            else None
        )
        required = {
            "create_or_return",
            "lookup",
            "typed_outcomes",
            "content_addressed_history",
        }
        limits = {"transcript_bytes", "fork_bytes", "free_disk_bytes"}
        features = (
            set(capability.get("features", ()))
            if isinstance(capability, dict)
            else set()
        )
        writer_limits = (
            set(capability.get("writer_limits", ()))
            if isinstance(capability, dict)
            else set()
        )
        if (
            not isinstance(capability, dict)
            or capability.get("contract_version") != TASK_LAUNCH_CONTRACT_VERSION
            or not required <= features
            or not limits <= writer_limits
        ):
            raise CdesktopRejectedError(
                "Pinned cdesktop does not advertise the complete task-launch-v1 "
                "idempotency and writer-quota contract; refusing task-id launch"
            )

    def lookup_task_launch(self, idempotency_key: str) -> TaskLaunchResult | None:
        self.require_task_launch_contract()
        try:
            result = self.request(
                "GET",
                "/task-launches/by-key",
                query={"idempotency_key": idempotency_key},
            )
        except CdesktopNotFoundError:
            return None
        return _task_launch_result(result, expected_key=idempotency_key)

    def create_or_return_task_launch(
        self,
        *,
        task_id: str,
        incarnation_generation: int,
        attempt_id: str,
        idempotency_key: str,
        launch: dict[str, Any],
    ) -> TaskLaunchResult:
        """Lookup first, then atomically create-or-return one keyed side effect."""
        launch_fingerprint = hashlib.sha256(
            json.dumps(launch, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing = self.lookup_task_launch(idempotency_key)
        if existing is not None:
            _match_task_launch(
                existing,
                task_id=task_id,
                incarnation_generation=incarnation_generation,
                attempt_id=attempt_id,
                launch_fingerprint=launch_fingerprint,
            )
            return existing
        result = self.request(
            "POST",
            "/task-launches",
            {
                "contract_version": TASK_LAUNCH_CONTRACT_VERSION,
                "task_id": task_id,
                "incarnation_generation": incarnation_generation,
                "attempt_id": attempt_id,
                "idempotency_key": idempotency_key,
                "launch_fingerprint": launch_fingerprint,
                "launch": launch,
            },
        )
        parsed = _task_launch_result(result, expected_key=idempotency_key)
        _match_task_launch(
            parsed,
            task_id=task_id,
            incarnation_generation=incarnation_generation,
            attempt_id=attempt_id,
            launch_fingerprint=launch_fingerprint,
        )
        return parsed

    def set_update_drain(self, seconds: int) -> dict[str, Any]:
        if not 0 <= seconds <= 30:
            raise ValueError("Update drain seconds must be between 0 and 30")
        result = self.request("POST", "/maintenance/drain", {"seconds": seconds})
        if not isinstance(result, dict):
            raise CdesktopError("cdesktop update drain response is invalid")
        return result

    def configure_local(self, workspace_root: Path) -> dict[str, Any]:
        info = self.info()
        config = dict(info["config"])
        config.update(
            {
                "analytics_enabled": False,
                "relay_enabled": False,
                "workspace_dir": str(workspace_root.expanduser().resolve()),
                "disclaimer_acknowledged": True,
                "onboarding_acknowledged": True,
            }
        )
        return self.request("PUT", "/config", config)

    def repos(self) -> list[dict[str, Any]]:
        return self.request("GET", "/repos")

    def providers(self) -> list[dict[str, Any]]:
        return self.request("GET", "/providers")

    def register_repo(
        self,
        path: Path,
        display_name: str | None = None,
        *,
        setup_script: str | None = None,
        configure_setup: bool = False,
    ) -> dict[str, Any]:
        resolved = path.expanduser().resolve()
        for repo in self.repos():
            if Path(repo["path"]).expanduser().resolve() == resolved:
                if configure_setup:
                    return self.request(
                        "PUT",
                        f"/repos/{repo['id']}",
                        {
                            "setup_script": setup_script,
                            "parallel_setup_script": False,
                        },
                    )
                return repo
        payload: dict[str, Any] = {
            "path": str(resolved),
            "display_name": display_name or resolved.name,
        }
        if configure_setup:
            payload.update(
                {
                    "setup_script": setup_script,
                    "parallel_setup_script": False,
                }
            )
        return self.request(
            "POST",
            "/repos",
            payload,
        )

    def create_workspace_record(
        self, name: str, *, use_worktree: bool
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/workspaces",
            {"name": name, "use_worktree": use_worktree},
        )

    def add_workspace_repo(
        self,
        workspace_id: str,
        repo_path: Path,
        target_branch: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        repo = self.register_repo(repo_path, display_name)
        return self.request(
            "POST",
            f"/workspaces/{workspace_id}/repos",
            {"repo_id": repo["id"], "target_branch": target_branch},
        )

    def create_session_record(
        self, workspace_id: str, *, executor: str | None, name: str | None
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/sessions",
            {"workspace_id": workspace_id, "executor": executor, "name": name},
        )

    def set_parent(self, session_id: str, parent_session_id: str) -> dict[str, Any]:
        if str(session_id) == str(parent_session_id):
            raise ValueError(f"Session {session_id} cannot be its own parent")
        return self.request(
            "PUT",
            f"/sessions/{session_id}",
            {"name": None, "parent_session_id": parent_session_id},
        )

    def workspaces(self) -> list[dict[str, Any]]:
        return self.request("GET", "/workspaces")

    def workspace(self, workspace_id: str) -> dict[str, Any]:
        return self.request("GET", f"/workspaces/{workspace_id}")

    def sessions(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.request("GET", "/sessions", query={"workspace_id": workspace_id})

    def session(self, session_id: str) -> dict[str, Any]:
        return self.request("GET", f"/sessions/{session_id}")

    def execution_process(self, execution_process_id: str) -> dict[str, Any]:
        return self.request("GET", f"/execution-processes/{execution_process_id}")

    def execution_processes(self, session_id: str) -> list[dict[str, Any]]:
        result = self.request(
            "GET", "/execution-processes", query={"session_id": session_id}
        )
        if not isinstance(result, list):
            raise CdesktopError("cdesktop execution process response is not a list")
        return [dict(item) for item in result if isinstance(item, dict)]

    def queue_status(self, session_id: str) -> dict[str, Any]:
        result = self.request("GET", f"/sessions/{session_id}/queue")
        if not isinstance(result, dict):
            raise CdesktopError("cdesktop queue status response is invalid")
        return result

    def probe_connectivity(self) -> bool:
        """Bounded, side-effect-free gate used before native dispatch."""
        try:
            with urlopen(f"{self.base_url}/api/health", timeout=1) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    def session_commands(self, session_id: str) -> list[dict[str, Any]]:
        result = self.request("GET", f"/sessions/{session_id}/commands")
        if not isinstance(result, list):
            raise CdesktopError("cdesktop command response is not a list")
        return [dict(item) for item in result if isinstance(item, dict)]

    def requeue_execution_commands(
        self, session_id: str, execution_process_id: str
    ) -> Any:
        """Return one dead execution's native command rows to the queue."""
        return self.request(
            "POST",
            f"/sessions/{session_id}/commands/requeue",
            {"execution_process_id": execution_process_id},
        )

    def dispatch_queued(self, session_id: str) -> Any:
        return self.request("POST", f"/sessions/{session_id}/commands/dispatch")

    def normalized_snapshot(self, execution_process_id: str) -> dict[str, Any]:
        result = self.request(
            "GET",
            f"/execution-processes/{execution_process_id}/normalized-snapshot",
        )
        if not isinstance(result, dict):
            raise CdesktopError("cdesktop normalized snapshot response is invalid")
        return result

    def stop_execution(
        self, execution_process_id: str, *, dedupe_key: str | None = None
    ) -> Any:
        """Stop one execution through cdesktop's process-scoped dedupe contract."""
        return self.request(
            "POST",
            f"/execution-processes/{execution_process_id}/stop",
            {"dedupe_key": dedupe_key} if dedupe_key else {},
        )

    def wait_for_session_idle(
        self,
        session_id: str,
        *,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.1,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        latest: list[dict[str, Any]] = []
        while time.monotonic() <= deadline:
            latest = self.execution_processes(session_id)
            running = [
                process
                for process in latest
                if process.get("status") == "running"
                and process.get("run_reason") != "devserver"
            ]
            if not running:
                return latest
            time.sleep(poll_seconds)
        raise CdesktopError(
            f"Session {session_id} did not stop within {timeout_seconds:g} seconds; "
            "the steering follow-up was not sent"
        )

    def pending_approvals(
        self, *, timeout_seconds: float = 5.0
    ) -> list[dict[str, Any]]:
        try:
            result = self.request("GET", "/approvals")
        except (CdesktopError, json.JSONDecodeError):
            return self._pending_approvals_websocket(timeout_seconds=timeout_seconds)
        if not isinstance(result, list):
            raise CdesktopError("cdesktop pending approval response is not a list")
        return [dict(item) for item in result if isinstance(item, dict)]

    def _pending_approvals_websocket(
        self, *, timeout_seconds: float
    ) -> list[dict[str, Any]]:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise CdesktopError(
                f"Unsupported cdesktop URL scheme for approvals: {parsed.scheme!r}"
            )
        websocket_url = urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                f"{parsed.path.rstrip('/')}/api/approvals/stream/ws",
                "",
                "",
            )
        )
        pending: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout_seconds
        try:
            with websocket_connect(
                websocket_url,
                open_timeout=timeout_seconds,
                close_timeout=1,
            ) as socket:
                while time.monotonic() < deadline:
                    remaining = max(0.01, deadline - time.monotonic())
                    raw = socket.recv(timeout=remaining)
                    message = json.loads(raw)
                    patches = message.get("JsonPatch")
                    if isinstance(patches, list):
                        pending = _apply_approval_patches(pending, patches)
                    if message.get("Ready") is True:
                        return sorted(
                            pending.values(),
                            key=lambda item: (
                                str(item.get("created_at") or ""),
                                str(item.get("approval_id") or ""),
                            ),
                        )
        except (OSError, TimeoutError, WebSocketException, json.JSONDecodeError) as exc:
            raise CdesktopError(
                f"Cannot read cdesktop approval stream at {websocket_url}: {exc}"
            ) from exc
        raise CdesktopError(
            f"cdesktop approval stream did not become ready within {timeout_seconds:g} seconds"
        )

    def respond_to_approval(
        self,
        approval_id: str,
        execution_process_id: str,
        *,
        approved: bool,
        reason: str | None = None,
    ) -> Any:
        status: dict[str, Any]
        if approved:
            status = {"status": "approved"}
        else:
            status = {
                "status": "denied",
                "reason": reason or "Reviewer denied this request.",
            }
        return self.request(
            "POST",
            f"/approvals/{approval_id}/respond",
            {
                "execution_process_id": execution_process_id,
                "status": status,
            },
        )

    def respond_to_question(
        self,
        approval_id: str,
        execution_process_id: str,
        answers: list[dict[str, Any]],
    ) -> Any:
        return self.request(
            "POST",
            f"/approvals/{approval_id}/respond",
            {
                "execution_process_id": execution_process_id,
                "status": {"status": "answered", "answers": answers},
            },
        )

    def workspace_summaries(self, archived: bool = False) -> list[dict[str, Any]]:
        result = self.request("POST", "/workspaces/summaries", {"archived": archived})
        summaries = result.get("summaries") if isinstance(result, dict) else None
        return summaries if isinstance(summaries, list) else []

    def workspace_repos(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/workspaces/{workspace_id}/repos")

    def _git_repository_paths(self, workspace_id: str) -> list[Path]:
        """The on-disk path cdesktop uses for each Git repository, existing or not.

        A managed worktree with no container has no directory of its own, so it
        contributes no path at all.
        """
        workspace = self.workspace(workspace_id)
        paths: list[Path] = []
        for repo in self.workspace_repos(workspace_id):
            if not repo.get("is_git"):
                continue
            if workspace.get("use_worktree"):
                container = workspace.get("container_ref")
                if not container:
                    continue
                paths.append(Path(container) / repo["name"])
            else:
                paths.append(Path(repo["path"]))
        return paths

    def dirty_repositories(self, workspace_id: str) -> list[dict[str, str]]:
        """Repositories holding uncommitted work that archiving would risk.

        A directory that does not exist holds no uncommitted work, so it is
        never dirty; ``missing_repositories`` reports it instead. That keeps the
        guard strict for real files while leaving an already reconciled
        workspace removable.
        """
        dirty: list[dict[str, str]] = []
        for path in self._git_repository_paths(workspace_id):
            if not path.is_dir():
                continue
            result = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=path,
                capture_output=True,
                text=True,
                check=False,
            )
            status = result.stdout.strip()
            if result.returncode != 0:
                status = (result.stderr or "git status failed").strip()
            if status:
                dirty.append({"path": str(path), "status": status})
        return dirty

    def missing_repositories(self, workspace_id: str) -> list[dict[str, str]]:
        """Repository paths cdesktop expects that are no longer on disk."""
        return [
            {"path": str(path), "status": "repository path is missing"}
            for path in self._git_repository_paths(workspace_id)
            if not path.is_dir()
        ]

    def spawn_workspace(
        self,
        *,
        name: str,
        repo_path: Path,
        target_branch: str,
        executor: str,
        prompt: str,
        use_worktree: bool,
        permission_policy: str,
        model: str | None,
        reasoning: str | None,
        provider_id: str | None,
        setup_script: str | None = None,
        auth_binding_id: str | None = None,
        launch_key: str | None = None,
    ) -> dict[str, Any]:
        known_workspace_ids = {str(item.get("id")) for item in self.workspaces()}
        payload = self.workspace_launch_spec(
            name=name,
            repo_path=repo_path,
            target_branch=target_branch,
            executor=executor,
            prompt=prompt,
            use_worktree=use_worktree,
            permission_policy=permission_policy,
            model=model,
            reasoning=reasoning,
            provider_id=provider_id,
            setup_script=setup_script,
            auth_binding_id=auth_binding_id,
        )
        if launch_key:
            payload["idempotency_key"] = launch_key
        try:
            return self.request("POST", "/workspaces/start", payload)
        except CdesktopError as exc:
            if launch_key:
                raise
            cleanup = self._cleanup_failed_workspace_start(name, known_workspace_ids)
            if cleanup:
                raise CdesktopError(f"{exc}; {cleanup}") from exc
            raise

    def workspace_launch_spec(
        self,
        *,
        name: str,
        repo_path: Path,
        target_branch: str,
        executor: str,
        prompt: str,
        use_worktree: bool,
        permission_policy: str,
        model: str | None,
        reasoning: str | None,
        provider_id: str | None,
        setup_script: str | None = None,
        auth_binding_id: str | None = None,
    ) -> dict[str, Any]:
        repo = self.register_repo(
            repo_path,
            setup_script=setup_script,
            configure_setup=use_worktree and setup_script is not None,
        )
        executor_config: dict[str, Any] = {
            "executor": executor,
            "permission_policy": permission_policy,
        }
        if model:
            executor_config["model_id"] = model
        if reasoning:
            executor_config["reasoning_id"] = reasoning
        payload: dict[str, Any] = {
            "name": name,
            "repos": [{"repo_id": repo["id"], "target_branch": target_branch}],
            "linked_issue": None,
            "executor_config": executor_config,
            "prompt": prompt,
            "attachment_ids": None,
            "use_worktree": use_worktree,
            "selected_provider_id": provider_id,
        }
        if auth_binding_id:
            # Opaque pool binding id per the SessionCommandConfig contract;
            # resolution to a credential happens inside cdesktop at launch.
            payload["auth_binding_id"] = auth_binding_id
        return payload

    def _cleanup_failed_workspace_start(
        self, name: str, known_workspace_ids: set[str]
    ) -> str | None:
        """Delete only the unambiguous native workspace left by a failed start."""
        try:
            candidates = [
                item
                for item in self.workspaces()
                if str(item.get("id")) not in known_workspace_ids
                and item.get("name") == name
            ]
        except CdesktopError as exc:
            return f"could not inspect partial workspace for cleanup: {exc}"
        if len(candidates) != 1:
            if candidates:
                return "left partial workspace untouched because cleanup was ambiguous"
            return None
        workspace_id = str(candidates[0]["id"])
        try:
            self.delete_workspace(workspace_id)
        except CdesktopError as exc:
            return f"partial workspace {workspace_id} was not cleaned up: {exc}"
        return f"native cleanup deleted partial workspace {workspace_id}"

    def send(
        self,
        session_id: str,
        prompt: str,
        sender_session: str | None = None,
        *,
        dedupe_key: str | None = None,
        intent: str = "continue",
    ) -> Any:
        headers = {}
        if sender_session:
            headers["x-cdesktop-from-session"] = sender_session
        payload: dict[str, Any] = {"prompt": prompt}
        if dedupe_key:
            payload["dedupe_key"] = dedupe_key
        payload["intent"] = intent
        return self.request(
            "POST",
            f"/sessions/{session_id}/follow-up",
            payload,
            headers=headers,
        )

    def wait_for_workspace_idle(
        self,
        workspace_id: str,
        *,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() <= deadline:
            last = next(
                (
                    item
                    for item in self.workspace_summaries(False)
                    if item.get("workspace_id") == workspace_id
                ),
                None,
            )
            if last is None:
                raise CdesktopError(
                    f"Workspace {workspace_id} is not active in cdesktop"
                )
            if last.get("latest_process_status") != "running":
                return last
            time.sleep(poll_seconds)
        raise CdesktopError(
            f"Workspace {workspace_id} did not stop within {timeout_seconds:g} seconds; "
            "the follow-up was not sent"
        )

    def spawn_teammate(
        self,
        *,
        caller_session: str,
        name: str,
        prompt: str,
        executor: str | None = None,
        permission_policy: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        provider_id: str | None = None,
        auth_binding_id: str | None = None,
        launch_key: str | None = None,
    ) -> dict[str, Any]:
        payload = self.teammate_launch_spec(
            caller_session=caller_session,
            name=name,
            prompt=prompt,
            executor=executor,
            permission_policy=permission_policy,
            model=model,
            reasoning=reasoning,
            provider_id=provider_id,
            auth_binding_id=auth_binding_id,
        )
        payload.pop("caller_session_id", None)
        if launch_key:
            payload["idempotency_key"] = launch_key
        return self.request("POST", f"/sessions/{caller_session}/teammates", payload)

    def teammate_launch_spec(
        self,
        *,
        caller_session: str,
        name: str,
        prompt: str,
        executor: str | None = None,
        permission_policy: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        provider_id: str | None = None,
        auth_binding_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "prompt": prompt}
        config: dict[str, Any] = {}
        if executor:
            config["executor"] = executor
        if permission_policy:
            config["permission_policy"] = permission_policy
        if model:
            config["model_id"] = model
        if reasoning:
            config["reasoning_id"] = reasoning
        if config:
            payload["executor_config"] = config
        if provider_id:
            payload["selected_provider_id"] = provider_id
        if auth_binding_id:
            payload["auth_binding_id"] = auth_binding_id
        payload["caller_session_id"] = caller_session
        return payload

    def stop_workspace(self, workspace_id: str) -> Any:
        return self.request("POST", f"/workspaces/{workspace_id}/execution/stop", {})

    def archive_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self.set_workspace_archived(workspace_id, True)

    def restore_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self.set_workspace_archived(workspace_id, False)

    def set_workspace_archived(
        self, workspace_id: str, archived: bool
    ) -> dict[str, Any]:
        return self.request(
            "PUT",
            f"/workspaces/{workspace_id}",
            {"archived": archived, "pinned": None, "name": None},
        )

    def rename_workspace(self, workspace_id: str, name: str) -> dict[str, Any]:
        return self.request(
            "PUT",
            f"/workspaces/{workspace_id}",
            {"archived": None, "pinned": None, "name": name},
        )

    def delete_workspace(self, workspace_id: str) -> Any:
        return self.request(
            "DELETE",
            f"/workspaces/{workspace_id}",
            query={"delete_remote": False, "delete_branches": False},
        )


def _task_launch_result(value: Any, *, expected_key: str) -> TaskLaunchResult:
    if not isinstance(value, dict) or value.get("contract_version") != 1:
        raise CdesktopError("cdesktop task launch response is not contract version 1")
    required = {
        "task_id": str,
        "incarnation_generation": int,
        "attempt_id": str,
        "idempotency_key": str,
        "launch_fingerprint": str,
        "phase": str,
        "effect": str,
    }
    if any(not isinstance(value.get(field), kind) for field, kind in required.items()):
        raise CdesktopError("cdesktop task launch response has invalid identity fields")
    if value["idempotency_key"] != expected_key:
        raise CdesktopError("cdesktop task launch response changed the idempotency key")
    fingerprint = value["launch_fingerprint"]
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise CdesktopError("cdesktop task launch response has an invalid fingerprint")
    if value["phase"] not in {"pending", "active", "terminal", "refused"}:
        raise CdesktopError("cdesktop task launch response has an invalid phase")
    if value["effect"] not in {"created", "existing", "none"}:
        raise CdesktopError("cdesktop task launch response has an invalid effect")

    outcome = value.get("outcome")
    if outcome is not None:
        if not isinstance(outcome, dict) or outcome.get("kind") not in TASK_LAUNCH_OUTCOMES:
            raise CdesktopError("cdesktop task launch response has an invalid outcome")
        for field in ("provider_id", "account_id"):
            if outcome.get(field) is not None and not isinstance(outcome[field], str):
                raise CdesktopError(f"cdesktop task launch outcome has invalid {field}")
        if outcome.get("retry_at") is not None and not isinstance(
            outcome["retry_at"], (int, float)
        ):
            raise CdesktopError("cdesktop task launch outcome has invalid retry_at")
        if outcome["kind"] == "storage_refused" and outcome.get(
            "refused_before_write"
        ) is not True:
            raise CdesktopError(
                "cdesktop storage refusal does not prove pre-write enforcement"
            )

    history_ref = value.get("history_ref")
    if history_ref is not None:
        digest = history_ref.removeprefix("sha256:") if isinstance(history_ref, str) else ""
        if (
            not isinstance(history_ref, str)
            or not history_ref.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CdesktopError(
                "cdesktop task launch history_ref is not content-addressed"
            )

    for field in ("workspace_id", "session_id"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise CdesktopError(f"cdesktop task launch response has invalid {field}")
    if value["phase"] == "active" and not all(
        isinstance(value.get(field), str) and value[field]
        for field in ("workspace_id", "session_id")
    ):
        raise CdesktopError("active cdesktop task launch is missing native identities")
    if value["phase"] in {"terminal", "refused"} and outcome is None:
        raise CdesktopError("terminal cdesktop task launch is missing its typed outcome")

    return TaskLaunchResult(
        task_id=value["task_id"],
        incarnation_generation=value["incarnation_generation"],
        attempt_id=value["attempt_id"],
        idempotency_key=value["idempotency_key"],
        launch_fingerprint=value["launch_fingerprint"],
        phase=value["phase"],
        effect=value["effect"],
        workspace_id=value.get("workspace_id"),
        session_id=value.get("session_id"),
        outcome=dict(outcome) if outcome is not None else None,
        history_ref=history_ref,
    )


def _match_task_launch(
    result: TaskLaunchResult,
    *,
    task_id: str,
    incarnation_generation: int,
    attempt_id: str,
    launch_fingerprint: str,
) -> None:
    if (
        result.task_id != task_id
        or result.incarnation_generation != incarnation_generation
        or result.attempt_id != attempt_id
        or result.launch_fingerprint != launch_fingerprint
    ):
        raise CdesktopRejectedError(
            "Existing cdesktop idempotency key belongs to different launch parameters"
        )


def _apply_approval_patches(
    pending: dict[str, dict[str, Any]], patches: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    updated = dict(pending)
    for patch in patches:
        operation = patch.get("op")
        path = patch.get("path")
        if path == "/pending" and operation in {"add", "replace"}:
            value = patch.get("value")
            if not isinstance(value, dict):
                raise CdesktopError(
                    "cdesktop approval snapshot has invalid pending data"
                )
            updated = {
                str(key): dict(item)
                for key, item in value.items()
                if isinstance(item, dict)
            }
            continue
        if not isinstance(path, str) or not path.startswith("/pending/"):
            continue
        approval_id = (
            path.removeprefix("/pending/").replace("~1", "/").replace("~0", "~")
        )
        if operation == "remove":
            updated.pop(approval_id, None)
        elif operation in {"add", "replace"} and isinstance(patch.get("value"), dict):
            updated[approval_id] = dict(patch["value"])
    return updated
