from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .escalation import EscalationStore, escalation_db_path

TASK_NAMESPACE = uuid.UUID("620f9fa2-f939-4a9f-aed5-2a558f2ed107")

#: States a task can still leave under its own power.
LIVE_STATES = frozenset({"reserved", "active", "replacing", "blocked"})
#: Legal predecessors per finish target. ``completed``, ``cancelled`` and
#: ``lost`` appear in no set, which is what makes them immutable terminals:
#: the first legal terminal transition wins and every later one is stale.
FINISH_PREDECESSORS: dict[str, frozenset[str]] = {
    "completed": frozenset({"active", "replacing", "blocked"}),
    "blocked": frozenset({"active", "replacing"}),
    "cancelled": frozenset({"reserved", "active", "replacing", "blocked"}),
    "lost": frozenset({"reserved", "active", "replacing"}),
}
#: ``replacing`` is included because a replacement launch activates the
#: successor session it just spawned for the prepared epoch.
ACTIVATE_PREDECESSORS = frozenset({"reserved", "active", "replacing"})
REPLACE_PREDECESSORS = frozenset({"active", "blocked", "lost"})

_MANAGED_TASKS_DDL = """
    CREATE TABLE {name} (
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
        version INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(scope, task_key),
        CHECK (parent_task_id IS NULL OR parent_task_id != task_id),
        FOREIGN KEY(parent_task_id) REFERENCES managed_tasks(task_id)
    )
"""
_MANAGED_TASKS_COLUMNS = (
    "task_id",
    "scope",
    "task_key",
    "parent_task_id",
    "state",
    "epoch",
    "attempts",
    "max_attempts",
    "child_limit",
    "spec_json",
    "workspace_id",
    "holder_session_id",
    "checkpoint",
    "result",
    "version",
    "created_at",
    "updated_at",
)
_SELF_PARENT_CHECK = "parent_task_id != task_id"
_REBUILD_TABLE = "managed_tasks_kernel_v1"


class TaskStoreError(RuntimeError):
    pass


class StaleTransition(TaskStoreError):
    """A guarded transition observed a task the caller no longer describes.

    Carries the current record so a caller can decide between an idempotent
    no-op (the observed state already is the attempted target) and a real
    conflict (another writer won the race).
    """

    def __init__(self, current: TaskRecord, attempted: str) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(
            f"Task {current.key!r} is {current.state} at version {current.version}; "
            f"it cannot transition to {attempted}"
        )


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
    version: int
    created_at: float
    updated_at: float


