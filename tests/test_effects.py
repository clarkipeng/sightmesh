from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sightmesh.cdesktop import CdesktopError
from sightmesh.effects import (
    EffectBusy,
    EffectConflict,
    EffectJournal,
    EffectTerminal,
    new_owner_instance,
    request_hash,
)
from sightmesh.fence import assert_external_io_allowed
from sightmesh.task_store import TaskStore, TaskStoreError


@pytest.fixture
def journal(tmp_path):
    return EffectJournal(TaskStore(tmp_path / "state.sqlite3"))


LAUNCH = {"kind": "workspace", "request": {"name": "audit", "prompt": "go"}}


def test_the_request_hash_ignores_key_order_but_not_content():
    """The hash is what proves a retry is the same launch and not drift, so
    it must survive dict ordering while still catching a changed prompt."""
    reordered = {"request": {"prompt": "go", "name": "audit"}, "kind": "workspace"}
    changed = {"kind": "workspace", "request": {"name": "audit", "prompt": "stop"}}

    assert request_hash(LAUNCH) == request_hash(reordered)
    assert request_hash(LAUNCH) != request_hash(changed)


def test_the_second_reserver_reads_the_first_row_instead_of_launching(journal):
    """One row per (task, epoch) is what makes a duplicate start impossible.

    Without it, two callers racing on the same key each believe they are
    first and cdesktop ends up with two native sessions.
    """
    owner = new_owner_instance()
    first, took_over = journal.reserve("task-1", 1, request_hash(LAUNCH), owner)
    second, retook = journal.reserve("task-1", 1, request_hash(LAUNCH), owner)

    assert (took_over, retook) == (False, False)
    assert first.state == second.state == "reserved"
    assert journal.get("task-1", 1).owner_instance == owner


def test_a_live_owner_blocks_a_second_instance(journal):
    """A reservation is a lease, not a lock file: only a dead owner's claim
    may be taken, or two SightMesh instances would both launch."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")

    with pytest.raises(EffectBusy):
        journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-b")


def test_an_expired_lease_is_adopted_by_the_next_owner(journal):
    """A crashed owner must not park the epoch forever; the next caller
    takes the same reserved identifiers rather than opening a new epoch."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a", ttl=-1.0)

    effect, took_over = journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-b")

    assert took_over is True
    assert effect.owner_instance == "owner-b"
    assert effect.lease_expires_at > time.time()


def test_a_different_launch_spec_is_a_conflict_not_a_relaunch(journal):
    """Silently relaunching a drifted spec on a reserved epoch would run work
    nobody asked for under an identity that promises the opposite."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")

    with pytest.raises(EffectConflict):
        journal.reserve("task-1", 1, request_hash({"kind": "session"}), "owner-a")


def test_a_launched_effect_is_returned_for_adoption(journal):
    """Reserve is also the crash-recovery read: an already launched epoch
    must hand back its native ids so the retry adopts them."""
    owner = new_owner_instance()
    journal.reserve("task-1", 1, request_hash(LAUNCH), owner)
    journal.mark_launched("task-1", 1, "workspace-1", "session-1")

    effect, took_over = journal.reserve("task-1", 1, request_hash(LAUNCH), owner)

    assert (effect.state, took_over) == ("launched", False)
    assert (effect.workspace_id, effect.session_id) == ("workspace-1", "session-1")


def test_the_first_terminal_outcome_wins(journal):
    """A typed provider outcome must not be overwritten by a later generic
    one, or the reason a task died becomes unrecoverable.

    The first terminal write wins by *rejecting* a second advance rather than
    silently swallowing it: a terminal effect is a barrier, so ``mark_terminal``
    on an already-terminal row raises (rowcount 0 is no longer a silent no-op),
    and the recorded outcome is unchanged.
    """
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")
    journal.mark_launched("task-1", 1, "workspace-1", "session-1")

    journal.mark_terminal("task-1", 1, "quota:retry_at=60")
    with pytest.raises(EffectTerminal):
        journal.mark_terminal("task-1", 1, "lost:unknown")

    assert journal.get("task-1", 1).outcome == "quota:retry_at=60"


def test_expired_reservations_that_never_launched_are_retired(journal):
    """An owner that died before the native call leaves a reservation with no
    session behind it; leaving it 'reserved' hides a task that will never run."""
    journal.reserve("dead", 1, request_hash(LAUNCH), "owner-a", ttl=-1.0)
    journal.reserve("live", 1, request_hash(LAUNCH), "owner-a")
    journal.reserve("launched", 1, request_hash(LAUNCH), "owner-a", ttl=-1.0)
    journal.mark_launched("launched", 1, "workspace-1", "session-1")

    expired = journal.expire_reservations()

    assert [effect.task_id for effect in expired] == ["dead"]
    assert expired[0].outcome == "lost:reservation-expired"
    assert journal.get("live", 1).state == "reserved"
    assert journal.get("launched", 1).state == "launched"


def test_a_hundred_concurrent_reservations_yield_one_effect(journal):
    """The whole point of the journal under load: many observers of one
    start request must converge on a single native effect."""
    owner = new_owner_instance()

    def reserve(_index):
        return journal.reserve("task-1", 1, request_hash(LAUNCH), owner)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        effects = list(pool.map(reserve, range(100)))

    assert {(effect.task_id, effect.epoch) for effect in effects} == {("task-1", 1)}
    assert journal.get("task-1", 1) is not None


def test_a_terminal_effect_refuses_a_relaunch(journal):
    """A terminal epoch is a launch barrier: reserving it again must raise, not
    hand back an adoptable row, or a finished epoch relaunches under an
    identity that promised it was done."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")
    journal.mark_launched("task-1", 1, "workspace-1", "session-1")
    journal.mark_terminal("task-1", 1, "completed")

    with pytest.raises(EffectTerminal):
        journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")


