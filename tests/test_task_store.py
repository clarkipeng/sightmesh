from __future__ import annotations

import ast
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sightmesh.escalation import EscalationStore
from sightmesh.task_store import (
    StaleTransition,
    TaskStore,
    TaskStoreError,
)

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
    """Materialize an unsupported pre-kernel store to prove safe refusal."""
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


def test_test_store_guard_rejects_any_path_outside_its_tmp_root(tmp_path):
    """A test must fail before SQLite can initialize a non-isolated store (#123)."""
    implicit = TaskStore()
    assert implicit.path.is_relative_to(tmp_path)
    with pytest.raises(AssertionError, match="non-isolated TaskStore"):
        TaskStore(tmp_path.parent / "forbidden.sqlite3")


ROUND1_KERNEL_DDL = """
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
        version INTEGER NOT NULL DEFAULT 0,
        UNIQUE(scope, task_key),
        CHECK (parent_task_id IS NULL OR parent_task_id != task_id),
        FOREIGN KEY(parent_task_id) REFERENCES managed_tasks(task_id)
    )
"""


def _round1_kernel_store(path, rows):
    """An unsupported intermediate shape with no watermark or accounting marker."""
    database = EscalationStore(path)
    with database._connect() as conn:
        conn.execute(ROUND1_KERNEL_DDL)
        now = time.time()
        for row in rows:
            conn.execute(
                "INSERT INTO managed_tasks "
                "(task_id, scope, task_key, parent_task_id, state, epoch, "
                " attempts, max_attempts, child_limit, spec_json, "
                " holder_session_id, created_at, updated_at, version) "
                "VALUES (?, ?, ?, ?, ?, 1, 1, 3, 0, '{}', ?, ?, ?, 0)",
                (*row, now, now),
            )
    return database


@pytest.mark.parametrize("factory", [_legacy_store, _round1_kernel_store])
@pytest.mark.parametrize("self_parent", [False, True])
def test_unversioned_legacy_store_is_refused_without_rewriting_rows(tmp_path, factory, self_parent):
    """Unsupported saved formats remain untouched, never silently rebuilt/reset."""
    if self_parent and factory is _round1_kernel_store:
        # This schema already prevents self-parentage at insertion.
        with pytest.raises(sqlite3.IntegrityError):
            factory(tmp_path / "state.sqlite3", [("t", "operator", "one", "t", "active", "s")])
        return
    database = factory(
        tmp_path / "state.sqlite3",
        [("t", "operator", "one", "t" if self_parent else None, "active", "s")],
    )
    with database._connect() as conn:
        conn.execute("UPDATE managed_tasks SET attempts=3")
        before = list(conn.iterdump())
    for _ in range(2):
        with pytest.raises(TaskStoreError, match="Unversioned failure accounting"):
            TaskStore(database.path)
        with database._connect() as conn:
            assert list(conn.iterdump()) == before


def test_unversioned_cohort_is_not_backfilled_or_rewritten(tmp_path):
    """Refusal cannot fabricate a wake or consume an already-satisfied cohort."""
    database = _round1_kernel_store(tmp_path / "cohort.sqlite3", [
        ("p", "operator", "manager", None, "active", "session-p"),
        ("c1", "operator", "c1", "p", "completed", None),
        ("c2", "operator", "c2", "p", "completed", None),
    ])
    with database._connect() as conn:
        before = list(conn.iterdump())
    with pytest.raises(TaskStoreError, match="Unversioned failure accounting"):
        TaskStore(database.path)
    with database._connect() as conn:
        assert list(conn.iterdump()) == before


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


def test_state_transition_requires_a_fence_capability(tmp_path):
    """A new terminal writer cannot accidentally bypass the lifecycle fence."""
    store = TaskStore(tmp_path / "state.sqlite3")
    record = _active(store, "audit")

    with pytest.raises(TaskStoreError, match="require the task fence"):
        store.transition(
            record.task_id,
            expect_states=frozenset({"active"}),
            expect_version=record.version,
            assign="state = 'completed'",
            values=(),
            attempted="completed",
        )


