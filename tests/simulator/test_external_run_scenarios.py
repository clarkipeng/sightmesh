"""S18-S24: external-run failures that must survive the supervisor turn."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from sightmesh.cdesktop import CdesktopError
from sightmesh.external_runs import (
    ExternalRunError,
    ExternalRunReconciler,
    ExternalRunStore,
    StaleExternalRunTransition,
)


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
        expect_version=result.run.version,
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
    """S21: parked external delivery reaches a recovered parent exactly once."""
    store, result = subscribed(tmp_path)
    receipt(result.run)
    ExternalRunReconciler(Client(reachable=False), store).reconcile()
    parked = store.escalations.pending()
    assert len(parked) == 1 and parked[0].dedupe_key == result.run.dedupe_key
    client = Client()
    reconciler = ExternalRunReconciler(client, store)
    reconciler.reconcile()
    reconciler.reconcile()
    assert client.sent == [("parent", result.run.dedupe_key)]


def test_s22_two_writers_race_for_one_output_root(tmp_path):
    """S22: the live-only lease index admits exactly one concurrent writer."""
    store = ExternalRunStore(tmp_path / "state.sqlite3")

    def subscribe(run_id):
        return store.subscribe(
            run_id=run_id, output_root=tmp_path / "output", return_session_id="parent"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(subscribe, run_id) for run_id in ("a", "b")]
    assert sum(future.exception() is None for future in attempts) == 1
    assert isinstance(next(future.exception() for future in attempts if future.exception()), ExternalRunError)


def test_s23_stale_version_actor_after_restart_is_fenced(tmp_path):
    """S23: a restarted stale runner cannot overwrite the current binding."""
    store, result = subscribed(tmp_path)
    store.bind(
        result.run.subscription_id,
        writer_capability=result.writer_capability,
        pid=1,
        process_fingerprint="first",
        expect_version=result.run.version,
        observer=lambda _: "first",
    )
    restarted = ExternalRunStore(store.path)
    with pytest.raises(StaleExternalRunTransition):
        restarted.bind(
            result.run.subscription_id,
            writer_capability=result.writer_capability,
            pid=2,
            process_fingerprint="second",
            expect_version=result.run.version,
            observer=lambda _: "second",
        )


def test_s24_existing_store_migrates_live_and_released_lease_history(tmp_path):
    """S24: old lifetime-root rows migrate without reviving released claims."""
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE external_run_leases (
                output_root TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, released_at REAL
            );
            CREATE TABLE external_run_subscriptions (
                subscription_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                output_root TEXT NOT NULL UNIQUE, return_session_id TEXT NOT NULL,
                return_workspace_id TEXT, writer_capability_digest TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0, pid INTEGER,
                process_fingerprint TEXT, receipt_path TEXT NOT NULL,
                receipt_digest TEXT, outcome TEXT, diagnostic TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                terminal_at REAL, notified_at REAL
            );
            """
        )
        for subscription_id, root, state in (
            ("live", str(tmp_path / "live"), "active"),
            ("released", str(tmp_path / "released"), "released"),
        ):
            conn.execute(
                "INSERT INTO external_run_leases VALUES (?, ?, ?, 0, 1, NULL)",
                (root, subscription_id, state),
            )
            conn.execute(
                "INSERT INTO external_run_subscriptions VALUES (?, ?, ?, 'parent', NULL, 'digest', ?, 'notified', 0, NULL, NULL, ?, NULL, NULL, NULL, 1, 1, NULL, 1)",
                (subscription_id, subscription_id, root, f"external-run:{subscription_id}", root + "/terminal-receipt.json"),
            )
    store = ExternalRunStore(path)
    reopened = ExternalRunStore(path)
    with reopened.escalations._connect() as conn:
        rows = conn.execute(
            "SELECT output_root, state FROM external_run_leases ORDER BY subscription_id"
        ).fetchall()
    assert [(row["output_root"], row["state"]) for row in rows] == [
        (str(tmp_path / "live"), "active"),
        (str(tmp_path / "released"), "released"),
    ]
    (tmp_path / "released").mkdir()
    assert store.subscribe(
        run_id="reclaimed", output_root=tmp_path / "released", return_session_id="parent"
    ).run.output_root == str(tmp_path / "released")