def test_a_terminal_effect_outranks_a_hash_conflict(journal):
    """State is checked before the hash: a terminal row raises EffectTerminal
    whatever the request hash, so a drifted retry can never dead-end an already
    finished epoch behind an unclearable EffectConflict."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")
    journal.mark_launched("task-1", 1, "workspace-1", "session-1")
    journal.mark_terminal("task-1", 1, "completed")

    with pytest.raises(EffectTerminal):
        journal.reserve("task-1", 1, request_hash({"kind": "session"}), "owner-a")


def test_a_launched_effect_adopts_despite_a_hash_conflict(journal):
    """The launch already happened, so a launched row is adopted whatever the
    hash; only a live reserved row with a different hash is a conflict."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")
    journal.mark_launched("task-1", 1, "workspace-1", "session-1")

    effect, took_over = journal.reserve(
        "task-1", 1, request_hash({"kind": "session"}), "owner-a"
    )

    assert (effect.state, took_over) == ("launched", False)


def test_marking_a_terminal_effect_again_raises_instead_of_no_op(journal):
    """A no-op UPDATE used to pass silently; the rowcount check turns a lost
    write into a raised error so a terminal effect is never re-marked."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")
    journal.mark_launched("task-1", 1, "workspace-1", "session-1")
    journal.mark_terminal("task-1", 1, "completed")

    with pytest.raises(TaskStoreError):
        journal.mark_launched("task-1", 1, "workspace-2", "session-2")


class _NativeReports:
    """Minimal cdesktop double answering only the managed-effect lookup."""

    def __init__(self, effect):
        self._effect = effect

    def managed_effect(self, task_id, epoch):
        if self._effect is None:
            raise CdesktopError(f"GET {task_id}/{epoch}: HTTP 404: not found")
        return dict(self._effect)


def test_expiry_adopts_a_reservation_a_live_native_session_stands_behind(tmp_path):
    """The 15s launch window makes 'session created, mark_launched never ran'
    ordinary; expiry must adopt a live native session, not orphan it."""
    store = TaskStore(tmp_path / "state.sqlite3")
    ((task, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "adopted", "children": 0}],
        max_attempts=3,
    )
    journal = EffectJournal(store)
    journal.reserve(task.task_id, task.epoch, request_hash(LAUNCH), "owner-a", ttl=-1.0)
    client = _NativeReports(
        {"state": "active", "workspace_id": "ws-x", "session_id": "sess-x"}
    )

    lost = journal.expire_reservations(client)

    assert lost == []
    assert journal.get(task.task_id, task.epoch).state == "launched"
    assert store.get_by_id(task.task_id).state == "active"


def test_expiry_loses_a_reservation_no_native_session_stands_behind(tmp_path):
    """A genuinely absent native session is the only case that still retires
    the reservation lost:reservation-expired."""
    store = TaskStore(tmp_path / "state.sqlite3")
    journal = EffectJournal(store)
    journal.reserve("gone", 1, request_hash(LAUNCH), "owner-a", ttl=-1.0)

    lost = journal.expire_reservations(_NativeReports(None))

    assert [effect.task_id for effect in lost] == ["gone"]
    assert journal.get("gone", 1).outcome == "lost:reservation-expired"


def test_expiry_stops_a_native_workspace_superseded_during_its_probe(tmp_path):
    """A probe that loses its epoch records and stops the workspace outside the fence."""
    store = TaskStore(tmp_path / "state.sqlite3")
    ((task, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "expired", "children": 0}],
        max_attempts=3,
    )
    journal = EffectJournal(store)
    journal.reserve(task.task_id, task.epoch, request_hash(LAUNCH), "owner-a", ttl=-1.0)

    class SupersedingClient:
        stopped: list[str] = []

        def managed_effect(self, task_id, epoch):
            store.finish(task_id, "cancelled")
            return {"state": "active", "workspace_id": "ws-race", "session_id": "s-race"}

        def stop_workspace(self, workspace_id):
            assert_external_io_allowed()
            self.stopped.append(workspace_id)

    client = SupersedingClient()
    assert journal.expire_reservations(client) == []
    assert client.stopped == ["ws-race"]
    effect = journal.get(task.task_id, task.epoch)
    assert effect is not None and effect.outcome == "superseded"
    assert effect.workspace_id is None


def test_superseded_stop_is_retried_from_its_recorded_intent(journal):
    """A failed stop leaves its workspace id durable for the next reconcile pass."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")
    journal.mark_superseded("task-1", 1, "ws-retry")

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def stop_workspace(self, _workspace_id):
            self.calls += 1
            if self.calls == 1:
                raise CdesktopError("temporary stop failure")

    client = FlakyClient()
    assert journal.reconcile_superseded(client) == 0
    assert journal.get("task-1", 1).workspace_id == "ws-retry"
    assert journal.reconcile_superseded(client) == 1
    assert journal.get("task-1", 1).workspace_id is None


