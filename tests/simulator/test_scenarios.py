"""Adversarial kernel-contract scenarios S1-S12 (docs/kernel-spec.md, "Simulator").

Each test replays one historical incident from the pre-kernel-v1 codebase and
pins the contract behavior kernel v1 is required to hold. Scenarios that
depend on a primitive kernel v1 has not built yet (``task_effects``,
``task_wakes``, ``StaleTransition``) fail cleanly through
``fail_missing_kernel_v1`` rather than erroring out of collection, so the
suite runs to completion red today and needs no edits to go green once the
transplant lands (see ``RED-BASELINE.md`` for the current run).

These tests exercise the real ``TaskStore`` and ``SightMesh`` SDK wired to
``FakeCdesktop`` - never mocks of the store itself.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from sightmesh.cdesktop import CdesktopRejectedError
from sightmesh.durable import DurableExecutionReconciler
from sightmesh.effects import EffectJournal, request_hash
from sightmesh.sdk import BatchError, SightMesh
from sightmesh.task_store import (
    _MANAGED_TASKS_COLUMNS,
    _MANAGED_TASKS_DDL,
    _REBUILD_TABLE,
    TaskStore,
    TaskStoreError,
)
from sightmesh.wakes import WakeDelivery, finish_with_wake

from .conftest import fail_missing_kernel_v1, make_mesh, query, table_exists, worker_spec
from .fake_cdesktop import SimulatedCrash

pytestmark = pytest.mark.simulator

FORENSICS_SNAPSHOT = (
    Path.home()
    / "Documents"
    / "sightmesh-forensics-2026-09-01"
    / "escalations.sqlite3"
)

_LEGACY_MANAGED_TASKS_DDL = """
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


def _manager(store: TaskStore, *, children: int) -> object:
    ((parent, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "manager", "children": children}],
        max_attempts=3,
    )
    return store.activate(
        parent.task_id, workspace_id="ws-manager", session_id="session-manager"
    )


