from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sightmesh import execution_routing, succession
from sightmesh.cdesktop import CdesktopError
from sightmesh.durable import DurableExecutionReconciler
from sightmesh.escalation import EscalationStore
from sightmesh.execution_routing import (
    ExecutionRoutingSettings,
    ExecutionRoutingStore,
    Route,
    select_route,
)
from sightmesh.pool import core as pool_core
from sightmesh.succession import (
    OwnershipStore,
    QuarantinedSessionError,
    SuccessionError,
    escalate_free_route_failure,
    reroute_after_quota_exhaustion,
    resolve_live_successor,
    transfer_ownership,
)

from fixtures import free_route_failures

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


def test_concurrent_retirement_keeps_the_first_terminal_record(tmp_path) -> None:
    """Why: failover workers may retire the same session at once, but quarantine
    must preserve one irreversible history rather than whichever JSON rewrite won."""
    path = tmp_path / "ownership.json"

    def retire(state: str, reason: str):
        return OwnershipStore(path).retire("session-1", state=state, reason=reason)

    with ThreadPoolExecutor(max_workers=2) as workers:
        records = list(
            workers.map(
                lambda args: retire(*args),
                (("retired", "operator"), ("superseded", "failover")),
            )
        )

    assert records[0] == records[1]
    assert OwnershipStore(path).get("session-1") == records[0]


def test_legacy_ownership_json_migrates_once_without_changing_quarantine(tmp_path) -> None:
    """Why: upgrade must preserve the delivery fence, even if opened again after
    the JSON file has been retired, so a formerly quarantined session stays shut."""
    path = tmp_path / "ownership.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "session-1": {
                        "session_id": "session-1",
                        "state": "superseded",
                        "reason": "failover",
                        "retired_at": "2026-01-01T00:00:00+00:00",
                        "logical_key": "handoff:session-1",
                        "successor_session_id": "session-2",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    migrated = OwnershipStore(path)
    record = migrated.get("session-1")
    assert record is not None and record.successor_session_id == "session-2"
    assert not path.exists()
    assert path.with_name("ownership.json.migrated").exists()
    with pytest.raises(QuarantinedSessionError):
        migrated.assert_deliverable("session-1")

    # A later opener sees SQLite only; it cannot import a second copy.
    migrated.link_successor("session-1", "session-2")
    reopened = OwnershipStore(path)
    assert reopened.get("session-1") == record
    with pytest.raises(QuarantinedSessionError):
        reopened.assert_deliverable("session-1")


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


def test_child_terminal_notification_never_resolves_back_to_child(tmp_path) -> None:
    ownership = store(tmp_path)
    ownership.retire("parent-1", state="superseded", reason="bad handoff")
    ownership.link_successor("parent-1", "child-1")
    signal_store = EscalationStore()
    client = FakeCdesktop()
    reconciler = DurableExecutionReconciler(
        client, ownership=ownership, signal_store=signal_store
    )

    reconciler.reconcile_child_terminal(
        {"id": "child-1", "parent_session_id": "parent-1"},
        status="completed",
    )

    assert client.sent == []
    parked = signal_store.pending()
    assert len(parked) == 1
    assert parked[0].recorded_parent_session_id == "parent-1"
    assert parked[0].dedupe_key == "child-terminal:child-1:completed"


def test_undeliverable_parent_wake_is_parked_then_resolved_on_delivery(
    tmp_path,
) -> None:
    """Why: a quarantined parent with no successor used to swallow the child's
    only terminal signal into a log line. The inbox has to carry it instead,
    and stop carrying it once a successor finally takes delivery."""
    ownership = store(tmp_path)
    ownership.retire("parent-1", state="superseded", reason="failover")
    signal_store = EscalationStore()
    client = FakeCdesktop()
    reconciler = DurableExecutionReconciler(
        client, ownership=ownership, signal_store=signal_store
    )
    child = {"id": "child-1", "parent_session_id": "parent-1"}

    reconciler.reconcile_child_terminal(child, status="interrupted")

    assert client.sent == []  # never into the retired parent
    parked = signal_store.pending()
    assert len(parked) == 1
    assert parked[0].dedupe_key == "child-terminal:child-1:interrupted"
    assert parked[0].recorded_parent_session_id == "parent-1"
    assert "CHILD_TERMINAL: child-1 interrupted" in parked[0].message

    # Repeated sweeps keep it to one record rather than flooding the inbox.
    reconciler.reconcile_child_terminal(child, status="interrupted")
    assert len(signal_store.pending()) == 1

    ownership.link_successor("parent-1", "parent-2")
    reconciler.reconcile_child_terminal(child, status="interrupted")

    assert client.sent[0][0] == "parent-2"
    # Delivered after all, so the stand-in no longer sits in the inbox.
    assert signal_store.pending() == []


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


# ---------------------------------------------------------------- free route failure


