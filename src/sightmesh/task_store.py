from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .escalation import EscalationStore, escalation_db_path

TASK_NAMESPACE = uuid.UUID("620f9fa2-f939-4a9f-aed5-2a558f2ed107")


class TaskStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    scope: str
    key: str
    parent_task_id: str | None
    state: str
    epoch: int
    attempts: int
    max_attempts: int
    child_limit: int
    spec: dict[str, Any]
    workspace_id: str | None
    holder_session_id: str | None
    checkpoint: str | None
    result: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class ParentWake:
    dedupe_key: str
    parent_task_id: str
    task_id: str
    epoch: int
    state: str
    detail: str | None
    sender_session_id: str | None
    intent: str
    acknowledged_at: float | None


class TaskStore:
    """Persist semantic parentage and budgets cdesktop cannot reconstruct."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or escalation_db_path()
        self._database = EscalationStore(self.path)
        self._initialize()

    def _initialize(self) -> None:
        try:
            with self._database._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS managed_tasks (
                        task_id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL,
                        task_key TEXT NOT NULL,
                        parent_task_id TEXT,
                        state TEXT NOT NULL CHECK (state IN
                            ('reserved', 'active', 'replacing', 'blocked',
                             'completed', 'cancelled', 'lost')),
                        epoch INTEGER NOT NULL CHECK (epoch > 0),
                        attempts INTEGER NOT NULL CHECK (attempts > 0),
                        max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                        child_limit INTEGER NOT NULL CHECK (child_limit >= 0),
                        spec_json TEXT NOT NULL,
                        workspace_id TEXT,
                        holder_session_id TEXT,
                        checkpoint TEXT,
                        result TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(scope, task_key),
                        FOREIGN KEY(parent_task_id) REFERENCES managed_tasks(task_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_managed_tasks_holder "
                    "ON managed_tasks(holder_session_id) "
                    "WHERE holder_session_id IS NOT NULL AND state IN "
                    "('active', 'replacing', 'blocked')"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_managed_tasks_parent "
                    "ON managed_tasks(parent_task_id, created_at)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_parent_wakes (
                        dedupe_key TEXT PRIMARY KEY,
                        parent_task_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        epoch INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        detail TEXT,
                        sender_session_id TEXT,
                        intent TEXT NOT NULL CHECK (intent IN ('continue', 'replace')),
                        acknowledged_at REAL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_parent_wakes_pending "
                    "ON task_parent_wakes(parent_task_id, acknowledged_at)"
                )
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot initialize managed tasks: {exc}") from exc

    @staticmethod
    def task_id(scope: str, key: str) -> str:
        return str(uuid.uuid5(TASK_NAMESPACE, f"{scope}\0{key}"))

    def reserve_all(
        self,
        *,
        scope: str,
        parent_task_id: str | None,
        specs: list[dict[str, Any]],
        max_attempts: int,
    ) -> list[tuple[TaskRecord, bool]]:
        if not specs:
            return []
        keys = [str(spec["key"]) for spec in specs]
        if len(keys) != len(set(keys)):
            raise TaskStoreError("A start batch cannot contain duplicate task keys")
        now = time.time()
        try:
            with self._database._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if parent_task_id:
                    parent = conn.execute(
                        "SELECT child_limit FROM managed_tasks WHERE task_id = ?",
                        (parent_task_id,),
                    ).fetchone()
                    if parent is None:
                        raise TaskStoreError(
                            "The current managed parent no longer exists"
                        )
                    existing_children = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM managed_tasks WHERE parent_task_id = ?",
                            (parent_task_id,),
                        ).fetchone()[0]
                    )
                    new_keys = sum(
                        conn.execute(
                            "SELECT 1 FROM managed_tasks WHERE scope = ? AND task_key = ?",
                            (scope, key),
                        ).fetchone()
                        is None
                        for key in keys
                    )
                    if existing_children + new_keys > int(parent["child_limit"]):
                        raise TaskStoreError(
                            f"Task child limit is {parent['child_limit']}; "
                            f"this batch would create {existing_children + new_keys}"
                        )

                reserved: list[tuple[TaskRecord, bool]] = []
                for spec in specs:
                    key = str(spec["key"])
                    task_id = self.task_id(scope, key)
                    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"))
                    row = conn.execute(
                        "SELECT * FROM managed_tasks WHERE scope = ? AND task_key = ?",
                        (scope, key),
                    ).fetchone()
                    inserted = row is None
                    if row is None:
                        conn.execute(
                            """
                            INSERT INTO managed_tasks
                            (task_id, scope, task_key, parent_task_id, state, epoch,
                             attempts, max_attempts, child_limit, spec_json,
                             created_at, updated_at)
                            VALUES (?, ?, ?, ?, 'reserved', 1, 1, ?, ?, ?, ?, ?)
                            """,
                            (
                                task_id,
                                scope,
                                key,
                                parent_task_id,
                                max_attempts,
                                int(spec.get("children", 0)),
                                encoded,
                                now,
                                now,
                            ),
                        )
                        row = conn.execute(
                            "SELECT * FROM managed_tasks WHERE task_id = ?", (task_id,)
                        ).fetchone()
                    elif str(row["spec_json"]) != encoded:
                        raise TaskStoreError(
                            f"Task {key!r} already exists with a different specification"
                        )
                    reserved.append((self._decode(row), inserted))
                conn.execute("COMMIT")
                return reserved
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot reserve managed tasks: {exc}") from exc

    def get(self, scope: str, key: str) -> TaskRecord | None:
        return self._one("scope = ? AND task_key = ?", (scope, key))

    def get_by_id(self, task_id: str) -> TaskRecord | None:
        return self._one("task_id = ?", (str(task_id),))

    def get_by_session(self, session_id: str) -> TaskRecord | None:
        return self._one("holder_session_id = ?", (str(session_id),))

    def list_scope(self, scope: str) -> list[TaskRecord]:
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM managed_tasks WHERE scope = ? ORDER BY created_at",
                    (scope,),
                ).fetchall()
            return [self._decode(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot list managed tasks: {exc}") from exc

    def reconciliation_tasks(self, limit: int = 100) -> list[TaskRecord]:
        """Return the bounded, nonterminal set that needs native observation.

        This is intentionally an index-backed task-store query: the bridge must
        never discover managed work by walking every cdesktop workspace.
        """
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM managed_tasks
                    WHERE state IN ('active', 'replacing', 'blocked')
                      AND holder_session_id IS NOT NULL
                    ORDER BY updated_at, task_id LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [self._decode(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot list reconciliation tasks: {exc}") from exc

    def pending_wake_parent_ids(self, limit: int = 100) -> list[str]:
        """Return parents with unacknowledged wakes, once and in delivery order."""
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    """SELECT parent_task_id, MIN(rowid) AS first_wake
                    FROM task_parent_wakes WHERE acknowledged_at IS NULL
                    GROUP BY parent_task_id ORDER BY first_wake LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [str(row["parent_task_id"]) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot list pending parent wakes: {exc}") from exc

    def activate(
        self, task_id: str, *, workspace_id: str, session_id: str
    ) -> TaskRecord:
        return self._update(
            task_id,
            "state = 'active', workspace_id = ?, holder_session_id = ?, updated_at = ?",
            (str(workspace_id), str(session_id), time.time()),
        )

    def prepare_replacement(
        self, task_id: str, *, target: dict[str, Any] | None = None
    ) -> TaskRecord:
        try:
            with self._database._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
                if row is None:
                    raise TaskStoreError("Managed task not found")
                task = self._decode(row)
                if task.state == "replacing":
                    conn.execute("COMMIT")
                    return task
                if task.state not in {"active", "blocked"}:
                    raise TaskStoreError(
                        f"Task {task.key!r} cannot be replaced from {task.state}"
                    )
                if task.attempts >= task.max_attempts:
                    raise TaskStoreError(
                        f"Task {task.key!r} tripped its {task.max_attempts}-attempt "
                        "circuit breaker"
                    )
                spec = task.spec if target is None else {**task.spec, "target": target}
                conn.execute(
                    """
                    UPDATE managed_tasks
                    SET state = 'replacing', epoch = epoch + 1,
                        attempts = attempts + 1, spec_json = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        json.dumps(spec, sort_keys=True, separators=(",", ":")),
                        time.time(),
                        str(task_id),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
                conn.execute("COMMIT")
                return self._decode(row)
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot prepare task replacement: {exc}") from exc

    def checkpoint(self, task_id: str, checkpoint: str) -> TaskRecord:
        return self._update(
            task_id,
            "checkpoint = ?, updated_at = ?",
            (checkpoint, time.time()),
        )

    def finish(
        self,
        task_id: str,
        state: str,
        result: str | None = None,
        *,
        epoch: int | None = None,
    ) -> TaskRecord:
        """Make one legal state transition and its parent wake atomically.

        Terminal observations are fenced by epoch and only accepted from an
        active or blocked task.  Once terminal, every retry returns the first
        winner without changing it or adding a second terminal wake.
        """
        if state not in {"completed", "blocked", "cancelled", "lost"}:
            raise ValueError(f"Unsupported task finish state: {state}")
        terminal = state in {"completed", "cancelled", "lost"}
        try:
            with self._database._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
                if row is None:
                    raise TaskStoreError("Managed task not found")
                task = self._decode(row)
                expected_epoch = task.epoch if epoch is None else epoch
                if task.epoch != expected_epoch:
                    conn.execute("COMMIT")
                    return task
                allowed = ("active",) if state == "blocked" else ("active", "blocked")
                if task.state not in allowed:
                    conn.execute("COMMIT")
                    return task
                updated = conn.execute(
                    f"""UPDATE managed_tasks SET state = ?, result = ?, updated_at = ?
                    WHERE task_id = ? AND epoch = ? AND state IN ({', '.join('?' for _ in allowed)})""",
                    (state, result, time.time(), str(task_id), expected_epoch, *allowed),
                ).rowcount
                if updated and task.parent_task_id:
                    key = (
                        f"terminal-wake:{task.parent_task_id}:{task.task_id}:{task.epoch}"
                        if terminal
                        else f"task-blocked:{task.parent_task_id}:{task.task_id}:{task.epoch}"
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO task_parent_wakes
                        (dedupe_key, parent_task_id, task_id, epoch, state, detail,
                         sender_session_id, intent, acknowledged_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                        (
                            key,
                            task.parent_task_id,
                            task.task_id,
                            task.epoch,
                            state,
                            result,
                            task.holder_session_id,
                            "continue" if state == "completed" else "replace",
                        ),
                    )
                row = conn.execute(
                    "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
                conn.execute("COMMIT")
            return self._decode(row)
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot finish managed task: {exc}") from exc

    def pending_parent_wakes(self, parent_task_id: str) -> list[ParentWake]:
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM task_parent_wakes
                    WHERE parent_task_id = ? AND acknowledged_at IS NULL
                    ORDER BY rowid""",
                    (str(parent_task_id),),
                ).fetchall()
            return [self._decode_wake(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot read task parent wakes: {exc}") from exc

    def acknowledge_parent_wake(self, dedupe_key: str) -> bool:
        try:
            with self._database._connect() as conn:
                return bool(
                    conn.execute(
                        """UPDATE task_parent_wakes SET acknowledged_at = ?
                        WHERE dedupe_key = ? AND acknowledged_at IS NULL""",
                        (time.time(), dedupe_key),
                    ).rowcount
                )
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot acknowledge task parent wake: {exc}") from exc

    def _one(self, where: str, params: tuple[object, ...]) -> TaskRecord | None:
        try:
            with self._database._connect() as conn:
                row = conn.execute(
                    f"SELECT * FROM managed_tasks WHERE {where}", params
                ).fetchone()
            return self._decode(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot read managed task: {exc}") from exc

    def _update(
        self, task_id: str, assignment: str, values: tuple[object, ...]
    ) -> TaskRecord:
        try:
            with self._database._connect() as conn:
                cursor = conn.execute(
                    f"UPDATE managed_tasks SET {assignment} WHERE task_id = ?",
                    (*values, str(task_id)),
                )
                if cursor.rowcount != 1:
                    raise TaskStoreError("Managed task not found")
                row = conn.execute(
                    "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
            return self._decode(row)
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot update managed task: {exc}") from exc

    @staticmethod
    def _decode(row: sqlite3.Row) -> TaskRecord:
        try:
            return TaskRecord(
                task_id=str(row["task_id"]),
                scope=str(row["scope"]),
                key=str(row["task_key"]),
                parent_task_id=row["parent_task_id"],
                state=str(row["state"]),
                epoch=int(row["epoch"]),
                attempts=int(row["attempts"]),
                max_attempts=int(row["max_attempts"]),
                child_limit=int(row["child_limit"]),
                spec=json.loads(row["spec_json"]),
                workspace_id=row["workspace_id"],
                holder_session_id=row["holder_session_id"],
                checkpoint=row["checkpoint"],
                result=row["result"],
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStoreError(f"Corrupt managed task record: {exc}") from exc

    @staticmethod
    def _decode_wake(row: sqlite3.Row) -> ParentWake:
        try:
            return ParentWake(**dict(row))
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskStoreError(f"Corrupt task parent wake: {exc}") from exc