def _reserve_child(store: TaskStore, parent_task_id: str, key: str, session: str):
    ((child, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=parent_task_id,
        specs=[{"key": key, "children": 0}],
        max_attempts=3,
    )
    return store.activate(
        child.task_id, workspace_id=f"ws-{key}", session_id=session
    )


def _rebuild_managed_tasks_kernel_v1(conn: sqlite3.Connection) -> None:
    """Stand in for a peer process P1 that completes the kernel-v1 rebuild.

    Produces the exact table SQL the production migration checks for, so a
    second initializer reading under its own lock recognizes the schema as
    already migrated and returns without touching a row.
    """
    carried = ", ".join(
        name
        for name in _MANAGED_TASKS_COLUMNS
        if name not in ("parent_task_id", "version")
    )
    conn.execute(f"DROP TABLE IF EXISTS {_REBUILD_TABLE}")
    conn.execute(_MANAGED_TASKS_DDL.format(name=_REBUILD_TABLE))
    conn.execute(
        f"INSERT INTO {_REBUILD_TABLE} (parent_task_id, version, {carried}) "
        f"SELECT parent_task_id, 0, {carried} FROM managed_tasks"
    )
    conn.execute("DROP TABLE managed_tasks")
    conn.execute(f"ALTER TABLE {_REBUILD_TABLE} RENAME TO managed_tasks")


def test_s1_duplicate_late_complete_on_a_blocked_task(mesh: SightMesh) -> None:
    """S1: a duplicate, late-arriving completion report must never overwrite
    an already-terminal task's recorded result.

    Historical incident: a retried/duplicate completion delivery (the
    at-least-once norm for any network call) landed after the task had
    already reached its terminal state, and the old task_store's blind
    ``UPDATE managed_tasks SET state = ?, result = ? WHERE task_id = ?`` (no
    state or version guard at all) overwrote the *original* recorded result
    with whatever text came along on the duplicate. A late duplicate must
    resolve to a no-op, never a second write.
    """
    mesh.start(worker_spec())
    mesh.blocked("waiting on review", worker="audit")

    first = mesh.complete("original summary", worker="audit")
    duplicate = mesh.complete("late duplicate summary", worker="audit")

    assert first.state == "completed"
    assert duplicate.state == "completed"
    assert duplicate.result == first.result == "original summary"


def test_s2_crash_between_state_change_and_notify_is_impossible_by_construction(
    client, store: TaskStore, ownership
) -> None:
    """S2: a crash between a child's terminal state transition and the
    parent notification must never lose the notification.

    Historical incident: ``finish()`` and ``_notify_parent()`` (sdk.py
    complete/blocked) are two separate, non-atomic steps with no reconciler
    watching the gap; a crash between them silently dropped the parent's
    wake forever. Kernel v1 commits the child's terminal transition and the
    wake-row insert in one transaction (docs/kernel-spec.md, "Wake outbox"),
    so the row survives any crash for a reconciler to deliver later.
    """
    if not table_exists(store, "task_wakes"):
        fail_missing_kernel_v1("task_wakes (atomic wake outbox) does not exist yet")

    parent_mesh = make_mesh(client, store, ownership)
    parent = parent_mesh.start(worker_spec("manager", children=1))
    child_mesh = make_mesh(client, store, ownership, session_id=parent.session_id)
    child_mesh.start(worker_spec("child"))

    parent_record = store.get("operator", "manager")
    assert parent_record is not None

    client.kill_after("notify")
    with pytest.raises(SimulatedCrash):
        child_mesh.complete("done", worker="child")

    child = store.get(parent_record.task_id, "child")
    assert child is not None and child.state == "completed"

    dedupe_key = f"{parent_record.task_id}:all_children_terminal"
    rows = query(
        store,
        "SELECT state FROM task_wakes WHERE dedupe_key = ?",
        (dedupe_key,),
    )
    assert len(rows) == 1
    assert rows[0]["state"] in {"pending", "claimed", "delivered"}


def test_s3_kill_between_native_launch_and_activation_retries_onto_the_same_effect(
    client, store: TaskStore
) -> None:
    """S3: a crash between the native launch succeeding and the task's
    activation persisting must retry onto the *same* effect, never fork a
    second native session.

    Historical incident: nothing durably reserved a (task, epoch) pair
    before calling the executor, so the only defense against a duplicate
    native session on retry was the executor's own idempotency - the kernel
    side had no fencing of its own. Kernel v1's effects journal
    (docs/kernel-spec.md, "Effects journal") reserves the pair first, so a
    retry after any crash adopts the existing reservation.
    """
    if not table_exists(store, "task_effects"):
        fail_missing_kernel_v1("task_effects (effects journal) does not exist yet")
    try:
        from sightmesh.effects import EffectJournal
    except ImportError:
        fail_missing_kernel_v1("sightmesh.effects.EffectJournal does not exist yet")
        return

    journal = EffectJournal(store)
    task_id, epoch, owner = "task-3", 1, "owner-a"
    request_hash = "deadbeef"

    reserved, _took_over = journal.reserve(task_id, epoch, request_hash, owner)
    assert reserved.state == "reserved"

    client.kill_after("activate")  # native launch succeeds; mark_launched never runs
    with pytest.raises(SimulatedCrash):
        client.managed_launch(task_id, epoch, {"kind": "workspace"})

    # Retry after the simulated crash: the reservation is still there to adopt.
    retried, _took_over_again = journal.reserve(task_id, epoch, request_hash, owner)
    assert retried.state in {"reserved", "launched"}

    launched_effect = client.managed_launch(task_id, epoch, {"kind": "workspace"})
    journal.mark_launched(
        task_id, epoch, launched_effect["workspace_id"], launched_effect["session_id"]
    )

    final = journal.get(task_id, epoch)
    assert final is not None
    assert final.workspace_id == launched_effect["workspace_id"]
    assert final.session_id == launched_effect["session_id"]
    assert len(client.distinct_effects()) == 1


def test_s4_a_hundred_concurrent_starts_on_one_key_launch_exactly_once(
    client, store: TaskStore, ownership
) -> None:
    """S4: 100 concurrent `start()` calls for the same semantic key must
    result in exactly one native launch.

    Multiple manager instances (or simple client retries) can legitimately
    race a `start()` for the same key; `reserve_all`'s `BEGIN IMMEDIATE`
    plus the executor's create-or-return launch contract
    (docs/kernel-contract.md, "Executor seam") are the two layers that must
    hold this to exactly one native session.
    """
    workers = 100
    barrier = threading.Barrier(workers)

    def start_once(_index: int):
        local_mesh = make_mesh(client, store, ownership)
        barrier.wait(timeout=10)
        return local_mesh.start(worker_spec())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(start_once, range(workers)))

    assert all(result.state == "active" for result in results)
    assert len({(result.workspace_id, result.session_id) for result in results}) == 1
    assert len(client.distinct_effects()) == 1


def test_s5_a_hundred_duplicate_child_completions_produce_one_parent_wake(
    client, store: TaskStore, ownership
) -> None:
    """S5: any number of child-completion events for one cohort must
    collapse into exactly one parent wake, never one mail per child.

    Historical incident: `_notify_parent` sent one machine-mail message per
    child completion. docs/kernel-contract.md's Mailbox clause is explicit
    that "the cohort produces wakes, not per-child mail" - a cohort of
    children completing fanned the parent's mailbox out into one message per
    event instead of one consolidated wake.
    """
    parent_mesh = make_mesh(client, store, ownership)
    parent = parent_mesh.start(worker_spec("manager", children=100))
    child_mesh = make_mesh(client, store, ownership, session_id=parent.session_id)

    for index in range(100):
        child_mesh.start(worker_spec(f"child-{index}"))
    for index in range(100):
        child_mesh.complete("done", worker=f"child-{index}")

    sent_to_parent = [row for row in client.sent if row[0] == parent.session_id]
    assert len(sent_to_parent) == 1


