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

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sightmesh.sdk import BatchError, SightMesh
from sightmesh.task_store import TaskStore, TaskStoreError

from .conftest import fail_missing_kernel_v1, make_mesh, query, table_exists, worker_spec
from .fake_cdesktop import SimulatedCrash

pytestmark = pytest.mark.simulator


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

    dedupe_key = f"{parent_record.task_id}:{parent_record.epoch}:all_children_terminal"
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
