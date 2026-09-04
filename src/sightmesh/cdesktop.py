from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as websocket_connect

from .service import DEFAULT_PORT, is_healthy, service_url
from .fence import open_transport


SEND_TRANSPORT_RETRIES = 3
SEND_RETRY_BACKOFF_SECONDS = 1.0


class CdesktopError(RuntimeError):
    pass


class CdesktopRejectedError(CdesktopError):
    """A cdesktop server response definitively rejected the requested action.

    ``status`` is the typed discriminator every routing decision reads; the
    optional ``retry_at`` is the provider's own advertised reset, so a rate
    limit cools its account until the provider says it is over rather than for
    a blunt default window.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_at: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_at = retry_at


class CdesktopTransportError(CdesktopError):
    """The request never got a response (unreachable, timeout). The server may
    or may not have applied it - the only honest state is 'unknown'."""


class CdesktopDeliveryError(CdesktopError):
    """A follow-up could not be confirmed queued after retries and lookup.
    Loud by design: silent loss of a directive cost a coordinator 24 hours."""


class CdesktopInterruptedError(CdesktopRejectedError):
    """cdesktop cannot establish whether the keyed side effect ran (HTTP 424)."""


class CdesktopPendingError(CdesktopRejectedError):
    """A keyed operation is still owned by another cdesktop request (HTTP 425)."""


#: HTTP statuses that carry a typed meaning routing acts on. Everything else
#: stays an untyped ``CdesktopError``: routing must never infer a provider
#: outcome from a status it was not told the meaning of.
#:
#: 5xx is deliberately absent. This status belongs to SightMesh's own localhost
#: request to cdesktop, not to the model provider behind it, so a cdesktop
#: process that is restarting, out of disk, or panicking would be read as the
#: provider being down - and the whole account pool cooled for a fault that has
#: nothing to do with any account. An unreachable local service leaves the task
#: reserved and retryable, which is what it is.
_REJECTION_STATUSES = (401, 403, 409, 429)


def _rejection_type(status: int) -> type[CdesktopRejectedError] | None:
    if status == 424:
        return CdesktopInterruptedError
    if status == 425:
        return CdesktopPendingError
    return CdesktopRejectedError if status in _REJECTION_STATUSES else None


def _retry_at(headers: Any) -> float | None:
    """Absolute time the provider says to retry, from ``Retry-After`` seconds.

    Only the delta-seconds form is honoured. An HTTP-date form depends on the
    provider's clock agreeing with ours, and a wrong absolute time would cool
    an account for the wrong window; falling back to the default cooldown is
    the safe reading.
    """
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return time.time() + seconds if math.isfinite(seconds) and seconds >= 0 else None


EFFECT_NOT_FOUND_MESSAGE = "Managed task effect not found"


def is_effect_not_found(exc: CdesktopError) -> bool:
    """True when the executor definitively reported a missing managed effect.

    The pinned cdesktop seam returns HTTP 400 with this exact message for a
    missing ``(task, epoch)`` effect (managed_tasks.rs maps the miss to
    ``ApiError::BadRequest``); a future seam may correct that to 404. Both are
    real absence. Anything else - 5xx, timeout, unreachable - proves nothing.
    """
    if not isinstance(exc, CdesktopError):
        return False
    text = str(exc)
    if getattr(exc, "status", None) == 404 or "404" in text:
        return True
    # The client maps the seam's 400 to a plain CdesktopError without a typed
    # status, so the canonical miss message is the discriminator.
    return EFFECT_NOT_FOUND_MESSAGE.lower() in text.lower()


def execution_process_event_time(process: dict[str, Any]) -> datetime | None:
    raw = (
        process.get("completed_at")
        or process.get("updated_at")
        or process.get("started_at")
        or process.get("created_at")
    )
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


#: An execution process in one of these statuses has stopped without finishing
#: its work (``ExecutionProcessStatus`` in cdesktop's execution_process model).
PROCESS_FAILURE_STATUSES = frozenset({"failed", "killed"})

#: cdesktop's own normalized outcome classes (``ExecutionOutcomeClass``) mapped
#: onto the routing outcomes a route chain advances on. Only capacity and auth
#: are here: every other class describes the work failing, not the binding, and
#: rerouting it would just fail the same way on a fresh account.
#:
#: The pinned seam does not surface this classification yet, so today the map
#: matches nothing and a failed process blocks its task visibly. Reading it by
#: name means the mid-run reroute starts working the moment the seam does,
#: without anyone going back to guess from transcript text.
PROCESS_OUTCOME_CLASSES = {
    "quota_exhausted": "rate_limited",
    "rate_limited_transient": "rate_limited",
    "auth_expired": "auth",
    "auth_invalid": "auth",
}


def process_provider_outcome(
    process: Mapping[str, Any],
) -> tuple[str | None, float | None]:
    """The typed provider outcome a stopped process reports, and its reset.

    Reads only typed fields - the outcome's class name and its advertised
    reset. Transcript text is never consulted, which is the whole point: a
    worker whose test output merely mentions a rate limit must be
    indistinguishable, to this function, from one that printed nothing.
    """
    outcome = process.get("outcome")
    if isinstance(outcome, str):
        outcome = {"class": outcome}
    if not isinstance(outcome, Mapping):
        return None, None
    routing_outcome = PROCESS_OUTCOME_CLASSES.get(str(outcome.get("class") or ""))
    if routing_outcome is None:
        return None, None
    return routing_outcome, _outcome_reset(outcome)


def _outcome_reset(outcome: Mapping[str, Any]) -> float | None:
    resets_at = outcome.get("resets_at")
    if isinstance(resets_at, str):
        try:
            return datetime.fromisoformat(resets_at).timestamp()
        except ValueError:
            return None
    try:
        seconds = float(outcome["retry_after_seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    return time.time() + seconds if math.isfinite(seconds) and seconds >= 0 else None


def process_failure_reason(process: Mapping[str, Any]) -> str:
    """Why a stopped process stopped, from typed fields only.

    Deliberately not the transcript. This becomes a blocked task's reason, and
    a reason built from provider text is both a leak risk and an invitation to
    parse it back out again later.
    """
    status = str(process.get("status") or "failed")
    outcome = process.get("outcome")
    detail = (
        str(outcome.get("class") or "")
        if isinstance(outcome, Mapping)
        else str(outcome or "")
    )
    exit_code = process.get("exit_code")
    parts = [
        # A class name is a short enum value. Bounding it anyway means a seam
        # that one day puts something longer there cannot turn a task's reason
        # into a dump of whatever the provider said.
        part
        for part in (detail[:64], f"exit {exit_code}" if exit_code else "")
        if part
    ]
    return f"worker process {status}" + (f" ({', '.join(parts)})" if parts else "")


def latest_execution_process(
    processes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        process
        for process in processes
        if not process.get("dropped") and process.get("run_reason") != "devserver"
    ]
    return (
        max(
            eligible,
            key=lambda process: (
                execution_process_event_time(process)
                or datetime.min.replace(tzinfo=UTC),
                str(process.get("id") or ""),
            ),
        )
        if eligible
        else None
    )


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
            with open_transport(urlopen, request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error_type = _rejection_type(exc.code)
            message = f"{method} {path} failed: HTTP {exc.code}: {detail}"
            if error_type is None:
                raise CdesktopError(message) from exc
            raise error_type(
                message,
                status=exc.code,
                retry_at=_retry_at(exc.headers),
            ) from exc
        except URLError as exc:
            raise CdesktopTransportError(
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

    def execution_processes(
        self, session_id: str | None = None, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = {
            key: value
            for key, value in (("session_id", session_id), ("status", status))
            if value is not None
        }
        result = self.request("GET", "/execution-processes", query=query or None)
        if not isinstance(result, list):
            raise CdesktopError("cdesktop execution process response is not a list")
        return [dict(item) for item in result if isinstance(item, dict)]

    def running_execution_processes(self) -> list[dict[str, Any]]:
        """Return fleet-wide running processes across the old and new API seam."""
        try:
            result = self.request("GET", "/execution-processes/running")
        except CdesktopError as exc:
            if "HTTP 404" not in str(exc):
                raise
        else:
            if not isinstance(result, list):
                raise CdesktopError("cdesktop running execution response is not a list")
            return [
                {**dict(item), "status": "running", "session_name": item.get("name")}
                for item in result
                if isinstance(item, dict)
            ]

        running: list[dict[str, Any]] = []
        for workspace in self.workspaces():
            workspace_id = workspace.get("id")
            if not workspace_id or workspace.get("archived"):
                continue
            for session in self.sessions(str(workspace_id)):
                session_id = session.get("id")
                if not session_id:
                    continue
                for process in self.execution_processes(str(session_id)):
                    if process.get("status") != "running":
                        continue
                    running.append(
                        {
                            **process,
                            "session_id": session_id,
                            "session_name": session.get("name"),
                            "workspace_id": workspace_id,
                            "workspace_name": workspace.get("name")
                            or workspace.get("branch"),
                        }
                    )
        return running

    def queue_status(self, session_id: str) -> dict[str, Any]:
        result = self.request("GET", f"/sessions/{session_id}/queue")
        if not isinstance(result, dict):
            raise CdesktopError("cdesktop queue status response is invalid")
        return result

    def probe_connectivity(self) -> bool:
        """Bounded, side-effect-free gate used before native dispatch."""
        try:
            with open_transport(
                urlopen, f"{self.base_url}/api/health", timeout=1
            ) as response:
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
            with open_transport(
                websocket_connect,
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
    ) -> dict[str, Any]:
        payload = self.workspace_launch_request(
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
        known_workspace_ids = {str(item.get("id")) for item in self.workspaces()}
        try:
            return self.request("POST", "/workspaces/start", payload)
        except CdesktopError as exc:
            cleanup = self._cleanup_failed_workspace_start(name, known_workspace_ids)
            if cleanup:
                raise CdesktopError(f"{exc}; {cleanup}") from exc
            raise

    def workspace_launch_request(
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
        """Build the native launch request used by direct and managed starts."""
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
        # A transport failure leaves the outcome unknown, not failed. Because
        # every keyed send is idempotent on dedupe_key, retrying the identical
        # POST is provably safe; afterwards we VERIFY the row instead of
        # guessing, and report a typed delivery state. Never silent.
        attempts = SEND_TRANSPORT_RETRIES if dedupe_key else 1
        last_error: CdesktopTransportError | None = None
        for attempt in range(attempts):
            try:
                result = self.request(
                    "POST", f"/sessions/{session_id}/follow-up", payload, headers=headers
                )
                if isinstance(result, dict):
                    result.setdefault("delivery", "queued")
                return result
            except CdesktopTransportError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(SEND_RETRY_BACKOFF_SECONDS * (attempt + 1))
        if dedupe_key and self._command_is_queued(session_id, dedupe_key):
            return {"delivery": "already_queued", "dedupe_key": dedupe_key}
        raise CdesktopDeliveryError(
            f"Follow-up to session {session_id} could NOT be confirmed queued after "
            f"{attempts} attempt(s) ({last_error}). Retry with the same dedupe_key "
            f"{dedupe_key!r}; it is safe to repeat."
        ) from last_error

    def _command_is_queued(self, session_id: str, dedupe_key: str) -> bool:
        """Verification read: is a command with this dedupe_key present?"""
        try:
            rows = self.session_commands(session_id)
        except CdesktopError:
            return False
        return any(
            isinstance(row, dict) and row.get("dedupe_key") == dedupe_key for row in rows
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
    ) -> dict[str, Any]:
        payload = self.session_launch_request(
            name=name,
            prompt=prompt,
            executor=executor,
            permission_policy=permission_policy,
            model=model,
            reasoning=reasoning,
            provider_id=provider_id,
            auth_binding_id=auth_binding_id,
        )
        return self.request("POST", f"/sessions/{caller_session}/teammates", payload)

    @staticmethod
    def session_launch_request(
        *,
        name: str,
        prompt: str,
        executor: str | None = None,
        permission_policy: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        provider_id: str | None = None,
        auth_binding_id: str | None = None,
    ) -> dict[str, Any]:
        """Build the native teammate request used by direct and managed starts."""
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
        return payload

    def managed_launch(
        self, task_id: str, epoch: int, launch: dict[str, Any]
    ) -> dict[str, Any]:
        """Create or return the one native effect reserved for a task epoch."""
        if epoch < 1:
            raise ValueError("Task epoch must be positive")
        result = self.request("PUT", f"/managed-tasks/{task_id}/epochs/{epoch}", launch)
        if not isinstance(result, dict):
            raise CdesktopError("cdesktop managed launch response is invalid")
        return result

    def managed_effect(self, task_id: str, epoch: int) -> dict[str, Any]:
        result = self.request("GET", f"/managed-tasks/{task_id}/epochs/{epoch}")
        if not isinstance(result, dict):
            raise CdesktopError("cdesktop managed effect response is invalid")
        return result

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