def test_s6_one_child_blocking_mid_cohort_survives_with_intent_continue(
    client, store: TaskStore, ownership
) -> None:
    """S6: when one child in a cohort blocks, the parent's wake delivery
    must use `intent="continue"`, never `intent="replace"`.

    Historical incident, named directly in docs/kernel-spec.md ("Wake
    outbox"): `_notify_parent` sent `intent="continue" if state ==
    "completed" else "replace"`, so a blocked child preempted the parent's
    own turn (`intent="replace"`) instead of letting it continue. "The
    known live bug (intent='replace' on blocked) disappears with it."
    """
    parent_mesh = make_mesh(client, store, ownership)
    parent = parent_mesh.start(worker_spec("manager", children=3))
    child_mesh = make_mesh(client, store, ownership, session_id=parent.session_id)
    child_mesh.start(worker_spec("child-a"))
    child_mesh.start(worker_spec("child-b"))
    child_mesh.start(worker_spec("child-c"))

    child_mesh.blocked("needs a human decision", worker="child-a")

    sent_to_parent = [row for row in client.sent if row[0] == parent.session_id]
    assert len(sent_to_parent) == 1
    assert sent_to_parent[0][4] == "continue"


def test_s7_self_parent_insert_is_unrepresentable(store: TaskStore) -> None:
    """S7: a task row that names itself as its own parent must be
    unrepresentable by the schema itself, not merely rejected by request-time
    ordering in one call path.

    Kernel v1 adds ``CHECK (parent_task_id IS NULL OR parent_task_id !=
    task_id)`` to `managed_tasks` (docs/kernel-spec.md, "Schema"). Today
    nothing enforces that at the database level - `sqlite3` connections here
    never turn `PRAGMA foreign_keys` on, so even the existing `FOREIGN KEY`
    reference is unenforced, and a direct self-parent insert succeeds
    silently.
    """
    task_id = TaskStore.task_id("operator", "self-parent")
    now = time.time()

    with pytest.raises(sqlite3.IntegrityError):
        with store._database._connect() as conn:  # noqa: SLF001 - probing schema invariants directly
            conn.execute(
                """
                INSERT INTO managed_tasks
                (task_id, scope, task_key, parent_task_id, state, epoch,
                 attempts, max_attempts, child_limit, spec_json,
                 created_at, updated_at)
                VALUES (?, 'operator', 'self-parent', ?, 'reserved', 1, 1, 3, 0, '{}', ?, ?)
                """,
                (task_id, task_id, now, now),
            )


def test_s8_stale_epoch_writer_after_transfer_is_rejected(store: TaskStore) -> None:
    """S8: a writer still holding a task's pre-transfer version must be
    rejected with `StaleTransition`, never silently applied on top of a
    transfer that already happened underneath it.

    Historical incident: no transition carried the version it observed
    (docs/kernel-contract.md: "Every mutation carries the task version it
    observed and fails on mismatch"), so a stale writer that read the task
    before a `replace()` bumped its epoch could blindly overwrite state a
    newer writer had already moved past.
    """
    try:
        from sightmesh.task_store import StaleTransition
    except ImportError:
        fail_missing_kernel_v1("sightmesh.task_store.StaleTransition does not exist yet")
        return
    if not hasattr(store, "transition"):
        fail_missing_kernel_v1("TaskStore.transition guarded helper does not exist yet")
        return

    reserved, _inserted = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "stale-writer", "repo": "project", "base": "main"}],
        max_attempts=3,
    )[0]
    activated = store.activate(reserved.task_id, workspace_id="ws-1", session_id="sess-1")
    stale_version = getattr(activated, "version", 0)

    store.prepare_replacement(activated.task_id)  # bumps epoch/version underneath the stale writer

    with pytest.raises(StaleTransition):
        store.transition(
            activated.task_id,
            expect_states=frozenset({"active"}),
            expect_version=stale_version,
            assign="state = 'completed', result = ?",
            values=("stale write",),
            attempted="completed",
        )


def test_s9_child_budget_exceeded_is_rejected_at_reserve(
    client, store: TaskStore, ownership
) -> None:
    """S9: exceeding a task's declared child budget must be rejected at
    `reserve`, before any native launch, never admitted and cleaned up after
    the fact.
    """
    parent_mesh = make_mesh(client, store, ownership)
    parent = parent_mesh.start(worker_spec("manager", children=1))
    child_mesh = make_mesh(client, store, ownership, session_id=parent.session_id)
    child_mesh.start(worker_spec("first"))

    launches_before = len(client.calls("managed_launch"))
    with pytest.raises(TaskStoreError, match="child limit is 1"):
        child_mesh.start(worker_spec("second"))
    assert len(client.calls("managed_launch")) == launches_before


