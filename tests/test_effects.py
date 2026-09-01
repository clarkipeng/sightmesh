from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from sightmesh.effects import (
    EffectBusy,
    EffectConflict,
    EffectJournal,
    new_owner_instance,
    request_hash,
)
from sightmesh.task_store import TaskStore


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
    one, or the reason a task died becomes unrecoverable."""
    journal.reserve("task-1", 1, request_hash(LAUNCH), "owner-a")
    journal.mark_launched("task-1", 1, "workspace-1", "session-1")

    journal.mark_terminal("task-1", 1, "quota:retry_at=60")
    later = journal.mark_terminal("task-1", 1, "lost:unknown")

    assert later.outcome == "quota:retry_at=60"


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


def test_get_returns_none_for_an_unreserved_epoch(journal):
    """Callers branch on this to decide between adopt and launch."""
    assert journal.get("task-1", 7) is None
