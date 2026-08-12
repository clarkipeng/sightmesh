import asyncio
import json

from agent_deck.bridge import BridgedSession, RepowireSessionBridge, _backend, _peer_name


class FakeClient:
    def __init__(self) -> None:
        self.sent = []

    def send(self, session_id, prompt, sender_session):
        self.sent.append((session_id, prompt, sender_session))
        return {"accepted": True}


class FakeWebSocket:
    def __init__(self) -> None:
        self.frames = []

    async def send(self, frame: str) -> None:
        self.frames.append(json.loads(frame))


def _bridge() -> tuple[RepowireSessionBridge, FakeClient]:
    client = FakeClient()
    bridge = RepowireSessionBridge(
        client,
        BridgedSession(
            workspace={"id": "workspace", "name": "Bridge Test"},
            session={"id": "session-123456", "executor": "CODEX"},
            path="/tmp/repo",
        ),
        "ws://127.0.0.1:8377/ws",
    )
    return bridge, client


def test_peer_metadata_is_stable_and_normalized() -> None:
    workspace = {"name": "Bridge Test!"}
    session = {"id": "abcdef123", "executor": "CLAUDE_CODE"}
    assert _peer_name(workspace, session) == "cd-Bridge-Test-abcdef"
    assert _backend(session["executor"]) == "claude-code"


def test_plain_ask_becomes_visible_follow_up_and_ack_command() -> None:
    bridge, client = _bridge()
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
    assert "Repowire ask from @sender" in client.sent[0][1]
    assert "agent-deck bridge-reply correlation-1" in client.sent[0][1]
    assert "--question" not in client.sent[0][1]
    assert ws.frames == [
        {
            "type": "delivery_ack",
            "delivery_id": "delivery-1",
            "message_type": "ask",
            "status": "injected",
        }
    ]


def test_structured_question_uses_answer_endpoint_flag() -> None:
    bridge, client = _bridge()
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
    assert "--question --message 'REPLY'" in client.sent[0][1]