def test_s10_typed_429_is_recorded_as_a_typed_outcome_never_inferred_from_text(
    mesh: SightMesh, client, store: TaskStore
) -> None:
    """S10: a typed HTTP 429 from the executor must be recorded as a typed
    terminal outcome on the task's effect, never something a caller has to
    detect by pattern-matching the error message.

    Historical incident: `start_all` catches every launch failure and
    collapses it to `str(exc)` in `BatchResult.errors` (sdk.py), discarding
    `CdesktopRejectedError`'s typed `status` field entirely - a caller
    wanting to distinguish "rate limited" from "really failed" is left
    grepping the error text.
    """
    client.rate_limit_after("launch")

    with pytest.raises(BatchError):
        mesh.start(worker_spec())

    if not table_exists(store, "task_effects"):
        fail_missing_kernel_v1(
            "task_effects (effects journal) does not exist yet to record a "
            "typed 429 outcome"
        )

    task = store.get("operator", "audit")
    assert task is not None
    from sightmesh.effects import EffectJournal

    journal = EffectJournal(store)
    effect = journal.get(task.task_id, task.epoch)
    assert effect is not None
    assert effect.outcome in {"quota", "rate_limited"}


def test_s11_show_with_a_thousand_terminal_tasks_performs_zero_fleet_scans(
    client, store: TaskStore, mesh: SightMesh
) -> None:
    """S11: `show(key)` must read task-local state with zero fleet scan,
    regardless of how many other tasks exist (docs/kernel-contract.md,
    "Observability": "show <task_key> reads task-local state with zero Git
    fan-out").
    """
    specs = [
        {"key": f"terminal-{index}", "repo": "project", "base": "main", "children": 0}
        for index in range(1000)
    ]
    reservations = store.reserve_all(
        scope="operator", parent_task_id=None, specs=specs, max_attempts=3
    )
    for record, _inserted in reservations:
        # 'cancelled' is the legal terminal from 'reserved'; guarded
        # transitions correctly refuse reserved -> completed.
        store.finish(record.task_id, "cancelled")

    before = len(client.call_log)
    mesh.show("terminal-500")
    after = len(client.call_log)

    assert after == before


def test_s12_two_concurrent_replace_on_one_task_yield_one_winner_one_stale(
    store: TaskStore,
) -> None:
    """S12: two managers racing a `replace()` on the same task must produce
    exactly one winner and one `StaleTransition`, never two silently
    "successful" epoch bumps for one failure.

    Historical incident: `prepare_replacement`'s only guard was `BEGIN
    IMMEDIATE` serialization plus an early return on `state == 'replacing'`
    that looks idempotent but is not - a genuinely concurrent second caller
    lands on that early return and walks away believing *its own* replace
    succeeded, when a different caller actually won and bumped the epoch out
    from under it (see `test_duplicate_failover_wakeups_reserve_one_successor_epoch`
    in tests/test_sdk.py, which pins today's two-winners behavior).
    """
    try:
        from sightmesh.task_store import StaleTransition
    except ImportError:
        fail_missing_kernel_v1("sightmesh.task_store.StaleTransition does not exist yet")
        return

    reserved, _inserted = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "racer", "repo": "project", "base": "main"}],
        max_attempts=5,
    )[0]
    store.activate(reserved.task_id, workspace_id="ws-1", session_id="sess-1")

    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def race(_index: int) -> None:
        barrier.wait(timeout=10)
        try:
            results.append(store.prepare_replacement(reserved.task_id))
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(race, range(2)))

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], StaleTransition)


def test_s18_a_second_cohort_transition_re_arms_the_wake(client, store, ownership):
    """S18 (F1): wakes never re-arm.

    Review finding F1: `dedupe_key = {parent}:{parent_epoch}:{predicate}` under a
    lifetime-UNIQUE index means a manager wakes once per predicate per epoch,
    ever, so a multi-wave manager hangs after wave one. Uniqueness must bind
    only live wakes, so a delivered wake's key arms again for the next cohort
    transition. (Fails today: the second `record_wakes` returns [], pump 0.)
    """
    parent = _manager(store, children=4)
    a1 = _reserve_child(store, parent.task_id, "a1", "session-a1")
    a2 = _reserve_child(store, parent.task_id, "a2", "session-a2")
    finish_with_wake(store, a1.task_id, "completed", "one")
    _record, first_wave = finish_with_wake(store, a2.task_id, "completed", "two")
    assert len(first_wave) == 1

    # WakeDelivery's default ownership is a real (never-retired) store; the
    # manager session is deliverable, so the wake leaves the outbox.
    delivery = WakeDelivery(client, store)
    assert delivery.pump() == 1

    # Wave two: dispatch and complete two more children of the same parent
    # epoch. The predicate goes false then true again, a genuinely new cohort
    # transition that must re-arm the wake (the delivered wave-one row no longer
    # blocks the dedupe key). Pre-fix, this second record_wakes returns [].
    a3 = _reserve_child(store, parent.task_id, "a3", "session-a3")
    a4 = _reserve_child(store, parent.task_id, "a4", "session-a4")
    finish_with_wake(store, a3.task_id, "completed", "three")
    _record2, second_wave = finish_with_wake(store, a4.task_id, "completed", "four")
    assert len(second_wave) == 1

    assert delivery.pump() == 1
    sent_to_parent = [row for row in client.sent if row[0] == parent.holder_session_id]
    assert len(sent_to_parent) == 2