class TaskStore:
    """Persist semantic parentage and budgets cdesktop cannot reconstruct."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or escalation_db_path()
        self._database = EscalationStore(self.path)
        self._initialize()

    def _initialize(self) -> None:
        try:
            with self._database._connect() as conn:
                self._migrate_managed_tasks(conn)
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
                    CREATE TABLE IF NOT EXISTS task_effects (
                        task_id TEXT NOT NULL,
                        epoch INTEGER NOT NULL,
                        request_hash TEXT NOT NULL,
                        state TEXT NOT NULL
                            CHECK (state IN ('reserved', 'launched', 'terminal')),
                        workspace_id TEXT,
                        session_id TEXT,
                        outcome TEXT,
                        owner_instance TEXT NOT NULL,
                        lease_expires_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (task_id, epoch)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_wakes (
                        wake_id TEXT PRIMARY KEY,
                        parent_task_id TEXT NOT NULL,
                        predicate TEXT NOT NULL CHECK (predicate IN
                            ('all_children_terminal', 'any_child_blocked')),
                        dedupe_key TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL CHECK (state IN
                            ('pending', 'claimed', 'delivered', 'resolved')),
                        claim_expires_at REAL,
                        payload TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_wakes_pending "
                    "ON task_wakes(state, created_at)"
                )
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot initialize managed tasks: {exc}") from exc

    @staticmethod
    def _migrate_managed_tasks(conn: sqlite3.Connection) -> None:
        """Bring ``managed_tasks`` to the kernel v1 shape, forward only.

        SQLite cannot add a CHECK constraint in place, so an existing table
        without the self-parent constraint or the ``version`` column is
        rebuilt. Running this twice is a no-op: the second pass sees both
        additions and returns before touching any row.
        """
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'managed_tasks'"
        ).fetchone()
        if existing is None:
            conn.execute(_MANAGED_TASKS_DDL.format(name="managed_tasks"))
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(managed_tasks)").fetchall()
        }
        has_version = "version" in columns
        if has_version and _SELF_PARENT_CHECK in str(existing["sql"]):
            return
        carried = ", ".join(
            name for name in _MANAGED_TASKS_COLUMNS if name != "parent_task_id"
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"DROP TABLE IF EXISTS {_REBUILD_TABLE}")
        conn.execute(_MANAGED_TASKS_DDL.format(name=_REBUILD_TABLE))
        conn.execute(
            f"INSERT INTO {_REBUILD_TABLE} (parent_task_id, {carried}) "
            # A legacy self-parent row cannot satisfy the new CHECK; keeping
            # the row and dropping the impossible link preserves history.
            "SELECT CASE WHEN parent_task_id = task_id THEN NULL "
            "ELSE parent_task_id END, "
            + ", ".join(
                ("version" if has_version else "0") if name == "version" else name
                for name in _MANAGED_TASKS_COLUMNS
                if name != "parent_task_id"
            )
            + " FROM managed_tasks"
        )
        conn.execute("DROP TABLE managed_tasks")
        conn.execute(f"ALTER TABLE {_REBUILD_TABLE} RENAME TO managed_tasks")
        conn.execute("COMMIT")

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

    def children(self, parent_task_id: str) -> list[TaskRecord]:
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM managed_tasks WHERE parent_task_id = ? "
                    "ORDER BY created_at",
                    (str(parent_task_id),),
                ).fetchall()
            return [self._decode(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot list managed children: {exc}") from exc

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Lend the store's connection so a caller can bundle its own writes.

        The wake outbox needs the child transition and the wake row in one
        transaction; without a shared connection they would be two.
        """
        with self._database._connect() as conn:
            yield conn

    def transition(
        self,
        task_id: str,
        *,
        expect_states: frozenset[str],
        expect_version: int | None,
        assign: str,
        values: tuple[object, ...],
        attempted: str,
        conn: sqlite3.Connection | None = None,
    ) -> TaskRecord:
        """Apply one state change only if the observed task still holds.

        Every mutation bumps ``version``, so a caller that read the row can
        prove nothing moved underneath it by passing ``expect_version``.
        """
        if conn is not None:
            return self._transition(
                conn, task_id, expect_states, expect_version, assign, values, attempted
            )
        try:
            with self._database._connect() as owned:
                return self._transition(
                    owned,
                    task_id,
                    expect_states,
                    expect_version,
                    assign,
                    values,
                    attempted,
                )
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot update managed task: {exc}") from exc

    def _transition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        expect_states: frozenset[str],
        expect_version: int | None,
        assign: str,
        values: tuple[object, ...],
        attempted: str,
    ) -> TaskRecord:
        states = sorted(expect_states)
        placeholders = ", ".join("?" for _ in states)
        cursor = conn.execute(
            f"UPDATE managed_tasks SET {assign}, version = version + 1, updated_at = ? "
            f"WHERE task_id = ? AND state IN ({placeholders}) "
            "AND (? IS NULL OR version = ?)",
            (
                *values,
                time.time(),
                str(task_id),
                *states,
                expect_version,
                expect_version,
            ),
        )
        row = conn.execute(
            "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
        ).fetchone()
        if row is None:
            raise TaskStoreError("Managed task not found")
        if cursor.rowcount != 1:
            raise StaleTransition(self._decode(row), attempted)
        return self._decode(row)

    def activate(
        self, task_id: str, *, workspace_id: str, session_id: str
    ) -> TaskRecord:
        return self.transition(
            task_id,
            expect_states=ACTIVATE_PREDECESSORS,
            expect_version=None,
            assign="state = 'active', workspace_id = ?, holder_session_id = ?",
            values=(str(workspace_id), str(session_id)),
            attempted="active",
        )

    def prepare_replacement(
        self,
        task_id: str,
        *,
        target: dict[str, Any] | None = None,
        expect_version: int | None = None,
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
                if task.attempts >= task.max_attempts:
                    raise TaskStoreError(
                        f"Task {task.key!r} tripped its {task.max_attempts}-attempt "
                        "circuit breaker"
                    )
                spec = task.spec if target is None else {**task.spec, "target": target}
                prepared = self.transition(
                    task_id,
                    expect_states=REPLACE_PREDECESSORS,
                    expect_version=expect_version,
                    assign=(
                        "state = 'replacing', epoch = epoch + 1, "
                        "attempts = attempts + 1, spec_json = ?"
                    ),
                    values=(json.dumps(spec, sort_keys=True, separators=(",", ":")),),
                    attempted="replacing",
                    conn=conn,
                )
                conn.execute("COMMIT")
                return prepared
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot prepare task replacement: {exc}") from exc

    def checkpoint(self, task_id: str, checkpoint: str) -> TaskRecord:
        return self.transition(
            task_id,
            expect_states=LIVE_STATES,
            expect_version=None,
            assign="checkpoint = ?",
            values=(checkpoint,),
            attempted="checkpoint",
        )

    def finish(
        self,
        task_id: str,
        state: str,
        result: str | None = None,
        *,
        expect_version: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TaskRecord:
        predecessors = FINISH_PREDECESSORS.get(state)
        if predecessors is None:
            raise ValueError(f"Unsupported task finish state: {state}")
        return self.transition(
            task_id,
            expect_states=predecessors,
            expect_version=expect_version,
            assign="state = ?, result = ?",
            values=(state, result),
            attempted=state,
            conn=conn,
        )

    def _one(self, where: str, params: tuple[object, ...]) -> TaskRecord | None:
        try:
            with self._database._connect() as conn:
                row = conn.execute(
                    f"SELECT * FROM managed_tasks WHERE {where}", params
                ).fetchone()
            return self._decode(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot read managed task: {exc}") from exc

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
                version=int(row["version"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStoreError(f"Corrupt managed task record: {exc}") from exc
