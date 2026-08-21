from __future__ import annotations

import time
from pathlib import Path

import pytest

from sightmesh import execution_routing, succession
from sightmesh.durable import DurableExecutionReconciler
from sightmesh.execution_routing import ExecutionRoutingSettings, Route, select_route
from sightmesh.pool import core as pool_core
from sightmesh.succession import (
    OwnershipStore,
    QuarantinedSessionError,
    SuccessionError,
    reroute_after_quota_exhaustion,
    resolve_live_successor,
    transfer_ownership,
)


class FakeCdesktop:
    """Stateful stand-in for cdesktop's durable command machinery.

    Models the frozen Lane A1 contract surface the reconciler consumes:
    command rows with states, idempotent enqueue on (session_id, dedupe_key),
    interrupt/requeue lifecycle transitions, and queue dispatch.
    """

    def __init__(self, commands=None, processes=None):
        self.commands = commands or []
        self.processes = processes or []
        self.sent: list[tuple[str, str, str | None, str]] = []
        self.enqueued: dict[tuple[str, str], str] = {}
        self.dispatches: list[str] = []
        self.interrupts: list[str] = []
        self.requeues: list[tuple[str, str | None]] = []

    def _find(self, command_id):
        return next(row for row in self.commands if row["id"] == command_id)

    def session_commands(self, session_id):
        return [dict(row) for row in self.commands if row["session_id"] == session_id]

    def execution_processes(self, session_id):
        return [
            dict(row) for row in self.processes if row["session_id"] == session_id
        ]

    def normalized_snapshot(self, _process_id):
        return {"stream_alive": True, "entries": []}

    def probe_connectivity(self):
        return True

    def dispatch_queued(self, session_id):
        self.dispatches.append(session_id)

    def interrupt_command(self, command_id):
        self.interrupts.append(command_id)
        self._find(command_id)["state"] = "cancelled"

    def requeue_command(self, command_id, *, dedupe_key=None):
        self.requeues.append((command_id, dedupe_key))
        row = self._find(command_id)
        row["state"] = "pending"
        row["execution_process_id"] = None

    def requeue_execution_commands(self, session_id, execution_process_id):
        # The real endpoint: one dead execution's command rows return to the
        # queue in a single call.
        for row in self.commands:
            if (
                row["session_id"] == session_id
                and row.get("execution_process_id") == execution_process_id
                and row["state"] == "claimed"
            ):
                self.requeues.append((row["id"], row.get("dedupe_key")))
                row["state"] = "pending"
                row["execution_process_id"] = None

    def complete_command(self, command_id):
        self._find(command_id)["state"] = "done"

    def send(self, session_id, prompt, sender=None, *, dedupe_key=None, intent="continue"):
        self.sent.append((session_id, prompt, dedupe_key, intent))
        if dedupe_key:
            # ON CONFLICT(session_id, dedupe_key) DO NOTHING
            self.enqueued.setdefault((session_id, dedupe_key), prompt)


def store(tmp_path: Path) -> OwnershipStore:
    return OwnershipStore(tmp_path / "ownership.json")


# ---------------------------------------------------------------- ownership


def test_terminal_ownership_is_atomic_durable_and_first_write_wins(tmp_path) -> None:
    first = store(tmp_path)
    record = first.retire(
        "session-1", state="superseded", reason="failover", logical_key="logical-1"
    )
    assert record.logical_key == "logical-1"

    # A later, conflicting transition can never rewrite recorded history.
    again = first.retire("session-1", reason="operator retire")
    assert again.state == "superseded"
    assert again.reason == "failover"

    # A fresh store over the same path (a restart) still sees the quarantine.
    restarted = store(tmp_path)
    assert restarted.is_quarantined("session-1")
    with pytest.raises(QuarantinedSessionError):
        restarted.assert_deliverable("session-1")
    assert restarted.assert_deliverable("session-2") is None


def test_successor_linkage_is_single_and_conflict_rejected(tmp_path) -> None:
    ownership = store(tmp_path)
    ownership.retire("session-1", state="superseded", reason="failover")
    ownership.link_successor("session-1", "successor-1")
    ownership.link_successor("session-1", "successor-1")
    with pytest.raises(SuccessionError):
        ownership.link_successor("session-1", "successor-2")
    with pytest.raises(SuccessionError):
        ownership.link_successor("session-9", "successor-1")

    assert resolve_live_successor(ownership, "session-1") == "successor-1"
    # A live session resolves to itself; a chain follows to the live end.
    assert resolve_live_successor(ownership, "successor-1") == "successor-1"
    ownership.retire("successor-1", state="superseded", reason="failover")
    assert resolve_live_successor(ownership, "session-1") is None
    ownership.link_successor("successor-1", "successor-2")
    assert resolve_live_successor(ownership, "session-1") == "successor-2"


