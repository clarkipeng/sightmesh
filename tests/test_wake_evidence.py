from __future__ import annotations

from sightmesh import history, wakes
from sightmesh.task_store import TaskStore
from sightmesh.wakes import WakeDelivery, finish_with_wake, record_wakes
from test_wakes import Recorder, cohort


def test_wake_uses_references_without_copying_child_evidence(cohort):
    store, parent, children = cohort
    sentinel = "RESULT-" + "x" * 8192
    store.record_liveness(children[0].task_id, "stalled", evidence=sentinel)
    finish_with_wake(store, children[0].task_id, "blocked", sentinel)
    client = Recorder(); WakeDelivery(client, store).pump()
    payload = client.sent[0][1]
    assert sentinel not in payload
    assert children[0].task_id in payload and children[0].holder_session_id in payload
    assert "liveness=stalled" in payload and "state=blocked" in payload
    with store.connect() as conn:
        entries = history.task_history(conn, children[0].task_id, entity="task")
    assert any(sentinel in row["changed"] for row in entries)


def test_retry_reuses_wake_bytes_without_payload_history_occurrence(cohort):
    store, _parent, children = cohort; client = Recorder(); client.fail = True
    finish_with_wake(store, children[0].task_id, "blocked", "unique")
    delivery = WakeDelivery(client, store, claim_seconds=-1)
    delivery.pump()
    with store.connect() as conn:
        before = conn.execute("SELECT count(*) FROM task_history WHERE entity='wake'").fetchone()[0]
        payload = conn.execute("SELECT payload FROM task_wakes").fetchone()[0]
    client.fail = False; delivery.pump()
    with store.connect() as conn:
        after = conn.execute("SELECT count(*) FROM task_history WHERE entity='wake'").fetchone()[0]
    assert client.sent[0][1] == payload and after == before + 2  # claim + settlement only


def test_history_failure_rolls_back_real_wake_arm(cohort, monkeypatch):
    store, parent, children = cohort
    original = wakes.history.record_change
    def fail(*args, **kwargs):
        if kwargs.get("entity") == "wake": raise RuntimeError("history failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(wakes.history, "record_change", fail)
    try:
        finish_with_wake(store, children[0].task_id, "blocked", "ready")
    except RuntimeError:
        with store.connect() as conn:
            assert conn.execute("SELECT count(*) FROM task_wakes").fetchone()[0] == 0
        assert store.get_by_id(children[0].task_id).state == "active"
    else: raise AssertionError("wake history failure did not propagate")