def test_every_transition_caller_passes_the_fence_capability():
    """Keep every lifecycle writer structurally closed over the fence token."""
    source_root = Path(__file__).parents[1] / "src"
    offenders: list[str] = []
    # These are the modules that own or invoke TaskStore's lifecycle API.
    # ``approvals_commands`` has a different ``finish`` API, so method-name
    # matching across every source file would be a false structural claim.
    lifecycle_modules = ("task_store.py", "wakes.py", "sdk.py", "durable.py", "effects.py")
    for name in lifecycle_modules:
        path = source_root / "sightmesh" / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in {"transition", "finish", "activate", "prepare_replacement"}:
                continue
            if not any(keyword.arg == "fence" for keyword in node.keywords):
                offenders.append(f"{path.relative_to(source_root)}:{node.lineno}")
    assert offenders == []


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


def test_fresh_liveness_columns_start_at_safe_defaults(tmp_path):
    """A task never observed by the detector must not invent a finding."""
    path = tmp_path / "liveness.sqlite3"
    store = TaskStore(path)
    task = _active(store, "manager")
    assert (task.liveness, task.liveness_episode, task.liveness_wakes) == ("live", 0, 0)
    assert (task.liveness_since, task.liveness_evidence, task.checkpoint_at) == (None, None, None)
    assert task.over_budget is False
    assert TaskStore(path).get_by_id(task.task_id) == task


def test_liveness_column_rejects_an_unknown_classification(tmp_path):
    """The schema enforces the detector's typed vocabulary."""
    store = TaskStore(tmp_path / "liveness.sqlite3")
    _active(store, "manager")
    with pytest.raises(sqlite3.IntegrityError), store.connect() as conn:
        conn.execute("UPDATE managed_tasks SET liveness = 'wedged'")


