from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .service import state_dir


class ApprovalAuditError(RuntimeError):
    pass


def approval_db_path() -> Path:
    return state_dir() / "approvals.sqlite3"


@dataclass(frozen=True)
class ApprovalDecision:
    decision_id: str
    approval_id: str
    execution_process_id: str
    session_id: str | None
    workspace_id: str | None
    tool_name: str
    decision: str
    reviewer_kind: str
    reviewer_id: str
    reason_sha256: str | None
    status: str
    error: str | None
    created_at: float
    completed_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "approval_id": self.approval_id,
            "execution_process_id": self.execution_process_id,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "tool_name": self.tool_name,
            "decision": self.decision,
            "reviewer_kind": self.reviewer_kind,
            "reviewer_id": self.reviewer_id,
            "reason_sha256": self.reason_sha256,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class ApprovalAuditStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or approval_db_path()
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
            raise ApprovalAuditError(
                f"Cannot open approval audit store {self.path}: {exc}"
            ) from exc

    def _initialize(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS approval_decisions (
                        decision_id TEXT PRIMARY KEY,
                        approval_id TEXT NOT NULL,
                        execution_process_id TEXT NOT NULL,
                        session_id TEXT,
                        workspace_id TEXT,
                        tool_name TEXT NOT NULL,
                        decision TEXT NOT NULL
                            CHECK (decision IN ('approved', 'denied')),
                        reviewer_kind TEXT NOT NULL
                            CHECK (reviewer_kind IN ('human', 'session')),
                        reviewer_id TEXT NOT NULL,
                        reason_sha256 TEXT,
                        status TEXT NOT NULL
                            CHECK (status IN ('submitting', 'responded', 'failed')),
                        error TEXT,
                        created_at REAL NOT NULL,
                        completed_at REAL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_approval_decisions_created "
                    "ON approval_decisions(created_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_approval_decisions_approval "
                    "ON approval_decisions(approval_id, created_at DESC)"
                )
        except sqlite3.DatabaseError as exc:
            raise ApprovalAuditError(
                f"Cannot initialize approval audit store {self.path}: {exc}"
            ) from exc

    def begin(
        self,
        *,
        approval: dict[str, Any],
        decision: str,
        reviewer_kind: str,
        reviewer_id: str,
        reason: str | None,
    ) -> ApprovalDecision:
        now = time.time()
        record = ApprovalDecision(
            decision_id=str(uuid.uuid4()),
            approval_id=str(approval["approval_id"]),
            execution_process_id=str(approval["execution_process_id"]),
            session_id=_optional_string(approval.get("session_id")),
            workspace_id=_optional_string(approval.get("workspace_id")),
            tool_name=str(approval["tool_name"]),
            decision=decision,
            reviewer_kind=reviewer_kind,
            reviewer_id=reviewer_id,
            reason_sha256=(
                hashlib.sha256(reason.encode("utf-8")).hexdigest() if reason else None
            ),
            status="submitting",
            error=None,
            created_at=now,
            completed_at=None,
        )
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO approval_decisions (
                        decision_id, approval_id, execution_process_id, session_id,
                        workspace_id, tool_name, decision, reviewer_kind,
                        reviewer_id, reason_sha256, status, error, created_at,
                        completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.decision_id,
                        record.approval_id,
                        record.execution_process_id,
                        record.session_id,
                        record.workspace_id,
                        record.tool_name,
                        record.decision,
                        record.reviewer_kind,
                        record.reviewer_id,
                        record.reason_sha256,
                        record.status,
                        record.error,
                        record.created_at,
                        record.completed_at,
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise ApprovalAuditError(f"Cannot record approval attempt: {exc}") from exc
        return record

    def finish(
        self, decision_id: str, *, succeeded: bool, error: str | None = None
    ) -> ApprovalDecision:
        status = "responded" if succeeded else "failed"
        completed_at = time.time()
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE approval_decisions
                    SET status = ?, error = ?, completed_at = ?
                    WHERE decision_id = ? AND status = 'submitting'
                    """,
                    (status, error, completed_at, decision_id),
                )
                if cursor.rowcount != 1:
                    raise ApprovalAuditError(
                        f"Approval audit attempt is missing or already finished: {decision_id}"
                    )
                row = conn.execute(
                    "SELECT * FROM approval_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ApprovalAuditError(f"Cannot finish approval audit: {exc}") from exc
        if row is None:
            raise ApprovalAuditError(
                f"Approval audit attempt is missing: {decision_id}"
            )
        return _from_row(row)

    def history(self, *, limit: int = 50) -> list[ApprovalDecision]:
        if limit < 1 or limit > 500:
            raise ValueError("Approval history limit must be between 1 and 500")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM approval_decisions ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ApprovalAuditError(f"Cannot read approval history: {exc}") from exc
        return [_from_row(row) for row in rows]


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _from_row(row: sqlite3.Row) -> ApprovalDecision:
    return ApprovalDecision(
        decision_id=row["decision_id"],
        approval_id=row["approval_id"],
        execution_process_id=row["execution_process_id"],
        session_id=row["session_id"],
        workspace_id=row["workspace_id"],
        tool_name=row["tool_name"],
        decision=row["decision"],
        reviewer_kind=row["reviewer_kind"],
        reviewer_id=row["reviewer_id"],
        reason_sha256=row["reason_sha256"],
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )
