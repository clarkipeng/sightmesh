"""Focused proof that opt-in policy signals stay durable and single-shot."""

from __future__ import annotations

import argparse

from sightmesh import cli, escalation
from sightmesh.durable import DurableCommand, DurableExecutionReconciler


class SignalClient:
    def __init__(self, process: dict, snapshot: dict | None = None, *, parent=True):
        self.process = process
        self.snapshot = snapshot or {}
        self.parent = parent
        self.sent: list[dict] = []

    def execution_processes(self, _session_id):
        return [self.process]

    def normalized_snapshot(self, _process_id):
        return self.snapshot

    def session(self, session_id):
        if session_id != "parent":
            raise AssertionError(session_id)
        return {"id": "parent", "workspace_id": "parent-workspace"}

    def workspace(self, workspace_id):
        assert workspace_id == "parent-workspace"
        return {"id": workspace_id, "archived": False}

    def send(self, session_id, prompt, sender_session=None, *, dedupe_key=None, intent="continue"):
        self.sent.append({"session_id": session_id, "prompt": prompt, "key": dedupe_key, "intent": intent})
        return {"ok": True}


class NoParentSignalClient(SignalClient):
    def session(self, session_id):
        raise AssertionError("a policy with no parent must park without delivery")


def _session(*, parent=True):
    row = {"id": "child", "workspace_id": "child-workspace"}
    if parent:
        row["parent_session_id"] = "parent"
    return row


class AssignmentQueue:
    def commands(self, _session_id):
        return [DurableCommand("assignment", "child", "work", "pending")]


def test_policy_crud_is_available_to_self_and_a_live_peer(monkeypatch, capsys):
    """Both selectors resolve live sessions, so workers and managers share one policy surface."""
    class PolicyClient:
        def workspaces(self):
            return [{"id": "self-w", "name": "self", "archived": False}, {"id": "peer-w", "name": "peer", "archived": False}]

        def workspace_summaries(self, archived=False):
            return []

        def sessions(self, workspace_id):
            return [{"id": "self-id" if workspace_id == "self-w" else "peer-id"}]

    monkeypatch.setattr(cli, "CdesktopClient", lambda _url: PolicyClient())
    self_args = argparse.Namespace(policy_action="set", session_id="@self", signal_on="terminal,idle:30", url=None, json=True)
    peer_args = argparse.Namespace(policy_action="set", session_id="@peer", signal_on="context-pressure:0.7", url=None, json=True)
    assert cli.cmd_policy(self_args) == cli.cmd_policy(peer_args) == 0
    store = escalation.EscalationStore()
    assert store.signal_policy("self-id").conditions == ("idle:30", "terminal")
    assert store.signal_policy("peer-id").conditions == ("context-pressure:0.7",)
    assert cli.cmd_policy(argparse.Namespace(policy_action="clear", session_id="@self", url=None, json=True)) == 0
    assert store.signal_policy("self-id").conditions == ()
    capsys.readouterr()


def test_each_condition_fires_once_across_sweeps_and_restart(tmp_path):
    """The durable acknowledgment fences terminal, pressure, and idle signals after restart."""
    store = escalation.EscalationStore(tmp_path / "signals.sqlite3")
    store.set_signal_policy("child", ("terminal", "context-pressure:0.7", "idle:10"))
    client = SignalClient(
        {"id": "process", "status": "completed", "updated_at": 0},
        {"entries": [{"content": {"entry_type": {"type": "token_usage_info", "total_tokens": 80, "model_context_window": 100}}}]},
    )
    first = DurableExecutionReconciler(client, AssignmentQueue(), signal_store=store, clock=lambda: 20)
    first.reconcile_session(_session())
    first.reconcile_session(_session())
    DurableExecutionReconciler(client, AssignmentQueue(), signal_store=escalation.EscalationStore(store.path), clock=lambda: 20).reconcile_session(_session())
    assert [item["key"] for item in client.sent] == [
        "signal-policy:child:context-pressure:0.7",
        "signal-policy:child:idle:10",
        "signal-policy:child:terminal",
    ]
    assert all(item["intent"] == "continue" for item in client.sent)


def test_no_parent_policy_signal_parks_once(tmp_path):
    """A signal without a live parent reuses the decision inbox and its durable dedupe fence."""
    store = escalation.EscalationStore(tmp_path / "signals.sqlite3")
    store.set_signal_policy("child", ("terminal",))
    client = NoParentSignalClient({"id": "process", "status": "completed"})
    reconciler = DurableExecutionReconciler(client, signal_store=store)
    reconciler.reconcile_session(_session(parent=False))
    DurableExecutionReconciler(client, signal_store=escalation.EscalationStore(store.path)).reconcile_session(_session(parent=False))
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0].dedupe_key == "signal-policy:child:terminal"
    assert client.sent == []


def test_empty_policy_is_a_no_op(tmp_path):
    """Absent policy rows preserve today's reconciler behavior without new parent messages."""
    client = SignalClient({"id": "process", "status": "completed"})
    DurableExecutionReconciler(client, signal_store=escalation.EscalationStore(tmp_path / "signals.sqlite3")).reconcile_session(_session())
    assert client.sent == []