# ---------------------------------------------------------------- quarantine


def _claimed_command(session_id="session-1"):
    return {
        "id": "command-1",
        "session_id": session_id,
        "body": "finish the migration",
        "state": "claimed",
        "dedupe_key": "logical-1",
        "execution_process_id": "process-1",
    }


def test_retired_session_cannot_resume(tmp_path) -> None:
    ownership = store(tmp_path)
    ownership.retire("session-1", state="superseded", reason="failover")
    client = FakeCdesktop(
        commands=[_claimed_command()],
        processes=[{"id": "process-1", "session_id": "session-1", "status": "killed"}],
    )
    reconciler = DurableExecutionReconciler(client, ownership=ownership)

    reconciler.reconcile_session({"id": "session-1"})
    reconciler.reconcile_session({"id": "session-1"})

    # Cancelled exactly once; never requeued, never dispatched back to life.
    assert client.interrupts == ["command-1"]
    assert client.requeues == []
    assert client.dispatches == []
    assert client.commands[0]["state"] == "cancelled"


def test_completed_turn_manager_still_resumes(tmp_path) -> None:
    ownership = store(tmp_path)
    client = FakeCdesktop(
        commands=[
            {
                "id": "command-2",
                "session_id": "session-1",
                "body": "callback: review PR",
                "state": "pending",
                "dedupe_key": "callback-1",
                "execution_process_id": None,
            }
        ],
        processes=[
            {"id": "process-1", "session_id": "session-1", "status": "completed"}
        ],
    )
    reconciler = DurableExecutionReconciler(client, ownership=ownership)

    reconciler.reconcile_session({"id": "session-1"})

    # An ordinary completed turn is not retirement: nothing is cancelled and
    # the queued callback still dispatches, so the manager wakes up normally.
    assert ownership.assert_deliverable("session-1") is None
    assert client.interrupts == []
    assert client.dispatches == ["session-1"]


def test_child_terminal_notification_follows_successor_and_never_retired(
    tmp_path,
) -> None:
    ownership = store(tmp_path)
    ownership.retire("parent-1", state="superseded", reason="failover")
    client = FakeCdesktop()
    reconciler = DurableExecutionReconciler(client, ownership=ownership)
    child = {"id": "child-1", "parent_session_id": "parent-1"}

    reconciler.reconcile_child_terminal(child, status="interrupted")
    assert client.sent == []  # no successor yet: parked, never into the retired parent

    ownership.link_successor("parent-1", "parent-2")
    reconciler.reconcile_child_terminal(child, status="interrupted")
    session_id, _body, key, intent = client.sent[0]
    assert session_id == "parent-2"
    assert key == "child-terminal:child-1:interrupted"
    assert intent == "continue"


def test_terminal_command_wake_never_targets_retired_parent_and_delivers_once(
    tmp_path,
) -> None:
    """Why: terminal-command wakes use the same successor quarantine as child terminals."""
    ownership = store(tmp_path)
    ownership.retire("parent-1", state="superseded", reason="failover")
    client = FakeCdesktop(
        commands=[
            {
                "id": "command-1",
                "session_id": "child-1",
                "body": "done work",
                "state": "done",
            }
        ]
    )
    reconciler = DurableExecutionReconciler(client, ownership=ownership)
    child = {"id": "child-1", "parent_session_id": "parent-1"}

    reconciler.reconcile_session(child)
    assert client.sent == []  # parked until the retired parent's successor exists

    ownership.link_successor("parent-1", "parent-2")
    reconciler.reconcile_session(child)
    reconciler.reconcile_session(child)

    assert client.sent == [
        (
            "parent-2",
            "CHILD_DELIVERY: child-1 command-1 done",
            "child-command:command-1:done",
            "continue",
        )
    ]


# ---------------------------------------------------------------- restart


