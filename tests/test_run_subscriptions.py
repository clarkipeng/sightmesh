from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sightmesh.cdesktop import CdesktopError
from sightmesh.run_subscriptions import (
    RunReconciler,
    RunSubscriptionError,
    RunSubscriptionStore,
)


class Client:
    def __init__(self, *, reachable: bool = True, fail_sends: int = 0) -> None:
        self.reachable = reachable
        self.fail_sends = fail_sends
        self.sent: list[dict[str, object]] = []

    def session(self, session_id: str) -> dict[str, str]:
        if not self.reachable:
            raise CdesktopError("parent unreachable")
        return {"id": session_id, "workspace_id": "workspace-parent"}

    def workspace(self, workspace_id: str) -> dict[str, object]:
        return {"id": workspace_id, "archived": False}

    def send(
        self,
        session_id: str,
        prompt: str,
        sender_session: str | None = None,
        *,
        dedupe_key: str | None = None,
        intent: str = "continue",
    ) -> dict[str, bool]:
        if self.fail_sends:
            self.fail_sends -= 1
            raise CdesktopError("send failed")
        self.sent.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "sender_session": sender_session,
                "dedupe_key": dedupe_key,
                "intent": intent,
            }
        )
        return {"ok": True}


def _subscribe(tmp_path: Path) -> tuple[RunSubscriptionStore, object]:
    store = RunSubscriptionStore(tmp_path / "escalations.sqlite3")
    result = store.subscribe(
        run_id="run-a",
        output_root=tmp_path / "run-a",
        return_session_id="parent-a",
        return_workspace_id="workspace-parent",
    )
    return store, result