def test_the_wake_predicate_check_widens_for_the_liveness_predicates(tmp_path):
    """SQLite cannot alter a CHECK in place, so the widened `predicate` set is a
    table rebuild - and a rebuild that dropped rows would lose undelivered
    wakes on upgrade, which is precisely the crash gap the outbox exists to
    close.
    """
    path = tmp_path / "wakes-widen.sqlite3"
    database = EscalationStore(path)
    with database._connect() as conn:
        # A kernel-v1 `task_wakes`: watermark column present, narrow CHECK.
        conn.execute(
            """
            CREATE TABLE task_wakes (
                wake_id TEXT PRIMARY KEY,
                parent_task_id TEXT NOT NULL,
                predicate TEXT NOT NULL CHECK (predicate IN
                    ('all_children_terminal', 'any_child_blocked')),
                dedupe_key TEXT NOT NULL,
                event_seq INTEGER,
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
            "INSERT INTO task_wakes (wake_id, parent_task_id, predicate, dedupe_key, "
            "state, created_at, updated_at) "
            "VALUES ('w1', 'p', 'any_child_blocked', 'p:any_child_blocked', "
            "'pending', 1, 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_wakes (wake_id, parent_task_id, predicate, "
                "dedupe_key, state, created_at, updated_at) "
                "VALUES ('w0', 'p', 'any_child_stalled', 'k0', 'pending', 1, 1)"
            )

    store = TaskStore(path)  # opening rebuilds the table with the wider CHECK

    with store._database._connect() as conn:
        # The undelivered pre-upgrade wake survived the rebuild.
        assert conn.execute("SELECT COUNT(*) FROM task_wakes").fetchone()[0] == 1
        conn.execute(
            "INSERT INTO task_wakes (wake_id, parent_task_id, predicate, dedupe_key, "
            "state, created_at, updated_at) "
            "VALUES ('w2', 'p', 'any_child_stalled', 'p:1:stalled:1', 'pending', 1, 1)"
        )
        # ...and a nonsense predicate is still refused, so the widening did not
        # degenerate into dropping the constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_wakes (wake_id, parent_task_id, predicate, "
                "dedupe_key, state, created_at, updated_at) "
                "VALUES ('w3', 'p', 'any_child_confused', 'k3', 'pending', 1, 1)"
            )


def _one_task(tmp_path, key="child", **spec):
    store = TaskStore(tmp_path / "state.sqlite3")
    ((record, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": key, "children": 0, **spec}],
        max_attempts=3,
    )
    return store, store.activate(
        record.task_id, workspace_id="ws", session_id=f"s-{key}"
    )


def test_a_reason_flip_inside_one_silence_is_not_a_new_episode(tmp_path):
    """Why (the unbounded-wake bug): any classification change used to count as
    an episode boundary, and an episode boundary resets `liveness_wakes` to
    zero. One flaky snapshot read is enough to flip a silent task between
    `stalled` and `limbo`, so a wedged child minted a fresh episode on every
    tick, emitted a wake on every tick, and never reached the two-wake
    escalation that hands the incident to a human. An episode ends when
    progress resumes; nothing else."""
    store, task = _one_task(tmp_path)
    store.record_liveness(task.task_id, "stalled", evidence="{}", now=100.0)
    opened = store.get_by_id(task.task_id)
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE managed_tasks SET liveness_wakes = 1 WHERE task_id = ?",
            (task.task_id,),
        )
        conn.execute("COMMIT")

    for reason in ("limbo", "stalled", "idle_unreported", "limbo"):
        store.record_liveness(task.task_id, reason, evidence="{}", now=200.0)

    flipped = store.get_by_id(task.task_id)
    assert flipped.liveness == "limbo", "the reason itself is allowed to change"
    assert flipped.liveness_episode == opened.liveness_episode
    assert flipped.liveness_since == opened.liveness_since, (
        "the phase clock is untouched"
    )
    assert flipped.liveness_wakes == 1, "the escalation counter must survive a relabel"


def test_progress_closes_the_episode_and_the_next_silence_opens_a_new_one(tmp_path):
    """The other half of the same rule: the boundary that does exist has to
    keep existing, or a child that stalls, recovers, and stalls again would
    collapse into one incident and its manager would never hear about the
    second."""
    store, task = _one_task(tmp_path)
    store.record_liveness(task.task_id, "stalled", evidence="{}", now=100.0)
    store.record_liveness(task.task_id, "live", now=200.0)
    recovered = store.get_by_id(task.task_id)
    assert (recovered.liveness, recovered.liveness_since) == ("live", None)
    assert recovered.liveness_wakes == 0

    store.record_liveness(task.task_id, "stalled", evidence="{}", now=300.0)
    assert store.get_by_id(task.task_id).liveness_episode == 2


def test_parking_starts_its_own_phase_clock(tmp_path):
    """Why: the approval timeout is measured from `liveness_since`. If parking
    shared the preceding silence's clock, a task that went quiet and *then*
    parked on an approval would be timed out into blocked(approval) the
    instant it parked - reporting a decision nobody had waited for."""
    store, task = _one_task(tmp_path)
    store.record_liveness(task.task_id, "stalled", evidence="{}", now=100.0)
    store.record_liveness(task.task_id, "parked", evidence="{}", now=900.0)
    assert store.get_by_id(task.task_id).liveness_since == 900.0


def test_a_terminal_task_can_neither_be_reclassified_nor_flagged(tmp_path):
    """Why: a detector tick and a task's own terminal write race by nature. A
    late tick that lands after the terminal must not annotate a finished task
    with a stall finding or a budget flag - both surface to a human as an
    incident about a worker that is not there any more."""
    store, task = _one_task(tmp_path)
    store.finish(task.task_id, "completed", "done")

    store.record_liveness(task.task_id, "stalled", evidence="{}", now=100.0)
    assert store.get_by_id(task.task_id).liveness == "live"
    assert store.record_over_budget(task.task_id) is False
    assert store.get_by_id(task.task_id).over_budget is False
