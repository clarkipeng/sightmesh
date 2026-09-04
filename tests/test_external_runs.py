"""Acceptance coverage for issue #55's runner-owned durable wake contract."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sightmesh.cdesktop import CdesktopError
from sightmesh.cli import parser
from sightmesh.external_runs import (
    ExternalRunError,
    ExternalRunReconciler,
    ExternalRunStore,
)


class Client:
    def __init__(self, reachable: bool = True, fail_send: bool = False) -> None:
        self.reachable, self.fail_send, self.sent = reachable, fail_send, []

    def session(self, value):
        if not self.reachable:
            raise CdesktopError("unreachable")
        return {"id": value, "workspace_id": "ws-parent"}

    def workspace(self, value):
        return {"id": value, "archived": False}

    def send(self, session_id, message, sender_session=None, **kwargs):
        if self.fail_send:
            raise CdesktopError("retry")
        self.sent.append((session_id, message, kwargs["dedupe_key"]))
        return {"ok": True}


def subscribed(tmp_path: Path):
    store = ExternalRunStore(tmp_path / "state.sqlite3")
    result = store.subscribe(
        run_id="r1", output_root=tmp_path / "output", return_session_id="parent"
    )
    return store, result


def receipt(run, outcome="completed"):
    Path(run.receipt_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "subscription_id": run.subscription_id,
                "run_id": run.run_id,
                "terminal_state": outcome,
                "finished_at": "2026-09-03T00:00:00Z",
            }
        )
    )


def test_subscription_atomically_leases_empty_output_root_before_runner_activity(
    tmp_path,
):
    store, first = subscribed(tmp_path)
    with pytest.raises(ExternalRunError):
        store.subscribe(
            run_id="r2", output_root=tmp_path / "output", return_session_id="parent"
        )
    assert store.get(first.run.subscription_id).state == "subscribed"


def test_binding_requires_the_runner_capability_and_a_live_stable_fingerprint(tmp_path):
    store, result = subscribed(tmp_path)
    with pytest.raises(ExternalRunError):
        store.bind(
            result.run.subscription_id,
            writer_capability="other",
            pid=2,
            process_fingerprint="start",
            observer=lambda _: "start",
        )
    bound = store.bind(
        result.run.subscription_id,
        writer_capability=result.writer_capability,
        pid=2,
        process_fingerprint="start",
        observer=lambda _: "start",
    )
    assert bound.state == "running" and bound.pid == 2


def test_terminal_receipt_is_authoritative_and_releases_lease_only_after_preservation(
    tmp_path,
):
    store, result = subscribed(tmp_path)
    receipt(result.run, "failed")
    ExternalRunReconciler(Client(), store).reconcile()
    saved = store.get(result.run.subscription_id)
    assert saved.state == "notified" and saved.outcome == "failed"
    with store.escalations._connect() as conn:
        assert (
            conn.execute("SELECT state FROM external_run_leases").fetchone()[0]
            == "released"
        )


def test_disappeared_process_without_receipt_is_typed_lost_unknown_not_a_guessed_result(
    tmp_path,
):
    store, result = subscribed(tmp_path)
    run = store.bind(
        result.run.subscription_id,
        writer_capability=result.writer_capability,
        pid=2,
        process_fingerprint="old",
        observer=lambda _: "old",
    )
    ExternalRunReconciler(Client(), store, observer=lambda _: "new").reconcile_one(run)
    assert store.get(run.subscription_id).outcome == "lost/unknown"


def test_restart_reloads_durable_rows_and_delivers_the_receipt(tmp_path):
    store, result = subscribed(tmp_path)
    receipt(result.run)
    restarted = ExternalRunStore(store.path)
    ExternalRunReconciler(Client(), restarted).reconcile()
    assert restarted.get(result.run.subscription_id).state == "notified"


def test_duplicate_receipt_preserves_first_evidence_and_delivery_retries_under_one_key(
    tmp_path,
):
    store, result = subscribed(tmp_path)
    receipt(result.run, "completed")
    ExternalRunReconciler(Client(fail_send=True), store).reconcile()
    first = store.get(result.run.subscription_id)
    receipt(first, "failed")
    client = Client()
    ExternalRunReconciler(client, store).reconcile()
    saved = store.get(first.subscription_id)
    assert (
        saved.outcome == "completed"
        and saved.diagnostic == "duplicate terminal receipt differs"
    )
    assert client.sent[0][2] == result.run.dedupe_key


def test_pid_reuse_is_rejected_as_loss(tmp_path):
    store, result = subscribed(tmp_path)
    run = store.bind(
        result.run.subscription_id,
        writer_capability=result.writer_capability,
        pid=2,
        process_fingerprint="first",
        observer=lambda _: "first",
    )
    ExternalRunReconciler(Client(), store, observer=lambda _: "reused").reconcile_one(
        run
    )
    assert store.get(run.subscription_id).outcome == "lost/unknown"


def test_competing_root_claim_has_one_winner_and_keeps_its_directory(tmp_path):
    store = ExternalRunStore(tmp_path / "state.sqlite3")

    def claim(key):
        return store.subscribe(
            run_id=key, output_root=tmp_path / "root", return_session_id="parent"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(claim, key) for key in ("a", "b")]
    assert sum(f.exception() is None for f in outcomes) == 1
    assert (tmp_path / "root").is_dir()


def test_launch_failure_receipt_before_bind_and_unreachable_parent_both_stay_durable(
    tmp_path,
):
    store, result = subscribed(tmp_path)
    receipt(result.run, "failed")
    ExternalRunReconciler(Client(reachable=False), store).reconcile()
    assert store.get(result.run.subscription_id).state == "notified"
    assert store.escalations.pending()[0].dedupe_key == result.run.dedupe_key


def test_cli_exposes_the_minimal_external_run_verbs():
    assert (
        parser()
        .parse_args(
            [
                "run",
                "subscribe",
                "--run-id",
                "r",
                "--output-root",
                "/tmp/r",
                "--return-session",
                "p",
            ]
        )
        .run_action
        == "subscribe"
    )
    assert (
        parser()
        .parse_args(
            [
                "run",
                "bind",
                "subscription",
                "--writer-capability",
                "cap",
                "--pid",
                "1",
                "--process-fingerprint",
                "start",
            ]
        )
        .run_action
        == "bind"
    )
    assert parser().parse_args(["run", "show"]).run_action == "show"
    assert parser().parse_args(["run", "reconcile"]).run_action == "reconcile"
