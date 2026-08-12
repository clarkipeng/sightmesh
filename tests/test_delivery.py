import pytest

from agent_deck.delivery import (
    DeliveryCapacityError,
    DeliveryPolicy,
    DeliveryStore,
    DeliveryStoreError,
    make_record,
)


def _record(**overrides):
    values = {
        "session_id": "session-1",
        "message_type": "ask",
        "prompt": "visible follow-up",
        "delivery_id": "delivery-1",
        "correlation_id": "correlation-1",
        "from_peer": "sender",
        "text": "hello",
        "now": 100.0,
    }
    values.update(overrides)
    return make_record(**values)


def test_duplicate_delivery_reuses_existing_record(tmp_path) -> None:
    store = DeliveryStore(tmp_path / "delivery.sqlite3")
    first = store.enqueue(_record())
    second = store.enqueue(_record(prompt="different prompt"))
    assert second.idempotency_key == first.idempotency_key
    assert second.prompt == "visible follow-up"
    assert len(store.list()) == 1


def test_restart_persistence_preserves_pending_record(tmp_path) -> None:
    path = tmp_path / "delivery.sqlite3"
    DeliveryStore(path).enqueue(_record())
    reopened = DeliveryStore(path)
    rows = reopened.list(status="pending")
    assert len(rows) == 1
    assert rows[0].correlation_id == "correlation-1"


def test_retry_timing_uses_capped_exponential_backoff(tmp_path) -> None:
    store = DeliveryStore(
        tmp_path / "delivery.sqlite3",
        DeliveryPolicy(base_backoff_seconds=2, max_backoff_seconds=5),
    )
    record = store.enqueue(_record())
    failed_once = store.mark_failed(record.idempotency_key, "offline", now=10.0)
    failed_twice = store.mark_failed(record.idempotency_key, "offline", now=20.0)
    failed_third = store.mark_failed(record.idempotency_key, "offline", now=30.0)
    assert failed_once.next_attempt_at == 12.0
    assert failed_twice.next_attempt_at == 24.0
    assert failed_third.next_attempt_at == 35.0
    assert store.due(now=34.9) == []
    assert store.due(now=35.0)[0].idempotency_key == record.idempotency_key


def test_capacity_limits_pending_count_and_bytes(tmp_path) -> None:
    by_count = DeliveryStore(
        tmp_path / "count.sqlite3",
        DeliveryPolicy(max_pending=1, max_pending_bytes=1000),
    )
    by_count.enqueue(_record(delivery_id="d1", correlation_id="c1"))
    with pytest.raises(DeliveryCapacityError):
        by_count.enqueue(_record(delivery_id="d2", correlation_id="c2"))

    by_bytes = DeliveryStore(
        tmp_path / "bytes.sqlite3",
        DeliveryPolicy(max_pending=10, max_pending_bytes=5),
    )
    with pytest.raises(DeliveryCapacityError):
        by_bytes.enqueue(_record(prompt="too large"))


def test_dead_lettering_is_inspectable_and_retryable(tmp_path) -> None:
    store = DeliveryStore(
        tmp_path / "delivery.sqlite3",
        DeliveryPolicy(max_attempts=2, base_backoff_seconds=0),
    )
    record = store.enqueue(_record())
    store.mark_failed(record.idempotency_key, "offline", now=10.0)
    dead = store.mark_failed(record.idempotency_key, "offline again", now=11.0)
    assert dead.status == "dead"
    assert dead.last_error == "offline again"
    assert dead.prompt == "visible follow-up"

    retried = store.retry(record.idempotency_key, now=12.0)
    assert retried.status == "pending"
    assert retried.attempt_count == 0
    assert retried.next_attempt_at == 12.0


def test_successful_closeout_clears_retained_prompt(tmp_path) -> None:
    store = DeliveryStore(tmp_path / "delivery.sqlite3")
    record = store.enqueue(_record())
    injected = store.mark_injected(record.idempotency_key, now=20.0)
    assert injected.status == "injected"
    assert injected.prompt is None
    assert injected.prompt_bytes == 0


def test_corruption_fails_closed(tmp_path) -> None:
    path = tmp_path / "delivery.sqlite3"
    path.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(DeliveryStoreError):
        DeliveryStore(path)
