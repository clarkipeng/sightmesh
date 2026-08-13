import pytest

from sightmesh.delivery import (
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
    claimed = store.claim(record.idempotency_key, now=9.0)
    assert claimed and claimed.claim_token
    failed_once = store.mark_failed(
        record.idempotency_key, claimed.claim_token, "offline", now=10.0
    )
    claimed = store.claim(record.idempotency_key, now=20.0)
    assert claimed and claimed.claim_token
    failed_twice = store.mark_failed(
        record.idempotency_key, claimed.claim_token, "offline", now=20.0
    )
    claimed = store.claim(record.idempotency_key, now=30.0)
    assert claimed and claimed.claim_token
    failed_third = store.mark_failed(
        record.idempotency_key, claimed.claim_token, "offline", now=30.0
    )
    assert failed_once.next_attempt_at == 12.0
    assert failed_twice.next_attempt_at == 24.0
    assert failed_third.next_attempt_at == 35.0
    assert store.claim_due(now=34.9) is None
    assert store.claim_due(now=35.0).idempotency_key == record.idempotency_key


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
    claimed = store.claim(record.idempotency_key, now=9.0)
    assert claimed and claimed.claim_token
    store.mark_failed(record.idempotency_key, claimed.claim_token, "offline", now=10.0)
    claimed = store.claim(record.idempotency_key, now=11.0)
    assert claimed and claimed.claim_token
    dead = store.mark_failed(
        record.idempotency_key, claimed.claim_token, "offline again", now=11.0
    )
    assert dead.status == "dead"
    assert dead.last_error == "offline again"
    assert dead.prompt == "visible follow-up"

    retried = store.retry(record.idempotency_key, now=12.0)
    assert retried.status == "pending"
    assert retried.attempt_count == 0
    assert retried.next_attempt_at == 12.0


def test_successful_closeout_clears_retained_prompt(tmp_path) -> None:
    path = tmp_path / "delivery.sqlite3"
    store = DeliveryStore(path)
    record = store.enqueue(_record())
    claimed = store.claim(record.idempotency_key, now=19.0)
    assert claimed and claimed.claim_token
    injected = store.mark_injected(
        record.idempotency_key, claimed.claim_token, now=20.0
    )
    assert injected.status == "injected"
    assert injected.prompt is None
    assert injected.prompt_bytes == 0
    assert path.stat().st_mode & 0o777 == 0o600


def test_competing_processors_cannot_claim_the_same_record(tmp_path) -> None:
    store = DeliveryStore(tmp_path / "delivery.sqlite3")
    record = store.enqueue(_record())
    first = store.claim_due(now=10.0, session_id="session-1")
    second = store.claim_due(now=10.0, session_id="session-1")
    assert first is not None
    assert first.idempotency_key == record.idempotency_key
    assert first.status == "inflight"
    assert first.claim_token
    assert second is None


def test_stale_claim_recovers_without_incrementing_attempts(tmp_path) -> None:
    store = DeliveryStore(
        tmp_path / "delivery.sqlite3",
        DeliveryPolicy(claim_timeout_seconds=10),
    )
    record = store.enqueue(_record())
    first = store.claim(record.idempotency_key, now=100.0)
    assert first and first.claim_token
    assert store.claim(record.idempotency_key, now=105.0) is None

    recovered = store.claim(record.idempotency_key, now=111.0)
    assert recovered is not None
    assert recovered.status == "inflight"
    assert recovered.claim_token and recovered.claim_token != first.claim_token
    assert recovered.attempt_count == 0


def test_wrong_claim_token_is_rejected(tmp_path) -> None:
    store = DeliveryStore(tmp_path / "delivery.sqlite3")
    record = store.enqueue(_record())
    claimed = store.claim(record.idempotency_key, now=10.0)
    assert claimed and claimed.claim_token

    with pytest.raises(DeliveryStoreError):
        store.mark_injected(record.idempotency_key, "wrong-token", now=11.0)
    with pytest.raises(DeliveryStoreError):
        store.mark_failed(record.idempotency_key, "wrong-token", "offline", now=12.0)

    current = store.get(record.idempotency_key)
    assert current is not None
    assert current.status == "inflight"
    assert current.claim_token == claimed.claim_token
    assert current.attempt_count == 0


def test_corruption_fails_closed(tmp_path) -> None:
    path = tmp_path / "delivery.sqlite3"
    path.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(DeliveryStoreError):
        DeliveryStore(path)