def test_durable_requeue_survives_restart(tmp_path) -> None:
    ownership = store(tmp_path)
    client = FakeCdesktop(
        commands=[_claimed_command()],
        processes=[{"id": "process-1", "session_id": "session-1", "status": "killed"}],
    )

    DurableExecutionReconciler(client, ownership=ownership).reconcile_session(
        {"id": "session-1"}
    )
    assert client.requeues == [("command-1", "logical-1")]
    assert client.commands[0]["state"] == "pending"

    # Restart: a fresh reconciler over the same durable rows must not
    # manufacture a second command, and the surviving one still dispatches.
    restarted = DurableExecutionReconciler(client, ownership=store(tmp_path))
    restarted.reconcile_session({"id": "session-1"})

    assert client.requeues == [("command-1", "logical-1")]
    assert client.commands[0]["dedupe_key"] == "logical-1"
    assert client.dispatches.count("session-1") == 2


# ---------------------------------------------------------------- handoff


def test_cross_executor_handoff_preserves_one_logical_command(tmp_path) -> None:
    ownership = store(tmp_path)
    client = FakeCdesktop(
        commands=[
            {
                "id": "command-1",
                "session_id": "source-claude",
                "body": "finish the migration",
                "state": "pending",
                "dedupe_key": "logical-1",
                "execution_process_id": None,
            }
        ]
    )
    spawns: list[str] = []

    def spawn() -> str:
        spawns.append("successor-codex")
        return "successor-codex"

    first = transfer_ownership(
        client,
        ownership,
        source_session_id="source-claude",
        spawn=spawn,
        reason="failover:codex",
    )
    second = transfer_ownership(
        client,
        ownership,
        source_session_id="source-claude",
        spawn=spawn,
        reason="failover:codex",
    )

    assert spawns == ["successor-codex"]
    assert first.spawned and not second.spawned
    assert second.successor_session_id == "successor-codex"
    assert first.forwarded_commands == 1 and first.cancelled_commands == 1

    # Exactly one logical command survives on the successor; the source copy
    # is cancelled and the source itself is quarantined.
    successor_rows = [
        key for key in client.enqueued if key[0] == "successor-codex"
    ]
    assert successor_rows == [("successor-codex", "logical-1")]
    assert client.commands[0]["state"] == "cancelled"
    with pytest.raises(QuarantinedSessionError):
        ownership.assert_deliverable("source-claude")


def test_handoff_rerun_after_crash_still_forwards_exactly_once(tmp_path) -> None:
    ownership = store(tmp_path)
    client = FakeCdesktop(
        commands=[
            {
                "id": "command-1",
                "session_id": "source-claude",
                "body": "finish the migration",
                "state": "pending",
                "dedupe_key": "logical-1",
                "execution_process_id": None,
            }
        ]
    )
    # Crash window: terminal record and successor linkage were persisted, but
    # forwarding and cancellation never ran.
    ownership.retire(
        "source-claude",
        state="superseded",
        reason="failover:codex",
        logical_key="handoff:source-claude",
    )
    ownership.link_successor("source-claude", "successor-codex")

    result = transfer_ownership(
        client,
        ownership,
        source_session_id="source-claude",
        spawn=lambda: pytest.fail("must not spawn a second successor"),
        reason="failover:codex",
    )

    assert not result.spawned
    assert result.forwarded_commands == 1
    assert list(client.enqueued) == [("successor-codex", "logical-1")]
    assert client.commands[0]["state"] == "cancelled"


def test_handoff_rejects_a_quarantined_successor(tmp_path) -> None:
    ownership = store(tmp_path)
    ownership.retire("dead-successor", reason="operator retire")
    client = FakeCdesktop()
    with pytest.raises(QuarantinedSessionError):
        transfer_ownership(
            client,
            ownership,
            source_session_id="source-claude",
            spawn=lambda: "dead-successor",
            reason="failover",
        )


# ---------------------------------------------------------------- quota


@pytest.fixture
def pool_root(monkeypatch, tmp_path: Path) -> Path:
    """Keep every test off the operator's real ~/.config/agent-pool."""
    root = tmp_path / "agent-pool"
    monkeypatch.setattr(pool_core, "default_pool_root", lambda: root)
    return root


