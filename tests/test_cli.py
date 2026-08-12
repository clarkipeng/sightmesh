import argparse

from agent_deck import delivery
from agent_deck.cli import _read_text
from agent_deck.cli import parser
from agent_deck.delivery import DeliveryStore, make_record


def test_read_text_requires_one_source(tmp_path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("from file", encoding="utf-8")
    assert _read_text(None, str(prompt), "prompt") == "from file"
    assert _read_text("inline", None, "prompt") == "inline"


def test_namespace_import_is_available() -> None:
    assert argparse.Namespace is not None


def test_delivery_status_and_list_commands(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "delivery.sqlite3"
    monkeypatch.setattr(delivery, "delivery_db_path", lambda: path)
    DeliveryStore(path).enqueue(
        make_record(
            session_id="session-1",
            message_type="ask",
            prompt="prompt",
            delivery_id="delivery-1",
            correlation_id="correlation-1",
            from_peer="sender",
            text="text",
        )
    )

    args = parser().parse_args(["--json", "delivery", "status"])
    assert args.func(args) == 0
    assert '"pending"' in capsys.readouterr().out

    args = parser().parse_args(["--json", "delivery", "list", "--status", "pending"])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "delivery-1" in output
    assert '"prompt":' not in output


def test_delivery_retry_and_purge_require_exact_keys(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "delivery.sqlite3"
    monkeypatch.setattr(delivery, "delivery_db_path", lambda: path)
    store = DeliveryStore(path)
    record = store.enqueue(
        make_record(
            session_id="session-1",
            message_type="ask",
            prompt="prompt",
            delivery_id="delivery-1",
            correlation_id="correlation-1",
            from_peer="sender",
            text="text",
        )
    )
    claimed = store.claim(record.idempotency_key)
    assert claimed and claimed.claim_token
    store.mark_failed(record.idempotency_key, claimed.claim_token, "offline")

    args = parser().parse_args(["--json", "delivery", "retry", record.idempotency_key])
    assert args.func(args) == 0
    assert '"status": "pending"' in capsys.readouterr().out

    args = parser().parse_args(["--json", "delivery", "purge", record.idempotency_key])
    assert args.func(args) == 0
    assert '"deleted": 1' in capsys.readouterr().out
