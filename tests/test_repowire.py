import json

from agent_deck import repowire


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return b'{"ok":true}'


def test_plain_reply_uses_ack_and_auth(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update({"request": request, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setenv("REPOWIRE_AUTH_TOKEN", "secret")
    monkeypatch.setattr(repowire, "urlopen", fake_urlopen)
    result = repowire.reply(
        "cid",
        "done",
        from_peer="worker",
        question=False,
    )
    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:8377/ack"
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data) == {
        "correlation_id": "cid",
        "message": "done",
        "from_peer": "worker",
    }
    assert result == {"ok": True}


def test_question_reply_uses_answer(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return FakeResponse()

    monkeypatch.delenv("REPOWIRE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(repowire, "urlopen", fake_urlopen)
    repowire.reply("cid", "answer", from_peer="worker", question=True)
    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:8377/answer"
    assert json.loads(request.data) == {"correlation_id": "cid", "text": "answer"}
