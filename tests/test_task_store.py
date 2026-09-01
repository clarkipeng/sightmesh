from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sightmesh.escalation import EscalationStore
from sightmesh.task_store import StaleTransition, TaskStore, TaskStoreError

LEGACY_DDL = """
    CREATE TABLE managed_tasks (
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


def _legacy_store(path, rows):
    """Materialize the pre-kernel schema so the migration has real work."""
    database = EscalationStore(path)
    with database._connect() as conn:
        conn.execute(LEGACY_DDL)
        conn.execute(
            "CREATE UNIQUE INDEX idx_managed_tasks_holder "
            "ON managed_tasks(holder_session_id) "
            "WHERE holder_session_id IS NOT NULL AND state IN "
            "('active', 'replacing', 'blocked')"
        )
        conn.execute(
            "CREATE INDEX idx_managed_tasks_parent "
            "ON managed_tasks(parent_task_id, created_at)"
        )
        now = time.time()
        for row in rows:
            conn.execute(
                "INSERT INTO managed_tasks VALUES "
                "(?, ?, ?, ?, ?, 1, 1, 3, 0, '{}', NULL, ?, NULL, NULL, ?, ?)",
                (*row, now, now),
            )
    return database


def _schema(database):
    with database._connect() as conn:
        return str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'managed_tasks'"
            ).fetchone()["sql"]
        )


def _reserve(store, key, *, parent_task_id=None, children=0):
    ((record, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=parent_task_id,
        specs=[{"key": key, "children": children}],
        max_attempts=3,
    )
    return record


def _active(store, key, *, parent_task_id=None, children=0):
    record = _reserve(store, key, parent_task_id=parent_task_id, children=children)
    return store.activate(
        record.task_id, workspace_id=f"ws-{key}", session_id=f"session-{key}"
    )


def test_migration_preserves_rows_and_runs_twice_without_effect(tmp_path):
    """The rebuild only exists to add a CHECK, so it must not lose history.

    Running it twice is the real operational case: every process that opens
    the store re-enters _initialize, and a second rebuild would churn rows
    and drop the partial holder index under live readers.
    """
    path = tmp_path / "state.sqlite3"
    database = _legacy_store(
        path, [("task-a", "operator", "one", None, "active", "session-a")]
    )

    TaskStore(path)
    once = _schema(database)
    TaskStore(path)
    twice = _schema(database)

    assert once == twice
    assert "parent_task_id != task_id" in once
    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM managed_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT version FROM managed_tasks").fetchone()[0] == 0
        indexes = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert {"idx_managed_tasks_holder", "idx_managed_tasks_parent"} <= indexes
        assert "managed_tasks_kernel_v1" not in {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def test_migration_repairs_a_legacy_self_parent_row(tmp_path):
    """A self-parent row predates the constraint that now forbids it.

    Failing the migration would strand the whole store, and deleting the row
    would lose a real task, so the impossible link is what gets dropped.
    """
    path = tmp_path / "state.sqlite3"
    database = _legacy_store(
        path, [("task-a", "operator", "one", "task-a", "active", "session-a")]
    )

    store = TaskStore(path)

    assert store.get("operator", "one").parent_task_id is None
    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM managed_tasks").fetchone()[0] == 1


def test_self_parentage_is_unrepresentable(tmp_path):
    """A task that is its own parent makes every predicate over children
    circular. The schema rejects it so no runtime check has to."""
    store = TaskStore(tmp_path / "state.sqlite3")
    record = _active(store, "manager", children=1)

    with store.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE managed_tasks SET parent_task_id = task_id WHERE task_id = ?",
            (record.task_id,),
        )


def test_terminal_states_are_immutable(tmp_path):
    """The first legal terminal transition wins.

    Without this, a late 'lost' from a reconciler could overwrite a real
    completion and resurrect work that already shipped.
    """
    store = TaskStore(tmp_path / "state.sqlite3")
    record = _active(store, "audit")
    completed = store.finish(record.task_id, "completed", "done")

    for state in ("blocked", "cancelled", "lost", "completed"):
        with pytest.raises(StaleTransition) as caught:
            store.finish(record.task_id, state, "later")
        assert caught.value.current.state == "completed"
    assert store.get_by_id(record.task_id).result == completed.result


def test_blocked_tasks_can_still_complete_but_reserved_ones_cannot(tmp_path):
    """The predecessor table is the whole legality rule; spot-check both
    directions so an edit to it cannot silently widen or narrow the graph."""
    store = TaskStore(tmp_path / "state.sqlite3")
    blocked = store.finish(_active(store, "one").task_id, "blocked", "needs input")
    assert store.finish(blocked.task_id, "completed", "unblocked").state == "completed"

    reserved = _reserve(store, "two")
    with pytest.raises(StaleTransition):
        store.finish(reserved.task_id, "completed", "never launched")


def test_a_stale_version_loses_the_transition(tmp_path):
    """A writer holding a pre-transfer snapshot must not win.

    This is the stale-epoch writer case: it observed the task before someone
    else moved it, so its write is based on facts that no longer hold.
    """
    store = TaskStore(tmp_path / "state.sqlite3")
    record = _active(store, "audit")
    store.checkpoint(record.task_id, "progress")

    with pytest.raises(StaleTransition) as caught:
        store.finish(
            record.task_id, "completed", "stale", expect_version=record.version
        )
    assert caught.value.current.version > record.version


def test_checkpoints_cannot_be_written_to_a_terminal_task(tmp_path):
    """A checkpoint is recovery state for a task that can still be resumed;
    writing one to a finished task would advertise a resume path that does
    not exist."""
    store = TaskStore(tmp_path / "state.sqlite3")
    record = _active(store, "audit")
    store.finish(record.task_id, "cancelled")

    with pytest.raises(StaleTransition):
        store.checkpoint(record.task_id, "too late")


def test_concurrent_finishes_produce_one_winner(tmp_path):
    """Two observers of the same worker exit must not both claim it."""
    store = TaskStore(tmp_path / "state.sqlite3")
    record = _active(store, "audit")

    def finish(state):
        try:
            return store.finish(record.task_id, state, state)
        except StaleTransition as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, ("completed", "lost")))

    assert sum(isinstance(item, StaleTransition) for item in outcomes) == 1
    assert store.get_by_id(record.task_id).state in {"completed", "lost"}


def test_finish_rejects_an_unknown_target_state(tmp_path):
    """The predecessor table is also the allow-list of finish targets."""
    store = TaskStore(tmp_path / "state.sqlite3")
    record = _active(store, "audit")

    with pytest.raises(ValueError, match="Unsupported task finish state"):
        store.finish(record.task_id, "archived")


def test_a_missing_task_is_not_reported_as_a_stale_transition(tmp_path):
    """StaleTransition carries a record; a task that never existed has none,
    so it must stay the plainer error callers already handle."""
    store = TaskStore(tmp_path / "state.sqlite3")

    with pytest.raises(TaskStoreError, match="not found"):
        store.finish("00000000-0000-0000-0000-000000000000", "completed")