def test_s19_a_terminal_effect_is_a_launch_barrier(store):
    """S19 (F2): terminal effect is not a launch barrier.

    Review finding F2: `reserve()` folded `launched` and `terminal` into
    `(existing, False)` and `_advance` never checked its rowcount, so a
    terminal epoch could relaunch and `mark_launched` silently no-op. A second
    `reserve()` on a terminal `(task, epoch)` must raise `EffectTerminal`, and
    `mark_launched` on a terminal row must raise, not no-op.
    """
    try:
        from sightmesh.effects import EffectTerminal
    except ImportError:
        fail_missing_kernel_v1("sightmesh.effects.EffectTerminal does not exist yet")
        return

    journal = EffectJournal(store)
    launch = {"kind": "workspace", "request": {"name": "t19"}}
    journal.reserve("t19", 1, request_hash(launch), "owner-a")
    journal.mark_launched("t19", 1, "ws-19", "sess-19")
    journal.mark_terminal("t19", 1, "completed")

    with pytest.raises(EffectTerminal):
        journal.reserve("t19", 1, request_hash(launch), "owner-a")

    with pytest.raises(TaskStoreError):
        journal.mark_launched("t19", 1, "ws-19", "sess-19")


def test_s20_expiry_adopts_an_in_flight_launch(client, store):
    """S20 (F3): expiry buries an in-flight launch.

    Review finding F3: `expire_reservations` marked any past-lease reservation
    `lost:reservation-expired` using only `session_id IS NULL`, orphaning a
    live native session created inside the 15s launch window. Expiry must
    adopt-or-lose: reserve, simulate a native session created but never marked,
    advance the clock past the lease, run expiry -> the effect ends `launched`
    (adopted), the task is active, and nothing is lost.
    """
    ((task, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "orphaned", "children": 0}],
        max_attempts=3,
    )
    journal = EffectJournal(store)
    launch = {"kind": "workspace", "request": {"name": "orphaned"}}
    # ttl < 0 makes the lease already expired the moment it is taken.
    journal.reserve(task.task_id, task.epoch, request_hash(launch), "owner-a", ttl=-1.0)
    client.create_native_session(
        task.task_id, task.epoch, workspace_id="ws-x", session_id="sess-x"
    )

    lost = journal.expire_reservations(client)

    assert lost == []
    effect = journal.get(task.task_id, task.epoch)
    assert effect is not None
    assert effect.state == "launched"
    assert (effect.workspace_id, effect.session_id) == ("ws-x", "sess-x")
    assert effect.outcome is None
    adopted = store.get_by_id(task.task_id)
    assert adopted is not None
    assert adopted.state == "active"
    assert adopted.holder_session_id == "sess-x"


def test_s21_terminal_outranks_conflict_when_the_hash_differs(store):
    """S21 (F4): EffectConflict outranks terminal, dead-ending an epoch.

    Review finding F4: `reserve()` checked `request_hash` before `state`, so a
    terminal row with a different hash raised `EffectConflict` that nothing
    could clear. A terminal row must raise `EffectTerminal` regardless of hash.
    """
    try:
        from sightmesh.effects import EffectTerminal
    except ImportError:
        fail_missing_kernel_v1("sightmesh.effects.EffectTerminal does not exist yet")
        return

    journal = EffectJournal(store)
    journal.reserve("t21", 1, request_hash({"kind": "workspace"}), "owner-a")
    journal.mark_launched("t21", 1, "ws-21", "sess-21")
    journal.mark_terminal("t21", 1, "completed")

    with pytest.raises(EffectTerminal):
        journal.reserve("t21", 1, request_hash({"kind": "session"}), "owner-a")


