"""Conductor-safe durable parent escalation.

When SightMesh is launched by cdesktop, a spawned child session always has a
live cdesktop parent to receive escalations (``sightmesh parent --message``).
When the launcher is external (for example Conductor invoking the SightMesh
CLI directly), no such parent may exist. This module makes that case correct
by construction: every escalation is either delivered to a confirmed live,
non-archived parent session, or durably parked in a decision inbox so it is
never silently dropped. Parked escalations are never auto-delivered into a
session that has since been archived -- that delivery path stays closed
forever for that record; an operator resolves it explicitly instead.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cdesktop import CdesktopClient, CdesktopError
from .service import state_dir

CDESKTOP_SESSION_ENV = "CDESKTOP_SESSION_ID"
CONDUCTOR_ENV_HINTS = ("CONDUCTOR_WORKSPACE_NAME", "CONDUCTOR_ROOT_PATH")

PARK_REASONS = frozenset({"no_parent", "parent_archived", "parent_unreachable"})


class EscalationStoreError(RuntimeError):
    pass


def escalation_db_path() -> Path:
    return state_dir() / "escalations.sqlite3"


@dataclass(frozen=True)
class LauncherIdentity:
    launcher: str  # "cdesktop" | "external"
    detail: str | None  # best-effort hint, e.g. "conductor" or "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"launcher": self.launcher, "detail": self.detail}


def detect_launcher(env: dict[str, str] | None = None) -> LauncherIdentity:
    """Identify what invoked this SightMesh process, using the same
    CDESKTOP_SESSION_ID invariant the rest of the CLI already treats as
    proof of a live cdesktop parent context."""
    source = env if env is not None else os.environ
    if source.get(CDESKTOP_SESSION_ENV):
        return LauncherIdentity(launcher="cdesktop", detail=None)
    if any(source.get(name) for name in CONDUCTOR_ENV_HINTS):
        return LauncherIdentity(launcher="external", detail="conductor")
    return LauncherIdentity(launcher="external", detail="unknown")


@dataclass(frozen=True)
class ParkedEscalation:
    escalation_id: str
    child_session_id: str
    child_workspace_id: str | None
    recorded_parent_session_id: str | None
    reason: str
    message: str
    dedupe_key: str
    status: str
    created_at: float
    resolved_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "escalation_id": self.escalation_id,
            "child_session_id": self.child_session_id,
            "child_workspace_id": self.child_workspace_id,
            "recorded_parent_session_id": self.recorded_parent_session_id,
            "reason": self.reason,
            "message": self.message,
            "dedupe_key": self.dedupe_key,
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


class EscalationStore:
    """Durable, restart-proof home for launcher identity and parked escalations."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or escalation_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            for path in (
                self.path,
                self.path.with_name(f"{self.path.name}-wal"),
                self.path.with_name(f"{self.path.name}-shm"),
            ):
                if path.exists():
                    os.chmod(path, 0o600)
            return conn
        except sqlite3.Error as exc:
            raise EscalationStoreError(
                f"Cannot open escalation store {self.path}: {exc}"
            ) from exc

    def _initialize(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS launcher_identities (
                        session_id TEXT PRIMARY KEY,
                        workspace_id TEXT,
                        launcher TEXT NOT NULL CHECK (launcher IN ('cdesktop', 'external')),
                        detail TEXT,
                        recorded_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS escalations (
                        escalation_id TEXT PRIMARY KEY,
                        child_session_id TEXT NOT NULL,
                        child_workspace_id TEXT,
                        recorded_parent_session_id TEXT,
                        reason TEXT NOT NULL
                            CHECK (reason IN ('no_parent', 'parent_archived', 'parent_unreachable')),
                        message TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK (status IN ('parked', 'resolved'))
                            DEFAULT 'parked',
                        created_at REAL NOT NULL,
                        resolved_at REAL
                    )
                    """
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_escalations_dedupe "
                    "ON escalations(dedupe_key)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_escalations_status "
                    "ON escalations(status, created_at DESC)"
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot initialize escalation store {self.path}: {exc}"
            ) from exc

    def record_launcher(
        self,
        *,
        session_id: str,
        workspace_id: str | None,
        identity: LauncherIdentity,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO launcher_identities
                        (session_id, workspace_id, launcher, detail, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        workspace_id = excluded.workspace_id,
                        launcher = excluded.launcher,
                        detail = excluded.detail,
                        recorded_at = excluded.recorded_at
                    """,
                    (
                        session_id,
                        workspace_id,
                        identity.launcher,
                        identity.detail,
                        time.time(),
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot record launcher identity for {session_id}: {exc}"
            ) from exc

    def get_launcher(self, session_id: str) -> LauncherIdentity | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT launcher, detail FROM launcher_identities WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot read launcher identity for {session_id}: {exc}"
            ) from exc
        if row is None:
            return None
        return LauncherIdentity(launcher=row["launcher"], detail=row["detail"])

    def park(
        self,
        *,
        child_session_id: str,
        child_workspace_id: str | None,
        recorded_parent_session_id: str | None,
        reason: str,
        message: str,
        dedupe_key: str,
    ) -> ParkedEscalation:
        if reason not in PARK_REASONS:
            raise ValueError(f"Unknown park reason: {reason}")
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO escalations (
                        escalation_id, child_session_id, child_workspace_id,
                        recorded_parent_session_id, reason, message, dedupe_key,
                        status, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'parked', ?, NULL)
                    """,
                    (
                        str(uuid.uuid4()),
                        child_session_id,
                        child_workspace_id,
                        recorded_parent_session_id,
                        reason,
                        message,
                        dedupe_key,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM escalations WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot park escalation: {exc}") from exc
        if row is None:
            raise EscalationStoreError(f"Escalation is missing after park: {dedupe_key}")
        return _from_row(row)

    def resolve(self, escalation_id: str) -> ParkedEscalation:
        completed_at = time.time()
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE escalations
                    SET status = 'resolved', resolved_at = ?
                    WHERE escalation_id = ? AND status = 'parked'
                    """,
                    (completed_at, escalation_id),
                )
                if cursor.rowcount != 1:
                    raise EscalationStoreError(
                        f"Escalation is missing or already resolved: {escalation_id}"
                    )
                row = conn.execute(
                    "SELECT * FROM escalations WHERE escalation_id = ?",
                    (escalation_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot resolve escalation: {exc}") from exc
        if row is None:
            raise EscalationStoreError(f"Escalation is missing: {escalation_id}")
        return _from_row(row)

    def pending(self, *, limit: int = 100) -> list[ParkedEscalation]:
        if limit < 1 or limit > 1000:
            raise ValueError("Escalation pending limit must be between 1 and 1000")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM escalations
                    WHERE status = 'parked'
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot read parked escalations: {exc}") from exc
        return [_from_row(row) for row in rows]


def _from_row(row: sqlite3.Row) -> ParkedEscalation:
    return ParkedEscalation(
        escalation_id=row["escalation_id"],
        child_session_id=row["child_session_id"],
        child_workspace_id=row["child_workspace_id"],
        recorded_parent_session_id=row["recorded_parent_session_id"],
        reason=row["reason"],
        message=row["message"],
        dedupe_key=row["dedupe_key"],
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def default_dedupe_key(child_session_id: str, message: str) -> str:
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return f"parent-escalation:{child_session_id}:{digest}"


def escalate(
    client: CdesktopClient,
    *,
    child_session_id: str,
    child_workspace_id: str | None,
    parent_session_id: str | None,
    message: str,
    store: EscalationStore | None = None,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """Deliver to a confirmed live, non-archived parent, or durably park.

    There is no third outcome: an escalation is always either delivered or
    recorded in the decision inbox. A parked record's recorded parent is
    never re-targeted automatically once that parent's workspace is
    archived -- superseded or completed sessions must never be auto-resumed
    by queued delivery.
    """
    store = store or EscalationStore()
    key = dedupe_key or default_dedupe_key(child_session_id, message)
    reason: str
    if parent_session_id:
        try:
            parent = client.session(parent_session_id)
            workspace = client.workspace(str(parent["workspace_id"]))
        except CdesktopError:
            reason = "parent_unreachable"
        else:
            if workspace.get("archived"):
                reason = "parent_archived"
            else:
                follow_up = client.send(
                    parent_session_id,
                    message,
                    child_session_id,
                    dedupe_key=key,
                    intent="replace",
                )
                return {
                    "delivered": True,
                    "parent_session_id": parent_session_id,
                    "parent_workspace_id": str(workspace["id"]),
                    "follow_up": follow_up,
                }
    else:
        reason = "no_parent"
    parked = store.park(
        child_session_id=child_session_id,
        child_workspace_id=child_workspace_id,
        recorded_parent_session_id=parent_session_id,
        reason=reason,
        message=message,
        dedupe_key=key,
    )
    return {"delivered": False, "reason": reason, "parked": parked.to_dict()}
