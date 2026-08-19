from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

LOGGER = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 4 * 60 * 60


class LeaseError(RuntimeError):
    pass


def default_lease_dir() -> Path:
    return Path.home() / ".local" / "state" / "sightmesh" / "leases"


def _now() -> float:
    return time.time()


def _canonical(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _identity_key(repo_path: str, worktree_path: str | None) -> str:
    raw = "\0".join([repo_path, worktree_path or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Lease:
    token: str
    owner: str
    repo_path: str
    worktree_path: str | None
    created_at: float
    expires_at: float
    hostname: str
    workspace_id: str | None = None
    session_id: str | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at <= _now()

    @property
    def live(self) -> bool:
        return not self.expired

    def conflicts(self, repo_path: str, worktree_path: str | None) -> bool:
        if self.repo_path != repo_path:
            return (
                bool(self.worktree_path)
                and bool(worktree_path)
                and self.worktree_path == worktree_path
            )
        if self.worktree_path is None or worktree_path is None:
            return True
        return self.worktree_path == worktree_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "owner": self.owner,
            "repo_path": self.repo_path,
            "worktree_path": self.worktree_path,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "hostname": self.hostname,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
        }

    def to_public_dict(self, now: float | None = None) -> dict[str, Any]:
        """Return lease identity and age without its bearer token."""
        current_time = _now() if now is None else now
        return {
            "owner": self.owner,
            "repo_path": self.repo_path,
            "worktree_path": self.worktree_path,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "age_seconds": max(0, int(current_time - self.created_at)),
            "expired": self.expires_at <= current_time,
            "hostname": self.hostname,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lease:
        return cls(
            token=str(data["token"]),
            owner=str(data["owner"]),
            repo_path=str(data["repo_path"]),
            worktree_path=data.get("worktree_path"),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            hostname=str(data.get("hostname") or ""),
            workspace_id=data.get("workspace_id"),
            session_id=data.get("session_id"),
        )


class LeaseStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_lease_dir()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        workspace_root = self.root / "workspaces"
        workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace_root.chmod(0o700)

    def _lease_path(self, repo_path: str, worktree_path: str | None) -> Path:
        return self.root / f"{_identity_key(repo_path, worktree_path)}.json"

    def _workspace_path(self, workspace_id: str) -> Path:
        return self.root / "workspaces" / f"{workspace_id}.json"

    def _read(self, path: Path) -> Lease | None:
        try:
            return Lease.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LeaseError(f"Invalid lease file {path}: {exc}") from exc

    def _write_atomic(self, path: Path, lease: Lease) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(lease.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.chmod(0o600)
        os.replace(tmp, path)

    def _with_lock(self):
        return _FileLock(self.root / ".lock")

    def list(self, include_stale: bool = True) -> list[Lease]:
        self.root.mkdir(parents=True, exist_ok=True)
        leases: list[Lease] = []
        for path in sorted(self.root.glob("*.json")):
            lease = self._read(path)
            if lease and (include_stale or lease.live):
                leases.append(lease)
        return leases

    def recover_stale(self) -> list[Lease]:
        recovered: list[Lease] = []
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            for path in sorted(self.root.glob("*.json")):
                lease = self._read(path)
                if lease and not lease.live:
                    self._remove_locked(path, lease)
                    recovered.append(lease)
        return recovered

    def acquire(
        self,
        owner: str,
        repo_path: str | Path,
        worktree_path: str | Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> Lease:
        if ttl_seconds <= 0:
            raise LeaseError("ttl_seconds must be positive")
        repo = _canonical(repo_path)
        worktree = _canonical(worktree_path) if worktree_path else None
        now = _now()
        lease = Lease(
            token=uuid.uuid4().hex,
            owner=owner,
            repo_path=repo,
            worktree_path=worktree,
            created_at=now,
            expires_at=now + ttl_seconds,
            hostname=socket.gethostname(),
            workspace_id=workspace_id,
            session_id=session_id,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            for path in sorted(self.root.glob("*.json")):
                existing = self._read(path)
                if not existing:
                    continue
                if not existing.live:
                    self._remove_locked(path, existing)
                    continue
                if existing.conflicts(repo, worktree):
                    raise LeaseError(
                        "Repository or worktree is already owned by "
                        f"{existing.owner} until {existing.expires_at}"
                    )
            path = self._lease_path(repo, worktree)
            self._write_atomic(path, lease)
            if workspace_id:
                self._write_workspace(workspace_id, lease.token)
        return lease

    def renew(
        self,
        token: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        owner: str | None = None,
        workspace_id: str | None = None,
    ) -> Lease:
        if ttl_seconds <= 0:
            raise LeaseError("ttl_seconds must be positive")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            for path in sorted(self.root.glob("*.json")):
                lease = self._read(path)
                if lease and lease.token == token:
                    self._check_owner(lease, owner, workspace_id)
                    renewed = Lease(
                        token=lease.token,
                        owner=lease.owner,
                        repo_path=lease.repo_path,
                        worktree_path=lease.worktree_path,
                        created_at=lease.created_at,
                        expires_at=_now() + ttl_seconds,
                        hostname=lease.hostname,
                        workspace_id=lease.workspace_id,
                        session_id=lease.session_id,
                    )
                    self._write_atomic(path, renewed)
                    return renewed
        raise LeaseError("No lease found for token")

    def attach_workspace(
        self, token: str, workspace_id: str, session_id: str | None = None
    ) -> Lease:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            for path in sorted(self.root.glob("*.json")):
                lease = self._read(path)
                if lease and lease.token == token:
                    attached = Lease(
                        token=lease.token,
                        owner=lease.owner,
                        repo_path=lease.repo_path,
                        worktree_path=lease.worktree_path,
                        created_at=lease.created_at,
                        expires_at=lease.expires_at,
                        hostname=lease.hostname,
                        workspace_id=workspace_id,
                        session_id=session_id or lease.session_id,
                    )
                    self._write_atomic(path, attached)
                    self._write_workspace(workspace_id, token)
                    return attached
        raise LeaseError("No lease found for token")

    def release(
        self, token: str, owner: str | None = None, workspace_id: str | None = None
    ) -> Lease:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            return self._release_locked(token, owner=owner, workspace_id=workspace_id)

    def release_workspace(self, workspace_id: str) -> Lease:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            token = self._read_workspace(workspace_id)
            if not token:
                raise LeaseError(f"No lease found for workspace {workspace_id}")
            return self._release_locked(token, workspace_id=workspace_id)

    def release_workspace_if_present(self, workspace_id: str) -> Lease | None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            token = self._read_workspace(workspace_id)
            if not token:
                return None
            return self._release_locked(token, workspace_id=workspace_id)

    def renew_workspace(
        self, workspace_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> Lease | None:
        if ttl_seconds <= 0:
            raise LeaseError("ttl_seconds must be positive")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            token = self._read_workspace(workspace_id)
            if not token:
                return None
            for path in sorted(self.root.glob("*.json")):
                lease = self._read(path)
                if lease and lease.token == token:
                    self._check_owner(lease, workspace_id=workspace_id)
                    renewed = Lease(
                        token=lease.token,
                        owner=lease.owner,
                        repo_path=lease.repo_path,
                        worktree_path=lease.worktree_path,
                        created_at=lease.created_at,
                        expires_at=_now() + ttl_seconds,
                        hostname=lease.hostname,
                        workspace_id=lease.workspace_id,
                        session_id=lease.session_id,
                    )
                    self._write_atomic(path, renewed)
                    return renewed
            raise LeaseError(
                f"Workspace lease mapping has no lease record: {workspace_id}"
            )

    def workspace_token(self, workspace_id: str) -> str | None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            return self._read_workspace(workspace_id)

    def assert_spawn_allowed(self, repo_path: str | Path, use_worktree: bool) -> None:
        repo = _canonical(repo_path)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            for path in sorted(self.root.glob("*.json")):
                existing = self._read(path)
                if not existing:
                    continue
                if not existing.live:
                    self._remove_locked(path, existing)
                    continue
                if existing.repo_path == repo and (
                    not use_worktree or existing.worktree_path is None
                ):
                    raise LeaseError(
                        "Repository is already owned by "
                        f"{existing.owner} until {existing.expires_at}"
                    )

    def _release_locked(
        self, token: str, owner: str | None = None, workspace_id: str | None = None
    ) -> Lease:
        for path in sorted(self.root.glob("*.json")):
            lease = self._read(path)
            if lease and lease.token == token:
                self._check_owner(lease, owner, workspace_id)
                self._remove_locked(path, lease)
                return lease
        raise LeaseError("No lease found for token")

    def _remove_locked(self, path: Path, lease: Lease) -> None:
        path.unlink(missing_ok=True)
        if lease.workspace_id:
            self._workspace_path(lease.workspace_id).unlink(missing_ok=True)

    def _check_owner(
        self, lease: Lease, owner: str | None = None, workspace_id: str | None = None
    ) -> None:
        if owner and lease.owner != owner:
            raise LeaseError("Lease owner does not match")
        if workspace_id and lease.workspace_id != workspace_id:
            raise LeaseError("Lease workspace does not match")

    def _write_workspace(self, workspace_id: str, token: str) -> None:
        target = self._workspace_path(workspace_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps({"token": token}, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        os_replace_parent(tmp, target)

    def _read_workspace(self, workspace_id: str) -> str | None:
        try:
            data = json.loads(
                self._workspace_path(workspace_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise LeaseError(
                f"Invalid workspace lease mapping for {workspace_id}: {exc}"
            ) from exc
        token = data.get("token")
        return str(token) if token else None


def os_replace_parent(tmp: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(target)


class _FileLock:
    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._file: Any = None

    def __enter__(self) -> Self:
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        self.path.chmod(0o600)
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LeaseError(f"Timed out waiting for lease lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *_exc: object) -> None:
        if self._file:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()


def _sync_active_workspace(
    store: LeaseStore, client: Any, workspace: dict[str, Any], ttl_seconds: int
) -> Lease:
    workspace_id = str(workspace["id"])
    renewed = store.renew_workspace(workspace_id, ttl_seconds)
    if renewed:
        sessions = client.sessions(workspace_id)
        session_id = str(sessions[0]["id"]) if sessions else None
        if session_id and renewed.session_id != session_id:
            renewed = store.attach_workspace(renewed.token, workspace_id, session_id)
        return renewed

    repos = client.workspace_repos(workspace_id)
    if not repos:
        raise LeaseError(f"Active workspace has no repository: {workspace_id}")
    repo = repos[0]
    repo_path = Path(str(repo["path"])).expanduser().resolve()
    worktree_path = None
    if workspace.get("use_worktree"):
        container = workspace.get("container_ref")
        if not container:
            raise LeaseError(
                f"Active worktree workspace has no container: {workspace_id}"
            )
        worktree_path = Path(str(container)).expanduser().resolve() / str(repo["name"])
    sessions = client.sessions(workspace_id)
    session_id = str(sessions[0]["id"]) if sessions else None
    try:
        return store.acquire(
            f"cdesktop-workspace:{workspace_id}",
            repo_path,
            worktree_path,
            ttl_seconds,
            workspace_id=workspace_id,
            session_id=session_id,
        )
    except LeaseError:
        renewed = store.renew_workspace(workspace_id, ttl_seconds)
        if not renewed:
            raise
        return renewed


def sync_active_workspaces(
    client: Any,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    on_error: Callable[[str], None] | None = None,
) -> list[Lease]:
    """Renew or backfill leases for every valid active cdesktop workspace.

    A workspace that fails to sync (malformed record, missing repo/container,
    etc.) is isolated: its error is reported via ``on_error`` (or logged, if
    no callback was given) and the remaining workspaces still sync normally.
    """
    store = LeaseStore()
    synced: list[Lease] = []
    for workspace in client.workspaces():
        if workspace.get("archived"):
            continue
        try:
            synced.append(_sync_active_workspace(store, client, workspace, ttl_seconds))
        except LeaseError as exc:
            if on_error is None:
                LOGGER.warning("Cannot sync cdesktop workspace: %s", exc)
            else:
                on_error(str(exc))
    return synced
