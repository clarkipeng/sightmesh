from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .service import state_dir


DEFAULT_MAX_PENDING = 500
DEFAULT_MAX_PENDING_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_PROMPT_BYTES = 128 * 1024
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 60.0


class DeliveryStoreError(RuntimeError):
    pass


class DeliveryCapacityError(DeliveryStoreError):
    pass


@dataclass(frozen=True)
class DeliveryPolicy:
    max_pending: int = DEFAULT_MAX_PENDING
    max_pending_bytes: int = DEFAULT_MAX_PENDING_BYTES
    max_prompt_bytes: int = DEFAULT_MAX_PROMPT_BYTES
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS


@dataclass(frozen=True)
class DeliveryRecord:
    idempotency_key: str
    session_id: str
    message_type: str
    status: str
    prompt: str | None
    delivery_id: str | None
    correlation_id: str | None
    from_peer: str | None
    attempt_count: int
    next_attempt_at: float
    last_error: str | None
    created_at: float
    updated_at: float
    injected_at: float | None
    dead_lettered_at: float | None
    prompt_bytes: int


def delivery_db_path() -> Path:
    return state_dir() / "delivery.sqlite3"


def idempotency_key(
    *,
    session_id: str,
    message_type: str,
    delivery_id: str | None,
    correlation_id: str | None,
    from_peer: str | None = None,
    text: str | None = None,
) -> str:
    source = delivery_id or correlation_id
    if not source:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "session_id": session_id,
                    "message_type": message_type,
                    "from_peer": from_peer,
                    "text": text,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        source = f"content:{digest}"
    raw = f"{session_id}\0{message_type}\0{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DeliveryStore:
    def __init__(self, path: Path | None = None, policy: DeliveryPolicy | None = None) -> None:
        self.path = path or delivery_db_path()
        self.policy = policy or DeliveryPolicy()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            return conn
        except sqlite3.Error as exc:
            raise DeliveryStoreError(f"Cannot open delivery store {self.path}: {exc}") from exc

    def _initialize(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS deliveries (
                        idempotency_key TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        message_type TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'injected', 'dead')
                        ),
                        prompt TEXT,
                        delivery_id TEXT,
                        correlation_id TEXT,
                        from_peer TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL DEFAULT 0,
                        last_error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        injected_at REAL,
                        dead_lettered_at REAL,
                        prompt_bytes INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_deliveries_status_next "
                    "ON deliveries(status, next_attempt_at, created_at)"
                )
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot initialize delivery store {self.path}: {exc}") from exc

    def enqueue(self, record: DeliveryRecord) -> DeliveryRecord:
        prompt_bytes = len((record.prompt or "").encode("utf-8"))
        if prompt_bytes > self.policy.max_prompt_bytes:
            raise DeliveryCapacityError(
                f"Delivery prompt is {prompt_bytes} bytes; limit is "
                f"{self.policy.max_prompt_bytes} bytes"
            )
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = self._get(conn, record.idempotency_key)
                if existing:
                    conn.execute("COMMIT")
                    return existing
                self._check_capacity(conn, prompt_bytes)
                conn.execute(
                    """
                    INSERT INTO deliveries (
                        idempotency_key, session_id, message_type, status, prompt,
                        delivery_id, correlation_id, from_peer, attempt_count,
                        next_attempt_at, last_error, created_at, updated_at,
                        injected_at, dead_lettered_at, prompt_bytes
                    )
                    VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, 0, NULL, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        record.idempotency_key,
                        record.session_id,
                        record.message_type,
                        record.prompt,
                        record.delivery_id,
                        record.correlation_id,
                        record.from_peer,
                        record.created_at,
                        record.created_at,
                        prompt_bytes,
                    ),
                )
                conn.execute("COMMIT")
                return self.get(record.idempotency_key) or record
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot enqueue delivery: {exc}") from exc

    def _check_capacity(self, conn: sqlite3.Connection, added_bytes: int) -> None:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(prompt_bytes), 0) AS bytes
            FROM deliveries WHERE status = 'pending'
            """
        ).fetchone()
        pending_count = int(row["count"])
        pending_bytes = int(row["bytes"])
        if pending_count >= self.policy.max_pending:
            raise DeliveryCapacityError(
                f"Pending delivery capacity exhausted: {pending_count}/"
                f"{self.policy.max_pending} records"
            )
        if pending_bytes + added_bytes > self.policy.max_pending_bytes:
            raise DeliveryCapacityError(
                f"Pending delivery byte capacity exhausted: {pending_bytes + added_bytes}/"
                f"{self.policy.max_pending_bytes} bytes"
            )

    def get(self, idempotency_key: str) -> DeliveryRecord | None:
        try:
            with self._connect() as conn:
                return self._get(conn, idempotency_key)
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot read delivery: {exc}") from exc

    def _get(self, conn: sqlite3.Connection, idempotency_key: str) -> DeliveryRecord | None:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return _record(row) if row else None

    def list(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[DeliveryRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM deliveries
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (*params, limit),
                ).fetchall()
                return [_record(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot list deliveries: {exc}") from exc

    def status(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status"
                ).fetchall()
                pending = conn.execute(
                    """
                    SELECT COUNT(*) AS count, COALESCE(SUM(prompt_bytes), 0) AS bytes
                    FROM deliveries WHERE status = 'pending'
                    """
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot inspect delivery status: {exc}") from exc
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {
            "path": str(self.path),
            "counts": {
                "pending": counts.get("pending", 0),
                "injected": counts.get("injected", 0),
                "dead": counts.get("dead", 0),
            },
            "pending": {
                "records": int(pending["count"]),
                "bytes": int(pending["bytes"]),
                "max_records": self.policy.max_pending,
                "max_bytes": self.policy.max_pending_bytes,
            },
            "max_prompt_bytes": self.policy.max_prompt_bytes,
            "max_attempts": self.policy.max_attempts,
        }

    def due(
        self,
        now: float | None = None,
        limit: int = 25,
        session_id: str | None = None,
    ) -> list[DeliveryRecord]:
        current = time.time() if now is None else now
        session_clause = "AND session_id = ?" if session_id else ""
        params: tuple[Any, ...] = (
            (current, session_id, limit) if session_id else (current, limit)
        )
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM deliveries
                    WHERE status = 'pending' AND next_attempt_at <= ? {session_clause}
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                return [_record(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot read due deliveries: {exc}") from exc

    def mark_injected(self, idempotency_key: str, now: float | None = None) -> DeliveryRecord:
        current = time.time() if now is None else now
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'injected', prompt = NULL, prompt_bytes = 0,
                        updated_at = ?, injected_at = ?, last_error = NULL
                    WHERE idempotency_key = ?
                    """,
                    (current, current, idempotency_key),
                )
                record = self._get(conn, idempotency_key)
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot mark delivery injected: {exc}") from exc
        if not record:
            raise DeliveryStoreError(f"Delivery not found: {idempotency_key}")
        return record

    def mark_failed(
        self, idempotency_key: str, error: str, now: float | None = None
    ) -> DeliveryRecord:
        current = time.time() if now is None else now
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                record = self._get(conn, idempotency_key)
                if not record:
                    raise DeliveryStoreError(f"Delivery not found: {idempotency_key}")
                attempts = record.attempt_count + 1
                if attempts >= self.policy.max_attempts:
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET status = 'dead', attempt_count = ?, updated_at = ?,
                            dead_lettered_at = ?, last_error = ?
                        WHERE idempotency_key = ?
                        """,
                        (attempts, current, current, _trim_error(error), idempotency_key),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET attempt_count = ?, updated_at = ?, next_attempt_at = ?,
                            last_error = ?
                        WHERE idempotency_key = ?
                        """,
                        (
                            attempts,
                            current,
                            current + self._backoff(attempts),
                            _trim_error(error),
                            idempotency_key,
                        ),
                    )
                conn.execute("COMMIT")
            record = self.get(idempotency_key)
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot mark delivery failed: {exc}") from exc
        if not record:
            raise DeliveryStoreError(f"Delivery not found: {idempotency_key}")
        return record

    def _backoff(self, attempts: int) -> float:
        delay = self.policy.base_backoff_seconds * (2 ** max(attempts - 1, 0))
        return min(delay, self.policy.max_backoff_seconds)

    def retry(self, idempotency_key: str, now: float | None = None) -> DeliveryRecord:
        current = time.time() if now is None else now
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                record = self._get(conn, idempotency_key)
                if not record:
                    raise DeliveryStoreError(f"Delivery not found: {idempotency_key}")
                if record.status == "injected":
                    conn.execute("COMMIT")
                    return record
                if record.prompt is None:
                    raise DeliveryStoreError(
                        "Cannot retry delivery without retained local prompt"
                    )
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'pending', attempt_count = 0, next_attempt_at = ?,
                        updated_at = ?, dead_lettered_at = NULL, last_error = NULL
                    WHERE idempotency_key = ?
                    """,
                    (current, current, idempotency_key),
                )
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot retry delivery: {exc}") from exc
        record = self.get(idempotency_key)
        if not record:
            raise DeliveryStoreError(f"Delivery not found: {idempotency_key}")
        return record

    def purge(self, keys: Iterable[str]) -> int:
        values = list(keys)
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"DELETE FROM deliveries WHERE idempotency_key IN ({placeholders})",
                    values,
                )
                return int(cursor.rowcount)
        except sqlite3.DatabaseError as exc:
            raise DeliveryStoreError(f"Cannot purge deliveries: {exc}") from exc


def make_record(
    *,
    session_id: str,
    message_type: str,
    prompt: str,
    delivery_id: str | None,
    correlation_id: str | None,
    from_peer: str | None,
    text: str | None,
    now: float | None = None,
) -> DeliveryRecord:
    current = time.time() if now is None else now
    return DeliveryRecord(
        idempotency_key=idempotency_key(
            session_id=session_id,
            message_type=message_type,
            delivery_id=delivery_id,
            correlation_id=correlation_id,
            from_peer=from_peer,
            text=text,
        ),
        session_id=session_id,
        message_type=message_type,
        status="pending",
        prompt=prompt,
        delivery_id=delivery_id,
        correlation_id=correlation_id,
        from_peer=from_peer,
        attempt_count=0,
        next_attempt_at=0,
        last_error=None,
        created_at=current,
        updated_at=current,
        injected_at=None,
        dead_lettered_at=None,
        prompt_bytes=len(prompt.encode("utf-8")),
    )


def to_dict(record: DeliveryRecord, *, include_prompt: bool = False) -> dict[str, Any]:
    data = {
        "idempotency_key": record.idempotency_key,
        "session_id": record.session_id,
        "message_type": record.message_type,
        "status": record.status,
        "delivery_id": record.delivery_id,
        "correlation_id": record.correlation_id,
        "from_peer": record.from_peer,
        "attempt_count": record.attempt_count,
        "next_attempt_at": record.next_attempt_at,
        "last_error": record.last_error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "injected_at": record.injected_at,
        "dead_lettered_at": record.dead_lettered_at,
        "prompt_bytes": record.prompt_bytes,
        "has_prompt": record.prompt is not None,
    }
    if include_prompt:
        data["prompt"] = record.prompt
    return data


def _record(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        idempotency_key=row["idempotency_key"],
        session_id=row["session_id"],
        message_type=row["message_type"],
        status=row["status"],
        prompt=row["prompt"],
        delivery_id=row["delivery_id"],
        correlation_id=row["correlation_id"],
        from_peer=row["from_peer"],
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=float(row["next_attempt_at"]),
        last_error=row["last_error"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        injected_at=float(row["injected_at"]) if row["injected_at"] is not None else None,
        dead_lettered_at=(
            float(row["dead_lettered_at"]) if row["dead_lettered_at"] is not None else None
        ),
        prompt_bytes=int(row["prompt_bytes"]),
    )


def _trim_error(error: str) -> str:
    return error[:2000]
