import asyncio
import json

from sightmesh import bridge as bridge_module
from sightmesh.bridge import (
    BridgedSession,
    BridgeSupervisor,
    RepowireSessionBridge,
    _backend,
    _dedupe_key,
    _peer_name,
)
from sightmesh.pool.core import PoolError


class FakeClient:
    def __init__(self) -> None:
        self.sent = []
        self.failures = []

    def send(
        self,
        session_id,
        prompt,
        sender_session,
        *,
        dedupe_key=None,
        intent=None,
    ):
        if self.failures:
            raise RuntimeError(self.failures.pop(0))
        self.sent.append((session_id, prompt, sender_session, dedupe_key, intent))
        return {"state": "pending"}


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


def test_plain_ask_becomes_durable_cdesktop_command() -> None:
    bridge, client = _bridge()
    ws = FakeWebSocket()
    message = {
        "type": "ask",
        "delivery_id": "delivery-1",
        "correlation_id": "correlation-1",
        "from_peer": "sender",
        "text": "Review the change",
    }
    asyncio.run(bridge._handle(ws, message))

    assert client.sent[0][0] == "session-123456"
    assert "## Request from @sender" in client.sent[0][1]
    assert "sightmesh bridge-reply correlation-1" in client.sent[0][1]
    assert client.sent[0][3] == "repowire:session-123456:ask:delivery-1"
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
    assert "--question --message 'RESULT'" in client.sent[0][1]


def test_duplicate_delivery_uses_the_same_cdesktop_dedupe_key() -> None:
    bridge, client = _bridge()
    ws = FakeWebSocket()
    message = {
        "type": "ask",
        "delivery_id": "delivery-1",
        "from_peer": "sender",
        "text": "Review the change",
    }
    asyncio.run(bridge._handle(ws, message))
    asyncio.run(bridge._handle(ws, message))

    assert client.sent[0][3] == client.sent[1][3]
    assert [frame["status"] for frame in ws.frames] == ["injected", "injected"]


def test_delivery_failure_is_visible_to_repowire() -> None:
    bridge, client = _bridge()
    client.failures = ["offline"]
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

    assert ws.frames == [
        {
            "type": "delivery_ack",
            "delivery_id": "delivery-2",
            "message_type": "ask",
            "status": "failed",
            "detail": "offline",
        },
        {
            "type": "error",
            "correlation_id": "correlation-2",
            "error": "offline",
        },
    ]


def test_unidentified_messages_still_dedupe_deterministically() -> None:
    message = {"from_peer": "sender", "text": "same"}
    first = _dedupe_key("session", "notify", message)
    second = _dedupe_key("session", "notify", dict(message))
    assert first == second


def test_an_unbridged_child_is_still_reconciled_but_never_stopped(monkeypatch) -> None:
    """Why: an unbridged child still needs its parent notified and its quota
    failure reconciled - that half is unchanged. What it must NOT get is the
    old "stall recovery": a wall-clock reaper that stopped a running execution
    from inside the same tick as the wake-only detector. A per-tick failure in
    one session must also not stop the tick from finishing the others."""

    class StallClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.process = {
                "id": "process-1",
                "status": "running",
                "run_reason": "coding_agent",
                "started_at": "2026-08-14T00:00:00Z",
            }

        def workspaces(self):
            return [{"id": "child-workspace", "archived": False}]

        def sessions(self, _workspace_id):
            return [{"id": "child", "parent_session_id": "parent"}]

        def execution_processes(self, _session_id):
            return [self.process]

        def normalized_snapshot(self, _process_id):
            return {"complete": True, "entries": []}

        def execution_process(self, _process_id):
            return self.execution_processes("child")[0]

        def stop_execution(self, process_id, *, dedupe_key=None):
            self.stopped.append(process_id)
            self.execution_process("child")["status"] = "killed"

    client = StallClient()
    client.stopped = []
    supervisor = BridgeSupervisor(client, "ws://127.0.0.1:8377/ws")
    reconciled = []

    def reconcile_quota_failure(session_id):
        reconciled.append(session_id)
        if len(reconciled) == 1:
            raise PoolError("pool state unavailable")

    supervisor.managed_tasks.reconcile_quota_failure = reconcile_quota_failure
    monkeypatch.setattr(bridge_module, "enabled_workspaces", lambda: set())
    monkeypatch.setattr(
        bridge_module.leases, "sync_active_workspaces", lambda *_args, **_kwargs: []
    )

    asyncio.run(supervisor.reconcile())
    asyncio.run(supervisor.reconcile())

    assert client.stopped == []
    assert client.execution_processes("child")[0]["status"] == "running"
    # No terminal notification either: the only reason the parent used to hear
    # about this child was the kernel's own kill, reported back as the child's
    # death. A healthy running child is not news.
    assert client.sent == []
    assert reconciled == ["child", "child"]
    assert supervisor.tasks == {}


def test_each_executor_maps_to_its_own_repowire_backend() -> None:
    from sightmesh.bridge import _backend

    assert _backend("CLAUDE_CODE") == "claude-code"
    assert _backend("CODEX") == "codex"
    assert _backend("OPENCODE") == "opencode"