class FakeParentClient:
    """cdesktop surface `escalate` needs: resolve a parent, then enqueue to it."""

    def __init__(self, sessions=None, workspaces=None):
        self.sessions_by_id = sessions or {}
        self.workspaces_by_id = workspaces or {}
        self.sent: list[dict] = []

    def session(self, session_id):
        if session_id not in self.sessions_by_id:
            raise CdesktopError(f"no such session {session_id}")
        return self.sessions_by_id[session_id]

    def workspace(self, workspace_id):
        return self.workspaces_by_id[workspace_id]

    def send(self, session_id, prompt, sender_session=None, *, dedupe_key=None, intent="continue"):
        self.sent.append(
            {"session_id": session_id, "prompt": prompt, "dedupe_key": dedupe_key, "intent": intent}
        )
        return {"ok": True}


def _live_parent_client() -> FakeParentClient:
    return FakeParentClient(
        sessions={"parent-1": {"id": "parent-1", "workspace_id": "ws-1"}},
        workspaces={"ws-1": {"id": "ws-1", "archived": False}},
    )


FREE_ROUTE = Route(
    id="opencode-ox-free",
    executor="OPENCODE",
    model="opencode/x-preview-f-free",
    billing_class="free",
)
PAID_ROUTE = Route(
    id="codex-max",
    executor="CODEX",
    model="gpt-5-codex",
    billing_class="subscription",
    account_pool="codex",
)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (free_route_failures.MODEL_NOT_FOUND, "model_unavailable"),
        (free_route_failures.UNKNOWN_PROVIDER, "model_unavailable"),
        (free_route_failures.SERVER_ERROR, "provider_rejected"),
        ("", "unknown"),
    ],
)
def test_free_failure_outcome_is_classified_from_captured_executor_output(
    output, expected
) -> None:
    """Why: the outcome class an operator reads has to come from the text the
    free tier really prints, not from a shape invented to match the parser."""
    assert execution_routing.classify_free_failure(output) == expected
    assert expected in execution_routing.FREE_FAILURE_OUTCOMES


def test_free_route_failure_escalates_to_a_live_parent_and_never_degrades(
    pool_root: Path,
) -> None:
    """Why: a free route owns no account, so nothing cools and nothing reroutes.
    Without an escalation the worker is simply blocked with no signal at all."""
    client = _live_parent_client()
    settings = ExecutionRoutingSettings(routes=(FREE_ROUTE, PAID_ROUTE))
    assert settings.fallback_on_free_failure is False

    failure = escalate_free_route_failure(
        client,
        settings,
        route_id=FREE_ROUTE.id,
        child_session_id="child-1",
        child_workspace_id="ws-child",
        parent_session_id="parent-1",
        output=free_route_failures.MODEL_NOT_FOUND,
    )

    assert failure.outcome == "model_unavailable"
    assert failure.escalation["delivered"] is True
    # No opt-in, so no selection was made at all: the paid route is untouched.
    assert failure.selection is None

    delivered = client.sent[0]
    assert delivered["session_id"] == "parent-1"
    # A blocked worker needs a decision, so this replaces the parent's turn.
    assert delivered["intent"] == "replace"
    assert FREE_ROUTE.id in delivered["prompt"]
    assert "model_unavailable" in delivered["prompt"]
    assert "Model not found" in delivered["prompt"]
    assert PAID_ROUTE.id not in delivered["prompt"]


def test_free_route_failure_parks_in_the_decision_inbox_without_a_parent(
    pool_root: Path,
) -> None:
    """Why: an externally launched worker has no parent to wake. Dropping the
    failure there is exactly the silent block this path exists to remove."""
    store_ = EscalationStore()
    failure = escalate_free_route_failure(
        FakeParentClient(),
        ExecutionRoutingSettings(routes=(FREE_ROUTE,)),
        route_id=FREE_ROUTE.id,
        child_session_id="child-1",
        parent_session_id=None,
        output=free_route_failures.SERVER_ERROR,
        store=store_,
    )

    assert failure.escalation["delivered"] is False
    assert failure.escalation["reason"] == "no_parent"
    parked = store_.pending()
    assert len(parked) == 1
    assert "provider_rejected" in parked[0].message
    assert parked[0].dedupe_key == (
        "free-route-failure:child-1:opencode-ox-free:provider_rejected"
    )


def test_repeated_free_route_failures_collapse_to_one_record(pool_root: Path) -> None:
    """Why: a retrying launcher must not turn one broken route into an inbox flood."""
    store_ = EscalationStore()
    settings = ExecutionRoutingSettings(routes=(FREE_ROUTE,))
    for _ in range(3):
        escalate_free_route_failure(
            FakeParentClient(),
            settings,
            route_id=FREE_ROUTE.id,
            child_session_id="child-1",
            parent_session_id=None,
            output=free_route_failures.MODEL_NOT_FOUND,
            store=store_,
        )

    assert len(store_.pending()) == 1


