from __future__ import annotations

import pytest

from sightmesh import history, wakes
from sightmesh.task_store import TaskStore
from sightmesh.wakes import WakeDelivery, finish_with_wake
from test_wakes import Recorder, cohort


def test_pre_token_wake_migration_preserves_saved_payload_and_reclaims(cohort):
    """An old claim has no invented owner; its successor keeps the saved bytes."""
    store, _parent, children = cohort
    finish_with_wake(store, children[0].task_id, "blocked", "ready")
    client = Recorder()
    client.fail = True
    WakeDelivery(client, store, claim_seconds=-1).pump()
    with store.connect() as conn:
        conn.execute("ALTER TABLE task_wakes DROP COLUMN claim_token")
        before = dict(conn.execute("SELECT * FROM task_wakes").fetchone())
    reopened = TaskStore(store.path)
    with reopened.connect() as conn:
        after = dict(conn.execute("SELECT * FROM task_wakes").fetchone())
    assert after.pop("claim_token") is None
    assert after == before
    client.fail = False
    assert WakeDelivery(client, reopened).pump() == 1
    assert client.sent[0][1] == before["payload"]


def test_wake_uses_references_without_copying_child_evidence(cohort):
    store, parent, children = cohort
    sentinel = "RESULT-" + "x" * 8192
    store.record_liveness(children[0].task_id, "stalled", evidence=sentinel)
    finish_with_wake(store, children[0].task_id, "blocked", sentinel)
    client = Recorder()
    WakeDelivery(client, store).pump()
    payload = client.sent[0][1]
    assert sentinel not in payload
    assert children[0].task_id in payload and children[0].holder_session_id in payload
    assert "liveness=stalled" in payload and "state=blocked" in payload
    with store.connect() as conn:
        entries = history.task_history(conn, children[0].task_id, entity="task")
    assert any(sentinel in row["changed"] for row in entries)


def test_retry_reuses_wake_bytes_without_payload_history_occurrence(cohort):
    store, _parent, children = cohort
    client = Recorder()
    client.fail = True
    finish_with_wake(store, children[0].task_id, "blocked", "unique")
    delivery = WakeDelivery(client, store, claim_seconds=-1)
    delivery.pump()
    with store.connect() as conn:
        before = conn.execute(
            "SELECT count(*) FROM task_history WHERE entity='wake'"
        ).fetchone()[0]
        payload = conn.execute("SELECT payload FROM task_wakes").fetchone()[0]
    client.fail = False
    delivery.pump()
    with store.connect() as conn:
        after = conn.execute(
            "SELECT count(*) FROM task_history WHERE entity='wake'"
        ).fetchone()[0]
    assert (
        client.sent[0][1] == payload and after == before + 2
    )  # claim + settlement only


def test_history_failure_rolls_back_real_wake_arm(cohort, monkeypatch):
    store, parent, children = cohort
    original = wakes.history.record_change

    def fail(*args, **kwargs):
        if kwargs.get("entity") == "wake":
            raise RuntimeError("history failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(wakes.history, "record_change", fail)
    try:
        finish_with_wake(store, children[0].task_id, "blocked", "ready")
    except RuntimeError:
        with store.connect() as conn:
            assert conn.execute("SELECT count(*) FROM task_wakes").fetchone()[0] == 0
        assert store.get_by_id(children[0].task_id).state == "active"
    else:
        raise AssertionError("wake history failure did not propagate")


@pytest.mark.parametrize("cause", ["claimed", "settled"])
def test_history_failure_keeps_wake_and_watermark_retryable(cohort, monkeypatch, cause):
    """A history write cannot leave a claim or acceptance half committed."""
    store, parent, children = cohort
    finish_with_wake(store, children[0].task_id, "blocked", "ready")
    client = Recorder()
    delivery = WakeDelivery(client, store, claim_seconds=-1)
    original = wakes.history.record_change

    def fail(*args, **kwargs):
        if kwargs.get("entity") == "wake" and kwargs.get("cause") == cause:
            raise RuntimeError("history failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(wakes.history, "record_change", fail)
    with pytest.raises(RuntimeError, match="history failure"):
        delivery.pump()
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM task_wakes").fetchone()
        assert row["state"] == ("pending" if cause == "claimed" else "claimed")
        assert (
            conn.execute(
                "SELECT count(*) FROM task_history WHERE entity='wake' AND cause=?",
                (cause,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT last_woken_seq FROM managed_tasks WHERE task_id=?",
                (parent.task_id,),
            ).fetchone()[0]
            == 0
        )
    assert len(client.sent) == (0 if cause == "claimed" else 1)

    monkeypatch.setattr(wakes.history, "record_change", original)
    assert delivery.pump() == 1
    with store.connect() as conn:
        assert (
            conn.execute(
                "SELECT last_woken_seq FROM managed_tasks WHERE task_id=?",
                (parent.task_id,),
            ).fetchone()[0]
            == row["event_seq"]
        )
    if cause == "settled":
        assert client.sent[0] == client.sent[1]  # same native dedupe key and bytes


@pytest.mark.parametrize("settlement", ["delivered", "resolved"])
def test_expired_claim_cannot_settle_another_claim_or_overwrite_delivery(
    cohort, settlement
):
    """An expired pump owns neither its successor's claim nor its final result."""
    store, parent, children = cohort
    finish_with_wake(store, children[0].task_id, "blocked", "ready")
    client = Recorder()
    stale = WakeDelivery(client, store, claim_seconds=-1)
    old_wake = stale.claim()[0]
    current = WakeDelivery(client, store)
    current_wake = current.claim()[0]
    with store.connect() as conn:
        before = dict(conn.execute("SELECT * FROM task_wakes").fetchone())
        history_before = conn.execute("SELECT count(*) FROM task_history").fetchone()[0]
    stale._settle(old_wake, settlement, "late decision")
    with store.connect() as conn:
        assert dict(conn.execute("SELECT * FROM task_wakes").fetchone()) == before
        assert (
            conn.execute("SELECT count(*) FROM task_history").fetchone()[0]
            == history_before
        )
    assert stale._deliver(old_wake) is False
    assert client.sent == []
    assert current._deliver(current_wake) is True
    with store.connect() as conn:
        delivered = dict(conn.execute("SELECT * FROM task_wakes").fetchone())
    stale._settle(old_wake, settlement, "even later decision")
    with store.connect() as conn:
        assert dict(conn.execute("SELECT * FROM task_wakes").fetchone()) == delivered
        assert (
            conn.execute(
                "SELECT last_woken_seq FROM managed_tasks WHERE task_id=?",
                (parent.task_id,),
            ).fetchone()[0]
            == current_wake.event_seq
        )