def _run_concurrent_first_run_migration(database, path: Path) -> object:
    """Drive the F5 interleaving: P2 reads schema while P1 holds the lock.

    A gate connection holds ``BEGIN IMMEDIATE`` while a second thread enters the
    real ``TaskStore`` migration. A pre-fix initializer reads ``has_version`` on
    the still-pre-kernel snapshot before it can lock, then blocks; the fixed one
    blocks on ``BEGIN IMMEDIATE`` first and only reads after the gate commits.
    The gate then completes the rebuild and bumps a version, so the pre-fix
    thread wipes it and the fixed thread preserves it.
    """
    started = threading.Event()
    errors: list[BaseException] = []

    def initialize_under_race() -> None:
        started.set()
        try:
            TaskStore(path)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors.append(exc)

    gate = database._open()
    gate.execute("BEGIN IMMEDIATE")
    thread = threading.Thread(target=initialize_under_race)
    thread.start()
    started.wait(timeout=10)
    # Give P2 time to reach its pre-lock read (fix: to block on BEGIN IMMEDIATE).
    time.sleep(0.4)
    _rebuild_managed_tasks_kernel_v1(gate)
    first_task_id = str(
        gate.execute("SELECT task_id FROM managed_tasks LIMIT 1").fetchone()[0]
    )
    gate.execute(
        "UPDATE managed_tasks SET version = 5 WHERE task_id = ?", (first_task_id,)
    )
    gate.execute("COMMIT")
    gate.close()
    thread.join(timeout=30)
    if errors:
        raise errors[0]
    return first_task_id


def test_s22_concurrent_first_run_migration_preserves_a_bumped_version(tmp_path):
    """S22 (F5): concurrent first-run migration resets version to 0.

    Review finding F5: `_initialize` read `sqlite_master`/`PRAGMA table_info`
    before `BEGIN IMMEDIATE`, so two processes on a pre-kernel DB both saw
    `has_version=False` and P2's rebuild wiped P1's preserved counters. Schema
    detection must move inside the lock: a version bumped between them must
    survive (final version >= 1, never reset to 0).
    """
    from sightmesh.escalation import EscalationStore

    path = tmp_path / "state.sqlite3"
    database = EscalationStore(path)
    now = time.time()
    with database._connect() as conn:
        conn.execute(_LEGACY_MANAGED_TASKS_DDL)
        conn.execute(
            "INSERT INTO managed_tasks VALUES "
            "('task-a', 'operator', 'one', NULL, 'active', 1, 1, 3, 0, '{}', "
            "NULL, 'sess-a', NULL, NULL, ?, ?)",
            (now, now),
        )

    task_id = _run_concurrent_first_run_migration(database, path)

    final = TaskStore(path).get_by_id(task_id)
    assert final is not None
    assert final.version >= 1
    assert final.version == 5


@pytest.mark.skipif(
    not FORENSICS_SNAPSHOT.exists(),
    reason="real escalations.sqlite3 forensics snapshot is not present",
)
def test_s22_forensics_snapshot_concurrent_migration_preserves_a_bumped_version(
    tmp_path,
):
    """S22 (F5): re-prove the concurrency fix against the real 28-row store.

    Runs the same interleaving against a COPY of the forensic
    `escalations.sqlite3` (28 pre-kernel `managed_tasks` rows); the original
    snapshot is never touched.
    """
    from sightmesh.escalation import EscalationStore

    path = tmp_path / "escalations.sqlite3"
    shutil.copy2(FORENSICS_SNAPSHOT, path)
    database = EscalationStore(path)
    with database._connect() as conn:
        before = int(
            conn.execute("SELECT COUNT(*) FROM managed_tasks").fetchone()[0]
        )
    assert before == 28

    task_id = _run_concurrent_first_run_migration(database, path)

    reopened = TaskStore(path)
    final = reopened.get_by_id(task_id)
    assert final is not None
    assert final.version == 5
    with database._connect() as conn:
        after = int(conn.execute("SELECT COUNT(*) FROM managed_tasks").fetchone()[0])
    assert after == before  # no rows lost to the rebuild race


def test_s23_a_wake_for_a_retired_parent_resolves_and_re_arms(client, store, ownership):
    """S23 (F6): wake delivery skips the deliverability + parent-state guard.

    Review finding F6: `WakeDelivery.deliver` checked only that the parent row
    exists and has a holder; it never called `ownership.assert_deliverable` nor
    checked `parent.state`, so it could send into a retired session and consume
    the dedupe key. A child completing while the parent holder is retired must
    resolve the wake with a reason and send nothing; with F1's re-arm, a later
    live cohort event still wakes the (successor) parent.
    """
    parent = _manager(store, children=4)
    a1 = _reserve_child(store, parent.task_id, "a1", "session-a1")
    a2 = _reserve_child(store, parent.task_id, "a2", "session-a2")

    # The parent's holder session is retired/superseded before the cohort ends.
    ownership.retire(
        parent.holder_session_id, state="retired", reason="superseded", logical_key="k"
    )

    finish_with_wake(store, a1.task_id, "completed", "one")
    finish_with_wake(store, a2.task_id, "completed", "two")

    delivery = WakeDelivery(client, store, ownership)
    assert delivery.pump() == 0
    resolved = query(store, "SELECT state, payload FROM task_wakes", ())
    assert len(resolved) == 1
    assert resolved[0]["state"] == "resolved"
    assert "retired" in resolved[0]["payload"]
    assert [row for row in client.sent if row[0] == parent.holder_session_id] == []

    # A live successor adopts the parent; a fresh cohort transition re-arms.
    revived = store.activate(
        parent.task_id, workspace_id="ws-manager", session_id="session-manager-2"
    )
    a3 = _reserve_child(store, parent.task_id, "a3", "session-a3")
    a4 = _reserve_child(store, parent.task_id, "a4", "session-a4")
    finish_with_wake(store, a3.task_id, "completed", "three")
    finish_with_wake(store, a4.task_id, "completed", "four")

    assert delivery.pump() == 1
    assert [row for row in client.sent if row[0] == revived.holder_session_id]