def test_free_route_failure_degrades_to_a_paid_route_only_when_opted_in(
    pool_root: Path, monkeypatch
) -> None:
    """Why: falling back onto an owned account spends real quota, so it may only
    happen on an explicit policy - and the escalation must name where it went."""
    pool_core.save_pool(
        {
            "accounts": [
                {
                    "id": "codex-sub1",
                    "provider": "codex",
                    "kind": "chatgpt",
                    "codex_home": "/tmp/codex-sub1",
                }
            ]
        }
    )
    monkeypatch.setattr(
        pool_core, "quota", lambda _account: {"known": False, "reason": "no source"}
    )
    client = _live_parent_client()
    settings = ExecutionRoutingSettings(
        routes=(FREE_ROUTE, PAID_ROUTE), fallback_on_free_failure=True
    )

    failure = escalate_free_route_failure(
        client,
        settings,
        route_id=FREE_ROUTE.id,
        child_session_id="child-1",
        parent_session_id="parent-1",
        output=free_route_failures.MODEL_NOT_FOUND,
    )

    assert failure.selection is not None
    assert failure.selection.status == "resolved"
    assert failure.selection.target is not None
    # The failed free route is excluded by id; every free route shares the
    # binding sentinel, so account exclusion could not have named just this one.
    assert failure.selection.target.route_id == PAID_ROUTE.id
    assert failure.selection.target.billing_class == "subscription"
    assert PAID_ROUTE.id in client.sent[0]["prompt"]


def test_opted_in_fallback_still_reports_when_no_route_is_left(
    pool_root: Path,
) -> None:
    """Why: opting in must not reintroduce the silent block when the fallback
    itself finds nothing - the operator still has to hear about it."""
    client = _live_parent_client()
    settings = ExecutionRoutingSettings(
        routes=(FREE_ROUTE,), fallback_on_free_failure=True
    )

    failure = escalate_free_route_failure(
        client,
        settings,
        route_id=FREE_ROUTE.id,
        child_session_id="child-1",
        parent_session_id="parent-1",
        output=free_route_failures.MODEL_NOT_FOUND,
    )

    assert failure.selection is not None
    assert failure.selection.status == "blocked"
    assert failure.selection.reason == "routes_exhausted"
    assert "routes_exhausted" in client.sent[0]["prompt"]


def test_free_route_failure_never_reads_pool_state_unless_opted_in(
    monkeypatch, pool_root: Path
) -> None:
    """Why: the free path must stay clear of accounts and credentials entirely
    while the default policy is in force."""
    for name in ("load_pool", "load_state"):
        monkeypatch.setattr(
            pool_core, name, lambda name=name: pytest.fail(f"free failure read {name}")
        )

    failure = escalate_free_route_failure(
        _live_parent_client(),
        ExecutionRoutingSettings(routes=(FREE_ROUTE, PAID_ROUTE)),
        route_id=FREE_ROUTE.id,
        child_session_id="child-1",
        parent_session_id="parent-1",
        output=free_route_failures.MODEL_NOT_FOUND,
    )
    assert failure.selection is None


def test_free_failure_detail_is_bounded_and_redacted(pool_root: Path) -> None:
    """Why: provider output is untrusted text that lands in a durable record."""
    store_ = EscalationStore()
    failure = escalate_free_route_failure(
        FakeParentClient(),
        ExecutionRoutingSettings(routes=(FREE_ROUTE,)),
        route_id=FREE_ROUTE.id,
        child_session_id="child-1",
        parent_session_id=None,
        output="Error: refused, api_key=sk-live-secret-value " + "x" * 2000,
        store=store_,
    )

    assert "sk-live-secret-value" not in failure.detail
    assert "[REDACTED]" in failure.detail
    assert len(failure.detail) <= succession.FREE_FAILURE_DETAIL_LIMIT
    assert "sk-live-secret-value" not in store_.pending()[0].message


def test_fallback_on_free_failure_defaults_off_and_round_trips(tmp_path: Path) -> None:
    """Why: the opt-in is a billing decision, so it must survive a restart
    exactly as written and must never be enabled by an older settings file."""
    path = tmp_path / "execution_routing.json"
    store_ = ExecutionRoutingStore(path)

    # A settings file written before this field existed stays opted out.
    path.write_text(
        json.dumps({"version": 1, "executionRouting": {"routes": []}}), encoding="utf-8"
    )
    assert store_.load().fallback_on_free_failure is False

    saved = store_.save(
        ExecutionRoutingSettings(routes=(FREE_ROUTE,), fallback_on_free_failure=True)
    )
    assert saved.fallback_on_free_failure is True
    assert store_.load() == saved
    assert json.loads(path.read_text(encoding="utf-8"))["executionRouting"][
        "fallbackOnFreeFailure"
    ] is True
