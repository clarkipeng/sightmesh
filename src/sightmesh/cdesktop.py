from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
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
            raise CdesktopError(
                f"{method} {path} failed: HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise CdesktopError(
                f"Cannot reach cdesktop at {self.base_url}: {exc}"
            ) from exc

        data = json.loads(raw) if raw else None
        if isinstance(data, dict) and data.get("success") is False:
            raise CdesktopError(str(data.get("message") or data))
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def info(self) -> dict[str, Any]:
        return self.request("GET", "/info")

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
        self, path: Path, display_name: str | None = None
    ) -> dict[str, Any]:
        resolved = path.expanduser().resolve()
        for repo in self.repos():
            if Path(repo["path"]).expanduser().resolve() == resolved:
                return repo
        return self.request(
            "POST",
            "/repos",
            {"path": str(resolved), "display_name": display_name or resolved.name},
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

    def normalized_snapshot(self, execution_process_id: str) -> dict[str, Any]:
        result = self.request(
            "GET",
            f"/execution-processes/{execution_process_id}/normalized-snapshot",
        )
        if not isinstance(result, dict):
            raise CdesktopError("cdesktop normalized snapshot response is invalid")
        return result

    def stop_execution(self, execution_process_id: str) -> Any:
        return self.request(
            "POST", f"/execution-processes/{execution_process_id}/stop", {}
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

    def workspace_summaries(self, archived: bool = False) -> list[dict[str, Any]]:
        result = self.request("POST", "/workspaces/summaries", {"archived": archived})
        summaries = result.get("summaries") if isinstance(result, dict) else None
        return summaries if isinstance(summaries, list) else []

    def workspace_repos(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/workspaces/{workspace_id}/repos")

    def dirty_repositories(self, workspace_id: str) -> list[dict[str, str]]:
        workspace = self.workspace(workspace_id)
        dirty: list[dict[str, str]] = []
        for repo in self.workspace_repos(workspace_id):
            if not repo.get("is_git"):
                continue
            if workspace.get("use_worktree"):
                container = workspace.get("container_ref")
                if not container:
                    if workspace.get("archived"):
                        continue
                    dirty.append({"path": "", "status": "missing container_ref"})
                    continue
                path = Path(container) / repo["name"]
            else:
                path = Path(repo["path"])
            if not path.is_dir():
                if workspace.get("archived") and workspace.get("use_worktree"):
                    continue
                dirty.append(
                    {"path": str(path), "status": "repository path is missing"}
                )
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
    ) -> dict[str, Any]:
        repo = self.register_repo(repo_path)
        executor_config: dict[str, Any] = {
            "executor": executor,
            "permission_policy": permission_policy,
        }
        if model:
            executor_config["model_id"] = model
        if reasoning:
            executor_config["reasoning_id"] = reasoning
        return self.request(
            "POST",
            "/workspaces/start",
            {
                "name": name,
                "repos": [{"repo_id": repo["id"], "target_branch": target_branch}],
                "linked_issue": None,
                "executor_config": executor_config,
                "prompt": prompt,
                "attachment_ids": None,
                "use_worktree": use_worktree,
                "selected_provider_id": provider_id,
            },
        )

    def send(
        self, session_id: str, prompt: str, sender_session: str | None = None
    ) -> Any:
        headers = {}
        if sender_session:
            headers["x-cdesktop-from-session"] = sender_session
        return self.request(
            "POST",
            f"/sessions/{session_id}/follow-up",
            {"prompt": prompt},
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
        return self.request("POST", f"/sessions/{caller_session}/teammates", payload)

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
