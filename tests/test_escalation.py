from __future__ import annotations

from sightmesh.cdesktop import CdesktopError
from sightmesh.escalation import (
    EscalationStore,
    LauncherIdentity,
    detect_launcher,
    escalate,
)


class FakeClient:
    def __init__(self, sessions=None, workspaces=None):
        self.sessions_by_id = sessions or {}
        self.workspaces_by_id = workspaces or {}
        self.sent = []

    def session(self, session_id):
        if session_id not in self.sessions_by_id:
            raise CdesktopError(f"no such session {session_id}")
        return self.sessions_by_id[session_id]

    def workspace(self, workspace_id):
        return self.workspaces_by_id[workspace_id]

    def send(self, session_id, prompt, sender_session=None, *, dedupe_key=None, intent="continue"):
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


def test_detect_launcher_recognizes_cdesktop_from_session_env():
    identity = detect_launcher({"CDESKTOP_SESSION_ID": "session-a"})
    assert identity == LauncherIdentity(launcher="cdesktop", detail=None)


def test_detect_launcher_recognizes_conductor_when_no_cdesktop_session():
    identity = detect_launcher({"CONDUCTOR_WORKSPACE_NAME": "my-workspace"})
    assert identity == LauncherIdentity(launcher="external", detail="conductor")


def test_detect_launcher_falls_back_to_unknown_external():
    identity = detect_launcher({})
    assert identity == LauncherIdentity(launcher="external", detail="unknown")


def test_launcher_identity_is_captured_and_survives_restart(tmp_path):
    path = tmp_path / "escalations.sqlite3"
    store = EscalationStore(path)
    identity = detect_launcher({"CONDUCTOR_WORKSPACE_NAME": "my-workspace"})
    store.record_launcher(session_id="child-a", workspace_id="workspace-a", identity=identity)

    reopened = EscalationStore(path)
    assert reopened.get_launcher("child-a") == identity
    assert reopened.get_launcher("unknown-session") is None


def test_escalate_delivers_to_a_live_non_archived_parent(tmp_path):
    client = FakeClient(
        sessions={"parent-a": {"id": "parent-a", "workspace_id": "parent-workspace"}},
        workspaces={"parent-workspace": {"id": "parent-workspace", "archived": False}},
    )
    store = EscalationStore(tmp_path / "escalations.sqlite3")

    result = escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id="parent-a",
        message="STATUS: done",
        store=store,
    )

    assert result["delivered"] is True
    assert result["parent_session_id"] == "parent-a"
    assert len(client.sent) == 1
    assert client.sent[0]["session_id"] == "parent-a"
    assert client.sent[0]["intent"] == "replace", (
        "escalations must interrupt the parent's turn like the steer path did"
    )
    assert store.pending() == []


def test_escalate_parks_durably_when_no_cdesktop_parent_exists(tmp_path):
    client = FakeClient()
    store = EscalationStore(tmp_path / "escalations.sqlite3")

    result = escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id=None,
        message="STATUS: blocked, no parent",
        store=store,
    )

    assert result["delivered"] is False
    assert result["reason"] == "no_parent"
    assert client.sent == []
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0].child_session_id == "child-a"
    assert pending[0].reason == "no_parent"
    assert pending[0].status == "parked"


def test_escalate_never_delivers_into_an_archived_parent_workspace(tmp_path):
    client = FakeClient(
        sessions={"parent-a": {"id": "parent-a", "workspace_id": "parent-workspace"}},
        workspaces={"parent-workspace": {"id": "parent-workspace", "archived": True}},
    )
    store = EscalationStore(tmp_path / "escalations.sqlite3")

    result = escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id="parent-a",
        message="STATUS: parent retired mid-flight",
        store=store,
    )

    assert result["delivered"] is False
    assert result["reason"] == "parent_archived"
    assert client.sent == [], "must never deliver into an archived/retired session"
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0].recorded_parent_session_id == "parent-a"
    assert pending[0].reason == "parent_archived"


def test_escalate_parks_when_recorded_parent_session_no_longer_resolves(tmp_path):
    client = FakeClient()  # parent-a is not in sessions_by_id -> deleted/retired
    store = EscalationStore(tmp_path / "escalations.sqlite3")

    result = escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id="parent-a",
        message="STATUS: parent gone",
        store=store,
    )

    assert result["delivered"] is False
    assert result["reason"] == "parent_unreachable"
    assert client.sent == []


def test_escalate_parked_decision_survives_process_restart(tmp_path):
    path = tmp_path / "escalations.sqlite3"
    client = FakeClient()
    first_process_store = EscalationStore(path)
    escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id=None,
        message="STATUS: blocked before restart",
        store=first_process_store,
    )

    restarted_store = EscalationStore(path)
    pending = restarted_store.pending()
    assert len(pending) == 1
    assert pending[0].message == "STATUS: blocked before restart"

    resolved = restarted_store.resolve(pending[0].escalation_id)
    assert resolved.status == "resolved"
    assert restarted_store.pending() == []


def test_escalate_is_idempotent_on_repeated_identical_retries(tmp_path):
    client = FakeClient()
    store = EscalationStore(tmp_path / "escalations.sqlite3")

    first = escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id=None,
        message="STATUS: retry me",
        store=store,
    )
    second = escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id=None,
        message="STATUS: retry me",
        store=store,
    )

    assert first["parked"]["escalation_id"] == second["parked"]["escalation_id"]
    assert len(store.pending()) == 1