def test_s24_activate_readback_shares_its_own_transaction(store):
    """S24 (F7): guarded readback outside its transaction for activate.

    Review finding F7: `transition()` opens a no-BEGIN connection, so with
    `isolation_level=None` the UPDATE and the readback SELECT autocommit
    separately; a competing terminal transition landing between them makes the
    returned record describe a row the caller never wrote. With activate wrapped
    in `BEGIN IMMEDIATE`, the readback must match the row its own UPDATE
    produced. Pre-fix, the competing writer slips into the gap and activate
    reports `lost`.
    """
    ((task, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "racer", "children": 0}],
        max_attempts=3,
    )

    writer_done = threading.Event()
    fired = {"value": False}

    def compete() -> None:
        conn = store._database._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE managed_tasks SET state = 'lost', version = version + 1, "
                "updated_at = ? WHERE task_id = ? AND state IN "
                "('reserved', 'active', 'replacing')",
                (time.time(), task.task_id),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        writer_done.set()

    class _SpyConnection:
        """Fire a competing terminal writer in the UPDATE -> SELECT gap once."""

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, *args: object):
            cursor = self._conn.execute(sql, *args)
            if "state = 'active'" in sql and not fired["value"]:
                fired["value"] = True
                threading.Thread(target=compete).start()
                # Pre-fix (autocommit): the competing writer commits into the
                # gap fast and sets the event. Post-fix: activate holds the
                # write lock, the competing writer blocks, the event stays
                # clear and we fall through after the timeout.
                writer_done.wait(timeout=1.5)
            return cursor

        def __getattr__(self, name: str):
            return getattr(self._conn, name)

    original_connect = store._database._connect

    @contextmanager
    def spy_connect():
        with original_connect() as conn:
            yield _SpyConnection(conn)

    store._database._connect = spy_connect
    try:
        activated = store.activate(
            task.task_id, workspace_id="ws-1", session_id="sess-1"
        )
    finally:
        store._database._connect = original_connect

    # The record activate returns must be the row its own UPDATE produced.
    assert activated.state == "active"
    assert activated.holder_session_id == "sess-1"

    writer_done.wait(timeout=30)
    # The competing terminal transition still lands, just not inside the gap.
    assert store.get_by_id(task.task_id).state == "lost"


def test_s25_a_reconciler_rescan_re_arms_a_wake_resolved_while_undeliverable(
    client, store, ownership
):
    """S25 (G1): a resolved wake poisoned re-arm and hung the manager.

    Round-2 finding G1: `record_wakes` suppressed a new wake if any row - a
    `resolved` one included - covered the cohort, so once F6 resolved a wake
    for a transiently-undeliverable parent, the unchanged cohort never re-armed.
    The watermark makes re-arm correct by construction: a resolve never advances
    `last_woken_seq`, so `child_event_seq` still outruns it and the next
    reconciler re-scan of the same cohort arms a fresh wake and delivers it.
    """
    parent = _manager(store, children=2)
    a1 = _reserve_child(store, parent.task_id, "a1", "session-a1")
    a2 = _reserve_child(store, parent.task_id, "a2", "session-a2")

    # The parent holder is transiently undeliverable as the cohort finishes.
    ownership.retire(
        parent.holder_session_id, state="retired", reason="superseded", logical_key="k"
    )
    finish_with_wake(store, a1.task_id, "completed", "one")
    finish_with_wake(store, a2.task_id, "completed", "two")

    reconciler = DurableExecutionReconciler(
        client, task_store=store, ownership=ownership
    )
    first = reconciler.reconcile_kernel()
    assert first["wakes_delivered"] == 0
    resolved = query(store, "SELECT state FROM task_wakes", ())
    assert [row["state"] for row in resolved] == ["resolved"]
    assert [row for row in client.sent if row[0] == parent.holder_session_id] == []

    # Deliverability is restored; a re-scan of the UNCHANGED cohort re-arms
    # (the watermark never advanced past the resolve) and delivers exactly once.
    ownership.records.clear()
    second = reconciler.reconcile_kernel()
    assert second["wakes_inserted"] == 1
    assert second["wakes_delivered"] == 1
    assert [row for row in client.sent if row[0] == parent.holder_session_id]


