from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .service import DEFAULT_PORT, is_healthy, service_url


class CdesktopError(RuntimeError):
    pass


class CdesktopClient:
    def __init__(self, base_url: str | None = None) -> None:
        configured = base_url or os.environ.get("AGENT_DECK_CDESKTOP_URL")
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
                "AGENT_DECK_CDESKTOP_URL."
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
            url = f"{url}?{urlencode(query)}"
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
            raise CdesktopError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise CdesktopError(f"Cannot reach cdesktop at {self.base_url}: {exc}") from exc

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

    def register_repo(self, path: Path, display_name: str | None = None) -> dict[str, Any]:
        resolved = path.expanduser().resolve()
        for repo in self.repos():
            if Path(repo["path"]).expanduser().resolve() == resolved:
                return repo
        return self.request(
            "POST",
            "/repos",
            {"path": str(resolved), "display_name": display_name or resolved.name},
        )

    def workspaces(self) -> list[dict[str, Any]]:
        return self.request("GET", "/workspaces")

    def workspace(self, workspace_id: str) -> dict[str, Any]:
        return self.request("GET", f"/workspaces/{workspace_id}")

    def sessions(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.request("GET", "/sessions", query={"workspace_id": workspace_id})

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
                    dirty.append({"path": "", "status": "missing container_ref"})
                    continue
                path = Path(container) / repo["name"]
            else:
                path = Path(repo["path"])
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

    def send(self, session_id: str, prompt: str, sender_session: str | None = None) -> Any:
        headers = {}
        if sender_session:
            headers["x-cdesktop-from-session"] = sender_session
        return self.request(
            "POST",
            f"/sessions/{session_id}/follow-up",
            {"prompt": prompt},
            headers=headers,
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
        return self.request(
            "PUT",
            f"/workspaces/{workspace_id}",
            {"archived": True, "pinned": None, "name": None},
        )
