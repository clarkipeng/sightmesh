"""S18-S21: external-run failures that must survive the supervisor turn."""

from __future__ import annotations

import json

from sightmesh.cdesktop import CdesktopError
from sightmesh.external_runs import ExternalRunReconciler, ExternalRunStore


class Client:
    def __init__(self, reachable=True):
        self.reachable, self.sent = reachable, []

    def session(self, value):
        if not self.reachable:
            raise CdesktopError("unreachable")
        return {"id": value, "workspace_id": "ws"}

    def workspace(self, value):
        return {"id": value, "archived": False}

    def send(self, session, message, sender=None, **kwargs):
        self.sent.append((session, kwargs["dedupe_key"]))
        return {"ok": True}


def subscribed(tmp_path):
    store = ExternalRunStore(tmp_path / "state.sqlite3")
    return store, store.subscribe(
        run_id="r1", output_root=tmp_path / "output", return_session_id="parent"
    )


def receipt(run):
    with open(run.receipt_path, "w") as output:
        output.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "subscription_id": run.subscription_id,
                    "run_id": run.run_id,
                    "terminal_state": "completed",
                    "finished_at": "2026-09-03T00:00:00Z",
                }
            )
        )


def test_s18_cross_restart_terminal_receipt_wake(tmp_path):
    """S18: service restart after runner completion still wakes the parent."""
    store, result = subscribed(tmp_path)
    receipt(result.run)
    restarted = ExternalRunStore(store.path)
    ExternalRunReconciler(Client(), restarted).reconcile()
    assert restarted.get(result.run.subscription_id).state == "notified"


def test_s19_pid_reuse_is_lost_unknown(tmp_path):
    """S19: a reused PID never becomes invented success or crash."""
    store, result = subscribed(tmp_path)
    run = store.bind(
        result.run.subscription_id,
        writer_capability=result.writer_capability,
        pid=1,
        process_fingerprint="old",
        observer=lambda _: "old",
    )
    ExternalRunReconciler(Client(), store, observer=lambda _: "new").reconcile_one(run)
    assert store.get(run.subscription_id).outcome == "lost/unknown"


def test_s20_duplicate_receipt_has_one_logical_delivery(tmp_path):
    """S20: receipt replay preserves the original digest and wake key."""
    store, result = subscribed(tmp_path)
    receipt(result.run)
    client = Client()
    runner = ExternalRunReconciler(client, store)
    runner.reconcile()
    runner.reconcile()
    assert len(client.sent) == 1 and client.sent[0][1] == result.run.dedupe_key


def test_s21_unreachable_parent_parks_then_recovers(tmp_path):
    """S21: no live parent parks the durable cdesktop command for recovery."""
    store, result = subscribed(tmp_path)
    receipt(result.run)
    ExternalRunReconciler(Client(reachable=False), store).reconcile()
    parked = store.escalations.pending()
    assert len(parked) == 1 and parked[0].dedupe_key == result.run.dedupe_key