def test_s26_a_replaced_then_reblocked_child_arms_a_distinct_second_wake(client, store):
    """S26 (G2): an identical repeated roster never re-woke the manager.

    Round-2 finding G2: `cohort_signature` hashed only child ids + state, so
    `a1` blocking, being `replace()`d, then blocking again produced an identical
    fingerprint and the delivered wave-one wake suppressed the second forever.
    Each child terminal/blocked event now bumps the parent watermark, so the
    second block outruns `last_woken_seq` and arms a distinct wake.
    """
    parent = _manager(store, children=1)
    a1 = _reserve_child(store, parent.task_id, "a1", "session-a1")
    finish_with_wake(store, a1.task_id, "blocked", "need input one")

    delivery = WakeDelivery(client, store)
    assert delivery.pump() == 1
    assert len([row for row in client.sent if row[0] == parent.holder_session_id]) == 1

    # replace(a1): bump the epoch, re-activate, then block again on a new reason.
    store.prepare_replacement(a1.task_id)
    store.activate(a1.task_id, workspace_id="ws-a1", session_id="session-a1-2")
    _record, second_wave = finish_with_wake(
        store, a1.task_id, "blocked", "need input two"
    )
    assert len(second_wave) == 1

    assert delivery.pump() == 1
    assert len([row for row in client.sent if row[0] == parent.holder_session_id]) == 2


def test_s27_a_transient_executor_failure_keeps_an_expired_reservation(client, store):
    """S27 (G3): an unknowable executor error orphaned a live launch.

    Round-2 finding G3: `_native_effect` caught bare `CdesktopError` and reported
    "absent", so a 5xx/timeout during the expiry sweep retired a reservation
    whose native session was still alive. Only a definitive 404 may retire;
    an unknowable error leaves the reservation intact for the next tick, which
    then adopts the live session once the executor answers.
    """
    ((task, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "flighted", "children": 0}],
        max_attempts=3,
    )
    journal = EffectJournal(store)
    launch = {"kind": "workspace", "request": {"name": "flighted"}}
    # ttl < 0 makes the lease already expired the moment it is taken.
    journal.reserve(task.task_id, task.epoch, request_hash(launch), "owner-a", ttl=-1.0)
    client.create_native_session(
        task.task_id, task.epoch, workspace_id="ws-x", session_id="sess-x"
    )

    # This tick the executor cannot answer (HTTP 500): not proof of death.
    client.fail_managed_effect(
        CdesktopRejectedError("GET managed effect: HTTP 500: server error", status=500)
    )
    assert journal.expire_reservations(client) == []
    effect = journal.get(task.task_id, task.epoch)
    assert effect is not None and effect.state == "reserved"
    assert store.get_by_id(task.task_id).state == "reserved"

    # Next tick the executor is reachable; the live native session is adopted.
    assert journal.expire_reservations(client) == []
    effect = journal.get(task.task_id, task.epoch)
    assert effect is not None and effect.state == "launched"
    assert (effect.workspace_id, effect.session_id) == ("ws-x", "sess-x")
    assert store.get_by_id(task.task_id).state == "active"


def test_s28_a_reconciler_rescan_of_an_unchanged_delivered_cohort_arms_nothing(
    client, store, ownership
):
    """S28 (G1 invariant, the key one): re-scanning a delivered cohort must
    create ZERO new wakes.

    A reconciler runs `record_wakes` over every parent every tick. Once a
    cohort's wake is delivered, `last_woken_seq` equals `child_event_seq`; an
    unchanged cohort therefore has `child_event_seq == last_woken_seq` and arms
    nothing, no matter how many times the reconciler re-scans it. This is the
    invariant the content-hash design kept getting wrong and the watermark makes
    true by construction.
    """
    parent = _manager(store, children=2)
    a1 = _reserve_child(store, parent.task_id, "a1", "session-a1")
    a2 = _reserve_child(store, parent.task_id, "a2", "session-a2")
    finish_with_wake(store, a1.task_id, "completed", "one")
    finish_with_wake(store, a2.task_id, "completed", "two")

    reconciler = DurableExecutionReconciler(
        client, task_store=store, ownership=ownership
    )
    first = reconciler.reconcile_kernel()
    assert first["wakes_delivered"] == 1
    to_parent = [row for row in client.sent if row[0] == parent.holder_session_id]
    assert len(to_parent) == 1

    # Re-scan the unchanged, already-delivered cohort several times: no new wake
    # arms, nothing is re-delivered, and no live wake is left behind.
    for _ in range(3):
        again = reconciler.reconcile_kernel()
        assert again["wakes_inserted"] == 0
        assert again["wakes_delivered"] == 0
    assert len([row for row in client.sent if row[0] == parent.holder_session_id]) == 1
    live = query(
        store,
        "SELECT COUNT(*) AS n FROM task_wakes WHERE state IN ('pending', 'claimed')",
        (),
    )
    assert live[0]["n"] == 0