def test_get_returns_none_for_an_unreserved_epoch(journal):
    """Callers branch on this to decide between adopt and launch."""
    assert journal.get("task-1", 7) is None


def test_the_pinned_seams_400_not_found_shape_is_definitive_absence(tmp_path):
    """Live-canary finding (2026-09-01): the pinned cdesktop seam reports a
    missing effect as HTTP 400 "Managed task effect not found", not 404
    (managed_tasks.rs maps the miss to ApiError::BadRequest). Treating only
    404 as absence blocked the capability probe against the real server and
    silently disabled reservation retirement. Both shapes are real absence;
    a 400 with any other message stays unknowable.
    """
    from sightmesh.cdesktop import CdesktopRejectedError, is_effect_not_found

    real_miss = CdesktopRejectedError(
        'HTTP 400: {"success":false,"data":null,"error_data":null,'
        '"message":"Managed task effect not found"}',
        status=400,
    )
    assert is_effect_not_found(real_miss)
    assert is_effect_not_found(CdesktopRejectedError("gone", status=404))
    assert not is_effect_not_found(
        CdesktopRejectedError("Caller session belongs to another workspace", status=400)
    )
    assert not is_effect_not_found(CdesktopRejectedError("boom", status=500))


def test_a_capacity_outcome_persists_the_providers_reset(journal) -> None:
    """The reconcile that reads a typed outcome runs long after the rejection,
    so the provider's advertised reset has to be durable next to the outcome.
    Losing it means every reroute falls back to a blunt multi-hour cooldown."""
    journal.reserve("task-a", 1, request_hash(LAUNCH), new_owner_instance())

    journal.mark_terminal("task-a", 1, "rate_limited", 1_893_456_000.0)

    effect = journal.get("task-a", 1)
    assert (effect.outcome, effect.retry_at) == ("rate_limited", 1_893_456_000.0)


def test_an_outcome_without_a_reset_stores_none_rather_than_a_guess(journal) -> None:
    """No advertised reset is a fact, not a zero: storing 0.0 would read as a
    cooldown that already expired."""
    journal.reserve("task-a", 1, request_hash(LAUNCH), new_owner_instance())

    journal.mark_terminal("task-a", 1, "auth")

    assert journal.get("task-a", 1).retry_at is None


def test_only_current_epoch_outcomes_on_live_tasks_are_swept(journal) -> None:
    """The sweep is what advances a task whose launch was rejected before it
    ever held a session. It must see exactly the epoch the task is on: a
    superseded epoch's outcome re-triggering the reroute that already moved
    past it would fork the work, and a completed task has nothing to reroute.
    """
    store = journal.store
    _insert_task(store, "task-live", epoch=2, state="blocked")
    _insert_task(store, "task-stale", epoch=3, state="active")
    _insert_task(store, "task-done", epoch=1, state="completed")
    for task_id, epoch, outcome in (
        ("task-live", 2, "rate_limited"),
        ("task-stale", 2, "rate_limited"),
        ("task-done", 1, "provider_down"),
    ):
        journal.reserve(task_id, epoch, request_hash(LAUNCH), new_owner_instance())
        journal.mark_terminal(task_id, epoch, outcome)

    swept = journal.with_outcomes({"rate_limited", "auth", "provider_down"})

    assert [(e.task_id, e.epoch) for e in swept] == [("task-live", 2)]
    assert journal.with_outcomes(set()) == []


def _insert_task(store, task_id: str, *, epoch: int, state: str) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO managed_tasks (task_id, scope, task_key, state, epoch, "
            "attempts, max_attempts, child_limit, spec_json, created_at, updated_at) "
            "VALUES (?, 'operator', ?, ?, ?, 1, 3, 0, '{}', 0, 0)",
            (task_id, task_id, state, epoch),
        )
