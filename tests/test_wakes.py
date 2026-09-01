from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from sightmesh.cdesktop import CdesktopError
from sightmesh.task_store import StaleTransition, TaskStore
from sightmesh.wakes import WakeDelivery, dedupe_key, finish_with_wake, record_wakes


class Recorder:
    """The one client call the outbox makes, plus a failure switch."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, session_id, prompt, sender=None, *, dedupe_key=None, intent=None):
        if self.fail:
            raise CdesktopError("cdesktop is unreachable")
        self.sent.append((session_id, prompt, sender, dedupe_key, intent))
        return {"queued": True}


@pytest.fixture
def cohort(tmp_path):
    """One active manager with two active children."""
    store = TaskStore(tmp_path / "state.sqlite3")
    ((parent, _),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "manager", "children": 4}],
        max_attempts=3,
    )
    parent = store.activate(
        parent.task_id, workspace_id="ws-manager", session_id="session-manager"
    )
    children = []
    for key in ("first", "second"):
        ((child, _),) = store.reserve_all(
            scope="operator",
            parent_task_id=parent.task_id,
            specs=[{"key": key, "children": 0}],
            max_attempts=3,
        )
        children.append(
            store.activate(
                child.task_id, workspace_id=f"ws-{key}", session_id=f"session-{key}"
            )
        )
    return store, parent, children


def _wakes(store):
    with store.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_wakes ORDER BY created_at"
            ).fetchall()
        ]


def test_no_wake_until_the_whole_cohort_is_terminal(cohort):
    """A manager waiting on all_children_terminal must not be resumed
    halfway through; that is the wake-per-child behaviour this replaces."""
    store, parent, children = cohort

    finish_with_wake(store, children[0].task_id, "completed", "one done")
    assert _wakes(store) == []

    _record, created = finish_with_wake(store, children[1].task_id, "completed", "two")
    assert len(created) == 1
    assert _wakes(store)[0]["predicate"] == "all_children_terminal"
    assert _wakes(store)[0]["dedupe_key"] == dedupe_key(
        parent.task_id, "all_children_terminal"
    )


def test_the_child_transition_and_its_wake_commit_together(cohort):
    """The historical crash between 'child finished' and 'parent notified'
    is impossible when both are the same commit, so a wake row must exist
    the instant the state change is visible, with no delivery attempted."""
    store, _parent, children = cohort

    record, created = finish_with_wake(store, children[0].task_id, "blocked", "stuck")

    assert record.state == "blocked"
    assert len(created) == 1
    assert _wakes(store)[0]["state"] == "pending"


def test_a_hundred_duplicate_completions_make_one_wake(cohort):
    """Duplicate child-completion events were a real source of repeated
    manager wakeups; the dedupe key must collapse them to one row."""
    store, _parent, children = cohort
    finish_with_wake(store, children[1].task_id, "completed", "two")

    def finish(_index):
        try:
            return finish_with_wake(store, children[0].task_id, "completed", "one")[1]
        except StaleTransition:
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = [wake for batch in pool.map(finish, range(100)) for wake in batch]

    assert len(created) == 1
    assert len(_wakes(store)) == 1


def test_one_blocked_child_wakes_the_parent_without_replacing_its_turn(cohort):
    """The live bug this replaces sent intent='replace' on a blocked child,
    killing the manager's turn mid-cohort. Continuation must be additive."""
    store, parent, children = cohort
    client = Recorder()
    finish_with_wake(store, children[0].task_id, "blocked", "needs a decision")

    assert WakeDelivery(client, store).pump() == 1

    session_id, payload, _sender, key, intent = client.sent[0]
    assert (session_id, intent) == (parent.holder_session_id, "continue")
    assert key == _wakes(store)[0]["wake_id"]
    assert "any_child_blocked" in payload


def test_the_payload_consolidates_every_child_row(cohort):
    """A manager resuming after a cohort needs the whole cohort, not the one
    child that happened to finish last."""
    store, _parent, children = cohort
    client = Recorder()
    finish_with_wake(store, children[0].task_id, "completed", "one done")
    finish_with_wake(store, children[1].task_id, "blocked", "two stuck")

    WakeDelivery(client, store).pump()

    payload = client.sent[0][1]
    assert "first: completed | one done" in payload
    assert "second: blocked | two stuck" in payload


def test_a_delivered_wake_is_not_delivered_again(cohort):
    """At-least-once delivery still has to converge, or every reconciler tick
    would re-interrupt a manager that already resumed."""
    store, _parent, children = cohort
    client = Recorder()
    finish_with_wake(store, children[0].task_id, "blocked", "stuck")
    delivery = WakeDelivery(client, store)

    assert delivery.pump() == 1
    assert delivery.pump() == 0
    assert len(client.sent) == 1


def test_a_claim_left_by_a_dead_pump_is_reclaimed(cohort):
    """A process that dies mid-delivery must not strand the wake; the claim
    is a lease so the next pump picks it up once the lease lapses."""
    store, _parent, children = cohort
    client = Recorder()
    finish_with_wake(store, children[0].task_id, "blocked", "stuck")
    WakeDelivery(client, store, claim_seconds=-1.0).claim()

    assert WakeDelivery(client, store).pump() == 1


def test_a_suppressed_delivery_is_resolved_with_a_reason(tmp_path):
    """Returning silently is how the old path lost signals. A wake that
    cannot be delivered must leave a readable record of why."""
    store = TaskStore(tmp_path / "state.sqlite3")
    ((parent, _),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "manager", "children": 2}],
        max_attempts=3,
    )
    ((child, _),) = store.reserve_all(
        scope="operator",
        parent_task_id=parent.task_id,
        specs=[{"key": "only", "children": 0}],
        max_attempts=3,
    )
    store.activate(child.task_id, workspace_id="ws", session_id="session-only")
    client = Recorder()
    finish_with_wake(store, child.task_id, "completed", "done")

    assert WakeDelivery(client, store).pump() == 0

    row = _wakes(store)[0]
    assert row["state"] == "resolved"
    assert "no holder session" in row["payload"]
    assert client.sent == []


def test_an_undeliverable_wake_stays_in_the_outbox(cohort):
    """A transport failure is not a resolution; the row has to remain
    claimable so the reconciler retries instead of dropping the wake."""
    store, _parent, children = cohort
    client = Recorder()
    client.fail = True
    finish_with_wake(store, children[0].task_id, "blocked", "stuck")

    assert WakeDelivery(client, store, claim_seconds=-1.0).pump() == 0

    client.fail = False
    assert WakeDelivery(client, store).pump() == 1


def test_record_wakes_is_idempotent_for_an_already_satisfied_predicate(cohort):
    """The reconciler replays this over every parent on every tick; it must
    only ever fill a real gap."""
    store, parent, children = cohort
    for child in children:
        finish_with_wake(store, child.task_id, "completed", "done")

    with store.connect() as conn:
        assert record_wakes(conn, parent.task_id) == []
