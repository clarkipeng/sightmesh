from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_TTL_SECONDS = 4 * 60 * 60


class LeaseError(RuntimeError):
    pass


def default_lease_dir() -> Path:
    return Path.home() / ".local" / "state" / "agent-deck" / "leases"


def _now() -> float:
    return time.time()


def _canonical(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _identity_key(repo_path: str, worktree_path: str | None) -> str:
    raw = "\0".join([repo_path, worktree_path or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _pid_is_alive(pid: int | None, hostname: str | None) -> bool:
    if not pid or hostname not in {None, "", socket.gethostname()}:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class Lease:
    token: str
    owner: str
    repo_path: str
    worktree_path: str | None
    created_at: float
    expires_at: float
    pid: int
    hostname: str

    @property
    def expired(self) -> bool:
        return self.expires_at <= _now()

    @property
    def live(self) -> bool:
        return not self.expired and _pid_is_alive(self.pid, self.hostname)

    def conflicts(self, repo_path: str, worktree_path: str | None) -> bool:
        return self.repo_path == repo_path or (
            bool(self.worktree_path) and bool(worktree_path) and self.worktree_path == worktree_path
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "owner": self.owner,
            "repo_path": self.repo_path,
            "worktree_path": self.worktree_path,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "pid": self.pid,
            "hostname": self.hostname,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Lease":
        return cls(
            token=str(data["token"]),
            owner=str(data["owner"]),
            repo_path=str(data["repo_path"]),
            worktree_path=data.get("worktree_path"),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            pid=int(data.get("pid") or 0),
            hostname=str(data.get("hostname") or ""),
        )


class LeaseStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_lease_dir()

    def _lease_path(self, repo_path: str, worktree_path: str | None) -> Path:
        return self.root / f"{_identity_key(repo_path, worktree_path)}.json"

    def _read(self, path: Path) -> Lease | None:
        try:
            return Lease.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LeaseError(f"Invalid lease file {path}: {exc}") from exc

    def _write_atomic(self, path: Path, lease: Lease) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(lease.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def _with_lock(self):
        return _DirectoryLock(self.root / ".lock")

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
                    path.unlink(missing_ok=True)
                    recovered.append(lease)
        return recovered

    def acquire(
        self,
        owner: str,
        repo_path: str | Path,
        worktree_path: str | Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
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
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            for path in sorted(self.root.glob("*.json")):
                existing = self._read(path)
                if not existing:
                    continue
                if not existing.live:
                    path.unlink(missing_ok=True)
                    continue
                if existing.conflicts(repo, worktree):
                    raise LeaseError(
                        "Repository or worktree is already owned by "
                        f"{existing.owner} until {existing.expires_at}"
                    )
            self._write_atomic(self._lease_path(repo, worktree), lease)
        return lease

    def release(self, token: str) -> Lease:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._with_lock():
            for path in sorted(self.root.glob("*.json")):
                lease = self._read(path)
                if lease and lease.token == token:
                    path.unlink(missing_ok=True)
                    return lease
        raise LeaseError("No lease found for token")


class _DirectoryLock:
    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> "_DirectoryLock":
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.path.mkdir()
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise LeaseError(f"Timed out waiting for lease lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *_exc: object) -> None:
        self.path.rmdir()