def test_quota_exhaustion_cools_binding_and_selects_next_route(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                {"id": "max-a", "provider": "claude", "kind": "oauth"},
                {
                    "id": "codex-b",
                    "provider": "codex",
                    "kind": "chatgpt",
                    "codex_home": "/tmp/codex-b",
                },
            ]
        }
    )
    pool_core.write_token("max-a", "sk-ant-oat01-" + "a" * 120)
    monkeypatch.setattr(
        pool_core, "quota", lambda _account: {"known": False, "reason": "no source"}
    )
    settings = ExecutionRoutingSettings(
        routes=(
            Route(
                id="claude-max",
                executor="CLAUDE_CODE",
                model="opus",
                billing_class="subscription",
                account_pool="claude",
            ),
            Route(
                id="codex-pool",
                executor="CODEX",
                model="gpt-5-codex",
                billing_class="subscription",
                account_pool="codex",
            ),
        )
    )

    before = select_route(settings)
    assert before.status == "resolved"
    assert before.target.auth_binding_id == "max-a"

    result = reroute_after_quota_exhaustion(
        settings, exhausted_binding_id="max-a", cooldown_seconds=3600
    )

    # The cooldown is durable pool state, observed by every later selection.
    assert pool_core.cooling_until(pool_core.load_state(), "max-a") > time.time()
    assert result.status == "resolved"
    assert result.target.route_id == "codex-pool"
    assert result.target.executor == "CODEX"
    assert result.target.auth_binding_id == "codex-b"

    # Even a selection after restart (no exclusions carried over) skips the
    # cooled binding because the state file is the single source of truth.
    after_restart = select_route(settings)
    assert after_restart.target.auth_binding_id == "codex-b"


# ---------------------------------------------------------------- delivery guards


def test_steer_and_message_reject_quarantined_sessions(monkeypatch, tmp_path) -> None:
    from sightmesh import cli

    path = tmp_path / "ownership.json"
    monkeypatch.setattr(succession, "default_ownership_path", lambda: path)
    OwnershipStore().retire("session-1", state="superseded", reason="failover")

    target = {
        "session_id": "session-1",
        "workspace_id": "workspace-a",
        "selector": "worker/agent",
    }
    with pytest.raises(QuarantinedSessionError):
        cli._steer_target(
            FakeCdesktop(), target, "resume please", caller_session=None
        )

    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: FakeCdesktop())
    monkeypatch.setattr(cli, "_resolve_session", lambda _client, _selector: target)
    import argparse

    args = argparse.Namespace(
        session_id="session-1",
        message="resume please",
        message_file=None,
        sender_session=None,
        url=None,
        json=True,
    )
    with pytest.raises(QuarantinedSessionError):
        cli.cmd_message(args)
    with pytest.raises(QuarantinedSessionError):
        cli.cmd_prompt_idle(args)


def test_routed_selection_reroutes_spawn_after_quota_exhaustion(
    pool_root: Path, monkeypatch, tmp_path
) -> None:
    """End to end: routing settings drive spawn, and a cooled binding moves
    the next spawn to the next route without any credential leaving the pool."""
    from sightmesh import cli

    pool_core.save_pool(
        {
            "accounts": [
                {"id": "max-a", "provider": "claude", "kind": "oauth"},
                {
                    "id": "codex-b",
                    "provider": "codex",
                    "kind": "chatgpt",
                    "codex_home": "/tmp/codex-b",
                },
            ]
        }
    )
    pool_core.write_token("max-a", "sk-ant-oat01-" + "a" * 120)
    monkeypatch.setattr(
        pool_core, "quota", lambda _account: {"known": False, "reason": "no source"}
    )
    settings_path = tmp_path / "execution_routing.json"
    monkeypatch.setattr(
        execution_routing, "default_settings_path", lambda: settings_path
    )
    settings = execution_routing.ExecutionRoutingStore().save(
        ExecutionRoutingSettings(
            routes=(
                Route(
                    id="claude-max",
                    executor="CLAUDE_CODE",
                    model="opus",
                    billing_class="subscription",
                    account_pool="claude",
                ),
                Route(
                    id="codex-pool",
                    executor="CODEX",
                    model="gpt-5-codex",
                    billing_class="subscription",
                    account_pool="codex",
                ),
            )
        )
    )
    import argparse

    args = argparse.Namespace(model=None, reasoning=None, provider=None)
    first = cli._profile_selection(args, client=None)
    assert (first.executor, first.route_id, first.auth_binding_id) == (
        "CLAUDE_CODE",
        "claude-max",
        "max-a",
    )

    reroute_after_quota_exhaustion(settings, exhausted_binding_id="max-a")

    second = cli._profile_selection(args, client=None)
    assert (second.executor, second.route_id, second.auth_binding_id) == (
        "CODEX",
        "codex-pool",
        "codex-b",
    )
