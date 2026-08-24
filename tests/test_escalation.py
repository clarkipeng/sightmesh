from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sightmesh.cdesktop import CdesktopError
from sightmesh.escalation import (
    EscalationClass,
    EscalationStore,
    EscalationStoreError,
    LauncherIdentity,
    classify_escalation,
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


def test_classify_routine_status_and_completion_as_continue():
    for message in ("STATUS: done", "STATUS: progress update", "Completed lane J merge"):
        assert classify_escalation(message) == EscalationClass(
            kind="routine", intent="continue"
        )


def test_classify_explicit_blocked_and_decision_as_replace():
    for message in ("BLOCKED: need credentials", "DECISION: merge order?", "blocked on CI"):
        assert classify_escalation(message) == EscalationClass(
            kind="interrupt", intent="replace"
        )


def test_routine_status_queues_with_continue_and_never_interrupts(tmp_path):
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
    assert result["kind"] == "routine"
    assert result["intent"] == "continue"
    assert len(client.sent) == 1
    assert client.sent[0]["session_id"] == "parent-a"
    assert client.sent[0]["intent"] == "continue", (
        "a routine progress report must never cancel and replace the "
        "recipient's active turn"
    )
    assert store.pending() == []


def test_routine_delivery_records_durable_acknowledgment(tmp_path):
    path = tmp_path / "escalations.sqlite3"
    client = FakeClient(
        sessions={"parent-a": {"id": "parent-a", "workspace_id": "parent-workspace"}},
        workspaces={"parent-workspace": {"id": "parent-workspace", "archived": False}},
    )
    store = EscalationStore(path)

    result = escalate(
        client,
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id="parent-a",
        message="STATUS: lane complete",
        store=store,
    )

    ack = result["acknowledgment"]
    assert ack["kind"] == "routine"
    assert ack["intent"] == "continue"
    assert ack["parent_session_id"] == "parent-a"

    reopened = EscalationStore(path)
    acks = reopened.acknowledgments()
    assert len(acks) == 1
    assert acks[0].ack_id == ack["ack_id"]
    assert acks[0].message == "STATUS: lane complete"


def test_blocked_and_decision_escalations_replace_the_active_turn(tmp_path):
    client = FakeClient(
        sessions={"parent-a": {"id": "parent-a", "workspace_id": "parent-workspace"}},
        workspaces={"parent-workspace": {"id": "parent-workspace", "archived": False}},
    )
    store = EscalationStore(tmp_path / "escalations.sqlite3")

    for message in ("BLOCKED: need a decision on merge base", "DECISION: pick PR order"):
        result = escalate(
            client,
            child_session_id="child-a",
            child_workspace_id="child-workspace",
            parent_session_id="parent-a",
            message=message,
            store=store,
        )
        assert result["delivered"] is True
        assert result["kind"] == "interrupt"
        assert result["intent"] == "replace"

    assert [entry["intent"] for entry in client.sent] == ["replace", "replace"]
    assert store.pending() == []


def test_repeated_identical_routine_delivery_reuses_one_ack(tmp_path):
    client = FakeClient(
        sessions={"parent-a": {"id": "parent-a", "workspace_id": "parent-workspace"}},
        workspaces={"parent-workspace": {"id": "parent-workspace", "archived": False}},
    )
    store = EscalationStore(tmp_path / "escalations.sqlite3")

    kwargs = dict(
        child_session_id="child-a",
        child_workspace_id="child-workspace",
        parent_session_id="parent-a",
        message="STATUS: retry me",
        store=store,
    )
    first = escalate(client, **kwargs)
    second = escalate(client, **kwargs)

    assert first["acknowledgment"]["ack_id"] == second["acknowledgment"]["ack_id"]
    assert len(store.acknowledgments()) == 1


def test_order_expectation_is_durable_and_any_recipient_report_satisfies_it(tmp_path):
    path = tmp_path / "escalations.sqlite3"
    store = EscalationStore(path)
    order = store.expect_order(
        order_id="order-1",
        sender_session_id="manager-a",
        recipient_session_id="worker-a",
        body="Run the focused tests",
    )

    assert order.body_digest
    assert EscalationStore(path).orders()[0].satisfied_at is None
    assert store.satisfy_orders("worker-a") == 1
    assert EscalationStore(path).orders()[0].satisfied_at is not None


def test_order_expectations_redact_obvious_secret_values(tmp_path):
    store = EscalationStore(tmp_path / "escalations.sqlite3")
    order = store.expect_order(
        order_id="order-secret",
        sender_session_id="manager-a",
        recipient_session_id="worker-a",
        body="Use token=super-secret-value for this task",
    )

    assert "super-secret-value" not in order.body
    assert "[REDACTED]" in order.body


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


# ---------------------------------------------------------------- store durability


def test_concurrent_openers_never_fail_with_database_is_locked(tmp_path: Path) -> None:
    """Why: SQLite refuses a journal-mode switch while another connection is
    open and does not run the busy handler for it, so two processes creating
    the store at the same moment used to surface a spurious
    "database is locked" - the failure looked like the caller's fault and lost
    a durable escalation write."""
    path = tmp_path / "escalations.sqlite3"

    def park(index: int):
        store = EscalationStore(path)
        return store.park(
            child_session_id=f"child-{index}",
            child_workspace_id=None,
            recorded_parent_session_id=None,
            reason="no_parent",
            message=f"BLOCKED: {index}",
            dedupe_key=f"key-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        parked = list(workers.map(park, range(24)))

    assert len({record.escalation_id for record in parked}) == 24
    assert len(EscalationStore(path).pending()) == 24


def test_wal_is_adopted_once_and_reread_by_later_openers(tmp_path: Path) -> None:
    """Why: journal mode is a persistent property of the file, so every opener
    after the first must read it back rather than re-take an exclusive lock."""
    path = tmp_path / "escalations.sqlite3"
    EscalationStore(path)

    with sqlite3.connect(path) as probe:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        # A second opener succeeds even while this connection is held open,
        # which an unconditional `PRAGMA journal_mode = WAL` could not do.
        EscalationStore(path)
    probe.close()


def test_every_connection_the_store_opens_is_closed(monkeypatch, tmp_path: Path) -> None:
    """Why: a leaked connection keeps SQLite refusing the journal-mode switch
    for the life of the process, and the store opens one per operation."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    store = EscalationStore(tmp_path / "escalations.sqlite3")
    store.park(
        child_session_id="child-1",
        child_workspace_id=None,
        recorded_parent_session_id=None,
        reason="no_parent",
        message="BLOCKED: work",
        dedupe_key="key-1",
    )
    store.pending()

    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_a_failing_statement_keeps_its_own_error_message(tmp_path: Path) -> None:
    """Why: the connection wrapper must not relabel statement errors as open
    failures, or every caller's specific message would be replaced."""
    store = EscalationStore(tmp_path / "escalations.sqlite3")
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("DROP TABLE escalations")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(EscalationStoreError, match="Cannot read parked escalations"):
        store.pending()
