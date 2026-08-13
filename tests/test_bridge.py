import asyncio
import json

from sightmesh.bridge import BridgedSession, RepowireSessionBridge, _backend, _peer_name
from sightmesh.delivery import DeliveryPolicy, DeliveryStore


class FakeClient:
    def __init__(self) -> None:
        self.sent = []
        self.failures = []

    def send(self, session_id, prompt, sender_session):
        if self.failures:
            raise RuntimeError(self.failures.pop(0))
        self.sent.append((session_id, prompt, sender_session))
        return {"accepted": True}


class FakeWebSocket:
    def __init__(self) -> None:
        self.frames = []

    async def send(self, frame: str) -> None:
        self.frames.append(json.loads(frame))


def _bridge(tmp_path) -> tuple[RepowireSessionBridge, FakeClient, DeliveryStore]:
    client = FakeClient()
    store = DeliveryStore(
        tmp_path / "delivery.sqlite3",
        DeliveryPolicy(max_attempts=2, base_backoff_seconds=0, max_backoff_seconds=0),
    )
    bridge = RepowireSessionBridge(
        client,
        BridgedSession(
            workspace={"id": "workspace", "name": "Bridge Test"},
            session={"id": "session-123456", "executor": "CODEX"},
            path="/tmp/repo",
        ),
        "ws://127.0.0.1:8377/ws",
        store,
    )
    return bridge, client, store


def test_peer_metadata_is_stable_and_normalized() -> None:
    workspace = {"name": "Bridge Test!"}
    session = {"id": "abcdef123", "executor": "CLAUDE_CODE"}
    assert _peer_name(workspace, session) == "cd-Bridge-Test-abcdef"
    assert _backend(session["executor"]) == "claude-code"


def test_plain_ask_becomes_visible_follow_up_and_ack_command(tmp_path) -> None:
    bridge, client, _store = _bridge(tmp_path)
    ws = FakeWebSocket()
    asyncio.run(
        bridge._handle(
            ws,
            {
                "type": "ask",
                "delivery_id": "delivery-1",
                "correlation_id": "correlation-1",
                "from_peer": "sender",
                "text": "Review the change",
            },
        )
    )
    assert client.sent[0][0] == "session-123456"
    assert "## Request from @sender" in client.sent[0][1]
    assert "sightmesh bridge-reply correlation-1" in client.sent[0][1]
    assert "--question" not in client.sent[0][1]
    assert ws.frames == [
        {
            "type": "delivery_ack",
            "delivery_id": "delivery-1",
            "message_type": "ask",
            "status": "injected",
        }
    ]


def test_structured_question_uses_answer_endpoint_flag(tmp_path) -> None:
    bridge, client, _store = _bridge(tmp_path)
    ws = FakeWebSocket()
    asyncio.run(
        bridge._handle(
            ws,
            {
                "type": "ask",
                "correlation_id": "correlation-2",
                "from_peer": "cli",
                "text": "Return the proof string",
                "question": {"kind": "text"},
            },
        )
    )
    assert "--question --message 'RESULT'" in client.sent[0][1]


def test_duplicate_delivery_after_injection_is_not_sent_twice(tmp_path) -> None:
    bridge, client, _store = _bridge(tmp_path)
    ws = FakeWebSocket()
    message = {
        "type": "ask",
        "delivery_id": "delivery-1",
        "correlation_id": "correlation-1",
        "from_peer": "sender",
        "text": "Review the change",
    }
    asyncio.run(bridge._handle(ws, message))
    asyncio.run(bridge._handle(ws, message))
    assert len(client.sent) == 1
    assert [frame["status"] for frame in ws.frames] == ["injected", "injected"]


def test_failed_delivery_dead_letters_and_sends_structured_error(tmp_path) -> None:
    bridge, client, _store = _bridge(tmp_path)
    client.failures = ["offline", "still offline"]
    ws = FakeWebSocket()
    asyncio.run(
        bridge._handle(
            ws,
            {
                "type": "ask",
                "delivery_id": "delivery-2",
                "correlation_id": "correlation-2",
                "from_peer": "sender",
                "text": "Review the change",
            },
        )
    )
    asyncio.run(bridge._process_due(ws))
    assert ws.frames[-2] == {
        "type": "delivery_ack",
        "delivery_id": "delivery-2",
        "message_type": "ask",
        "status": "failed",
        "detail": "still offline",
    }
    assert ws.frames[-1] == {
        "type": "error",
        "correlation_id": "correlation-2",
        "error": "still offline",
    }


def test_duplicate_pending_delivery_respects_backoff(tmp_path) -> None:
    client = FakeClient()
    store = DeliveryStore(
        tmp_path / "delivery.sqlite3",
        DeliveryPolicy(max_attempts=3, base_backoff_seconds=100, max_backoff_seconds=100),
    )
    bridge = RepowireSessionBridge(
        client,
        BridgedSession(
            workspace={"id": "workspace", "name": "Bridge Test"},
            session={"id": "session-123456", "executor": "CODEX"},
            path="/tmp/repo",
        ),
        "ws://127.0.0.1:8377/ws",
        store,
    )
    client.failures = ["offline"]
    ws = FakeWebSocket()
    message = {
        "type": "ask",
        "delivery_id": "delivery-3",
        "correlation_id": "correlation-3",
        "from_peer": "sender",
        "text": "Review the change",
    }
    asyncio.run(bridge._handle(ws, message))
    asyncio.run(bridge._handle(ws, message))
    assert client.sent == []