def _receipt(record, *, terminal_state: str = "completed", exit_code: int | None = 0) -> None:
    Path(record.receipt_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "subscription_id": record.subscription_id,
                "run_id": record.run_id,
                "terminal_state": terminal_state,
                "exit_code": exit_code,
                "finished_at": "2026-08-27T12:00:00Z",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_authoritative_terminal_receipt_wins_over_missing_process(tmp_path: Path) -> None:
    store, result = _subscribe(tmp_path)
    record = store.bind(
        result.subscription.subscription_id,
        writer_capability=result.writer_capability,
        pid=1234,
        process_start="start-a",
        observer=lambda _pid: "start-a",
    )
    _receipt(record)
    client = Client()

    state = RunReconciler(client, store, observer=lambda _pid: None).reconcile_one(record)

    assert state["state"] == "notified"
    saved = store.get(record.subscription_id)
    assert saved.terminal_state == "completed"
    assert saved.receipt_digest
    assert "terminal/completed" in str(client.sent[0]["prompt"])


def test_missing_receipt_after_process_disappears_is_lost_unknown(tmp_path: Path) -> None:
    store, result = _subscribe(tmp_path)
    record = store.bind(
        result.subscription.subscription_id,
        writer_capability=result.writer_capability,
        pid=1234,
        process_start="start-a",
        observer=lambda _pid: "start-a",
    )

    RunReconciler(Client(), store, observer=lambda _pid: None).reconcile_one(record)

    saved = store.get(record.subscription_id)
    assert saved.state == "notified"
    assert saved.terminal_state is None
    assert saved.diagnostic == "process fingerprint disappeared"
    assert saved.lease_released_at is not None


def test_receipt_published_during_final_liveness_check_wins(tmp_path: Path) -> None:
    store, result = _subscribe(tmp_path)
    record = store.bind(
        result.subscription.subscription_id,
        writer_capability=result.writer_capability,
        pid=1234,
        process_start="start-a",
        observer=lambda _pid: "start-a",
    )

    def exits_after_receipt(_pid: int) -> None:
        _receipt(record)
        return None

    client = Client()
    RunReconciler(client, store, observer=exits_after_receipt).reconcile_one(record)

    saved = store.get(record.subscription_id)
    assert saved.state == "notified"
    assert saved.terminal_state == "completed"
    assert "terminal/completed" in str(client.sent[0]["prompt"])


def test_restart_reloads_subscription_and_delivers_terminal_receipt(tmp_path: Path) -> None:
    path = tmp_path / "escalations.sqlite3"
    store = RunSubscriptionStore(path)
    result = store.subscribe(
        run_id="run-a",
        output_root=tmp_path / "run-a",
        return_session_id="parent-a",
    )
    record = store.bind(
        result.subscription.subscription_id,
        writer_capability=result.writer_capability,
        pid=1234,
        process_start="start-a",
        observer=lambda _pid: "start-a",
    )
    _receipt(record)

    restarted = RunSubscriptionStore(path)
    RunReconciler(Client(), restarted, observer=lambda _pid: "start-a").reconcile()

    assert restarted.get(record.subscription_id).state == "notified"


def test_duplicate_receipt_with_different_content_preserves_first_evidence(
    tmp_path: Path,
) -> None:
    store, result = _subscribe(tmp_path)
    record = result.subscription
    _receipt(record, terminal_state="completed", exit_code=0)
    RunReconciler(Client(fail_sends=1), store).reconcile_one(record)
    first = store.get(record.subscription_id)
    assert first.state == "terminal"

    _receipt(record, terminal_state="failed", exit_code=1)
    RunReconciler(Client(), store).reconcile_one(record)

    saved = store.get(record.subscription_id)
    assert saved.receipt_digest == first.receipt_digest
    assert saved.terminal_state == "completed"
    assert saved.diagnostic == "duplicate terminal receipt differs"


def test_repeated_delivery_retries_with_stable_dedupe_key(tmp_path: Path) -> None:
    store, result = _subscribe(tmp_path)
    record = result.subscription
    _receipt(record)
    failing = Client(fail_sends=1)

    RunReconciler(failing, store).reconcile_one(record)
    assert store.get(record.subscription_id).state == "terminal"

    succeeding = Client()
    RunReconciler(succeeding, store).reconcile_one(record)

    assert succeeding.sent[0]["dedupe_key"] == result.subscription.dedupe_key
    assert store.get(record.subscription_id).state == "notified"


def test_pid_reuse_marks_running_subscription_lost_unknown(tmp_path: Path) -> None:
    store, result = _subscribe(tmp_path)
    record = store.bind(
        result.subscription.subscription_id,
        writer_capability=result.writer_capability,
        pid=1234,
        process_start="old-start",
        observer=lambda _pid: "old-start",
    )

    RunReconciler(Client(), store, observer=lambda _pid: "new-start").reconcile_one(record)

    saved = store.get(record.subscription_id)
    assert saved.state == "notified"
    assert saved.diagnostic == "process fingerprint disappeared"


def test_duplicate_output_root_rejected_atomically(tmp_path: Path) -> None:
    store, _result = _subscribe(tmp_path)
    with pytest.raises(RunSubscriptionError):
        store.subscribe(
            run_id="run-b",
            output_root=tmp_path / "run-a",
            return_session_id="parent-a",
        )
    assert [record.run_id for record in store.all()] == ["run-a"]


def test_competing_output_root_claim_keeps_winners_directory(tmp_path: Path) -> None:
    store = RunSubscriptionStore(tmp_path / "escalations.sqlite3")
    output_root = tmp_path / "contended"

    def subscribe(run_id: str):
        return store.subscribe(
            run_id=run_id,
            output_root=output_root,
            return_session_id="parent-a",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(subscribe, run_id) for run_id in ("run-a", "run-b")]
    results = []
    errors = []
    for attempt in attempts:
        try:
            results.append(attempt.result())
        except RunSubscriptionError as exc:
            errors.append(exc)

    assert len(results) == 1
    assert len(errors) == 1
    assert output_root.is_dir()
    assert len(store.all()) == 1


def test_launch_failure_is_failed_receipt_before_bind(tmp_path: Path) -> None:
    store, result = _subscribe(tmp_path)
    _receipt(result.subscription, terminal_state="failed", exit_code=None)

    RunReconciler(Client(), store).reconcile_one(result.subscription)

    saved = store.get(result.subscription.subscription_id)
    assert saved.state == "notified"
    assert saved.terminal_state == "failed"
    assert saved.exit_code is None


def test_unreachable_parent_parks_wake_and_marks_notified(tmp_path: Path) -> None:
    store, result = _subscribe(tmp_path)
    _receipt(result.subscription)

    RunReconciler(Client(reachable=False), store).reconcile_one(result.subscription)

    saved = store.get(result.subscription.subscription_id)
    assert saved.state == "notified"
    parked = store.escalations.pending()
    assert len(parked) == 1
    assert parked[0].dedupe_key == result.subscription.dedupe_key
    assert parked[0].reason == "parent_unreachable"


def test_bad_receipt_is_isolated_and_does_not_block_other_wakes(tmp_path: Path) -> None:
    store = RunSubscriptionStore(tmp_path / "escalations.sqlite3")
    bad = store.subscribe(
        run_id="bad",
        output_root=tmp_path / "bad",
        return_session_id="parent-a",
    ).subscription
    Path(bad.receipt_path).mkdir()
    good = store.subscribe(
        run_id="good",
        output_root=tmp_path / "good",
        return_session_id="parent-a",
    ).subscription
    _receipt(good)
    client = Client()

    results = RunReconciler(client, store).reconcile()

    assert [result["state"] for result in results] == ["notified", "notified"]
    assert store.get(bad.subscription_id).terminal_state is None
    assert store.get(bad.subscription_id).diagnostic.startswith(
        "Cannot read terminal receipt"
    )
    assert store.get(good.subscription_id).terminal_state == "completed"
    assert len(client.sent) == 2
