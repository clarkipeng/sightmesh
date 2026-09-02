"""Task-local read model, attention queue, and the redaction boundary.

Every test here pins an invariant the shipped code got wrong at least once:
task surfaces that quietly fanned out to the executor, an attention queue
that silently omitted the facts it could not reach, and lease commands that
echoed a live capability token into a transcript.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sightmesh import cli, fleet, observability
from sightmesh.escalation import EscalationStore
from sightmesh.leases import LeaseStore
from sightmesh.task_store import TaskStore, TaskStoreError

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _args(*command: str) -> argparse.Namespace:
    """Build args through the real parser, so a command and its flags cannot
    drift apart in a test that hand-rolls a Namespace."""
    return cli.parser().parse_args(["--json", *command])


class ExplodingClient:
    """Any construction is a failure: a task surface must not hold a client."""

    def __init__(self, _url=None) -> None:
        raise AssertionError("a task-local surface must never reach cdesktop")


def seed(store: TaskStore, specs: list[dict], *, scope: str = "operator") -> None:
    store.reserve_all(scope=scope, parent_task_id=None, specs=specs, max_attempts=3)


def spec(key: str, **kwargs: object) -> dict:
    return {"key": key, "repo": "project", "base": "main", "children": 0, **kwargs}


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "state.sqlite3")


@pytest.fixture
def task_cli(monkeypatch, store: TaskStore, tmp_path: Path):
    """Wire the CLI to an isolated kernel store and an exploding client."""
    monkeypatch.setattr(observability, "task_store", lambda path=None: store)
    monkeypatch.setattr(cli, "CdesktopClient", ExplodingClient)
    monkeypatch.setattr(cli.service, "status", lambda _port: {"running": True})
    monkeypatch.setattr(cli.updates, "read_state", lambda: {"status": "idle"})
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(cli.ProfileStore, "list", lambda _self: [])
    return store


def test_read_tasks_answers_from_the_kernel_store_across_scopes(
    store: TaskStore,
) -> None:
    """`list` is an operator surface: it must see every scope, not just the
    caller's, or a manager cannot see the work it delegated.
    """
    seed(store, [spec("alpha")], scope="operator")
    seed(store, [spec("beta")], scope="session:s1")

    assert {view.key for view in observability.read_tasks(store)} == {"alpha", "beta"}
    scoped = observability.read_tasks(store, scope="session:s1")
    assert [view.key for view in scoped] == ["beta"]


def test_breaker_headroom_is_derived_not_stored(store: TaskStore) -> None:
    """A tripped breaker is `attempts >= max_attempts`, the same arithmetic
    the store enforces, so status and the store can never disagree.
    """
    reservations = store.reserve_all(
        scope="operator", parent_task_id=None, specs=[spec("burnt")], max_attempts=1
    )
    record, _inserted = reservations[0]
    store.activate(record.task_id, workspace_id="workspace-1", session_id="session-1")

    view = observability.read_tasks(store)[0]
    assert view.breaker_tripped is True
    assert observability.task_counts([view])["breaker_tripped"] == 1


def test_an_exhausted_task_is_never_told_to_replace_itself(store: TaskStore) -> None:
    """The shipped queue classified by state first, so a lost or blocked task
    that had already spent its budget was advised to "replace the task" -
    and `TaskStore.prepare_replacement` refuses exactly that. The only
    action the operator was offered could not be carried out.
    """
    reservations = store.reserve_all(
        scope="operator", parent_task_id=None, specs=[spec("spent")], max_attempts=1
    )
    record, _inserted = reservations[0]
    store.activate(record.task_id, workspace_id="w-s", session_id="s-s")
    store.finish(record.task_id, "lost")

    rows = tuple(view.to_dict() for view in observability.read_tasks(store))
    queue = fleet.attention(fleet.AttentionFacts(tasks=rows), now=NOW)

    assert [item.kind for item in queue.items] == ["tripped_breaker"]
    assert queue.items[0].next_action.startswith("Raise the budget")
    assert "cannot be replaced" in queue.items[0].next_action
    with pytest.raises(TaskStoreError, match="circuit breaker"):
        store.prepare_replacement(record.task_id)


def test_counts_and_the_attention_queue_agree_on_one_breaker(
    store: TaskStore,
) -> None:
    """Two surfaces reading the same store cannot disagree (kernel-contract,
    "Observability"). They did: `status` counted every unfinished task at its
    budget while the queue only recognised running ones, so one blocked task
    was simultaneously a tripped breaker in the count and "blocked on an
    approval" in the queue. A finished task counts in neither.
    """
    reservations = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[spec("stuck"), spec("finished")],
        max_attempts=1,
    )
    by_key = {record.key: record for record, _inserted in reservations}
    store.activate(by_key["stuck"].task_id, workspace_id="w-s", session_id="s-s")
    store.finish(by_key["stuck"].task_id, "blocked", "waiting on approval to merge")
    store.activate(by_key["finished"].task_id, workspace_id="w-f", session_id="s-f")
    store.finish(by_key["finished"].task_id, "completed", "shipped")

    views = observability.read_tasks(store)
    counts = observability.task_counts(views)
    queue = fleet.attention(
        fleet.AttentionFacts(tasks=tuple(view.to_dict() for view in views)), now=NOW
    )

    assert counts["breaker_tripped"] == 1
    assert [item.kind for item in queue.items] == ["tripped_breaker"]
    assert queue.items[0].task_key == "stuck"


def test_attention_reports_executor_owned_gaps_as_degraded() -> None:
    """The contract names five attention rows; two of them only cdesktop can
    produce. Reporting them as degraded is honest, whereas omitting them
    silently claims a clean fleet the kernel cannot actually vouch for.
    """
    queue = fleet.attention(fleet.AttentionFacts(), now=NOW)

    assert [entry["source"] for entry in queue.degraded] == [
        "dirty_closeouts",
        "failing_checks",
    ]
    assert all(entry["status"] == "reported-degraded" for entry in queue.degraded)
    assert all(entry["owner"] == "cdesktop" for entry in queue.degraded)


def test_attention_supplied_native_facts_stop_being_degraded() -> None:
    """A caller that already holds the executor facts gets the full queue;
    the degradation is about missing input, never about a missing feature.
    """
    queue = fleet.attention(
        fleet.AttentionFacts(
            dirty_closeouts=({"id": "workspace-a", "workspace_id": "workspace-a"},),
            failing_checks=({"id": "pr-7", "status": "failure"},),
        ),
        now=NOW,
    )

    assert queue.degraded == ()
    assert {item.kind for item in queue.items} == {"dirty_closeout", "failing_check"}


def test_attention_orders_approvals_ahead_of_breakers_and_deliveries(
    store: TaskStore,
) -> None:
    """The queue is a work order, not a dump: a human answers the approval
    that unblocks a worker before they read an unacknowledged report.
    """
    reservations = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[spec("waiting"), spec("stuck"), spec("gone")],
        max_attempts=3,
    )
    by_key = {record.key: record for record, _inserted in reservations}
    store.activate(
        by_key["waiting"].task_id, workspace_id="w-w", session_id="s-w"
    )
    store.finish(by_key["waiting"].task_id, "blocked", "waiting on approval to merge")
    store.activate(by_key["gone"].task_id, workspace_id="w-g", session_id="s-g")
    store.finish(by_key["gone"].task_id, "lost")

    escalations = EscalationStore(store.path)
    escalations.park(
        child_session_id="s-x",
        child_workspace_id="w-x",
        recorded_parent_session_id=None,
        reason="no_parent",
        message="BLOCKED: need a decision",
        dedupe_key="parked-1",
    )

    facts = observability.attention_facts(store, escalations=escalations)
    queue = fleet.attention(facts, now=NOW)

    assert [item.kind for item in queue.items] == [
        "blocked_approval",
        "lost",
        "unacked_delivery",
    ]
    assert queue.items[0].task_key == "waiting"


def test_unacked_deliveries_cover_parked_escalations_and_unmet_orders(
    store: TaskStore,
) -> None:
    """Both are deliveries the kernel took responsibility for and nobody
    closed; showing only one of them would leave real work invisible.
    """
    escalations = EscalationStore(store.path)
    escalations.park(
        child_session_id="s-1",
        child_workspace_id=None,
        recorded_parent_session_id=None,
        reason="parent_archived",
        message="STATUS: done",
        dedupe_key="parked-1",
    )
    escalations.expect_order(
        order_id=None,
        sender_session_id="s-lead",
        recipient_session_id="s-2",
        body="report back when green",
    )

    kinds = [row["kind"] for row in observability.unacked_deliveries(escalations)]
    assert sorted(kinds) == ["parked_escalation", "unmet_order"]


def test_unmet_orders_are_filtered_to_recipients_that_can_act(
    store: TaskStore,
) -> None:
    """The old `cli/fleet.py` projection surfaced an unmet order only while
    its recipient was live and not mid-turn. The kernel queue dropped that
    filter and started reporting orders for sessions that no longer exist.
    Liveness is an executor fact, so a caller that holds it supplies it, and
    then only those recipients' orders are anyone's business.
    """
    escalations = EscalationStore(store.path)
    for recipient in ("s-idle", "s-busy", "s-gone"):
        escalations.expect_order(
            order_id=None,
            sender_session_id="s-lead",
            recipient_session_id=recipient,
            body=f"report back, {recipient}",
        )

    rows = observability.unacked_deliveries(escalations, idle_recipients={"s-idle"})

    assert [row["session_id"] for row in rows] == ["s-idle"]
    assert observability.unacked_deliveries(escalations, idle_recipients=set()) == []


def test_unacked_deliveries_stop_at_the_bound_and_summarize_the_rest(
    store: TaskStore,
) -> None:
    """`pending()` was bounded but `orders(unmet_only=True)` was not, so one
    read emitted every unmet order the host had ever recorded (33k rows,
    10.8MB measured). A queue nobody can read is not a queue: past the bound
    it reports one honest count instead of the rows themselves.
    """
    escalations = EscalationStore(store.path)
    for index in range(250):
        escalations.expect_order(
            order_id=None,
            sender_session_id="s-lead",
            recipient_session_id="s-2",
            body=f"report {index}",
        )

    rows = observability.unacked_deliveries(escalations)

    assert len(rows) == observability.DEFAULT_UNACKED_LIMIT + 1
    summary = rows[0]
    assert summary["kind"] == "suppressed_unacked"
    assert summary["summary"] == "150 older unacknowledged deliveries suppressed"
    # What survives the bound is the newest work, not the oldest history.
    assert rows[-1]["summary"] == "report 249"


def test_a_satisfied_order_leaves_the_attention_queue(store: TaskStore) -> None:
    """Otherwise the queue only grows and operators learn to ignore it."""
    escalations = EscalationStore(store.path)
    escalations.expect_order(
        order_id=None,
        sender_session_id="s-lead",
        recipient_session_id="s-2",
        body="report back",
    )
    escalations.satisfy_orders("s-2")

    assert observability.unacked_deliveries(escalations) == []


def test_task_groups_split_running_from_done_since_view(store: TaskStore) -> None:
    """`overview` keeps its three groups after moving to the kernel store, so
    the surface a human already knows did not change shape, only its source.
    """
    reservations = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[spec("live"), spec("finished")],
        max_attempts=3,
    )
    by_key = {record.key: record for record, _inserted in reservations}
    store.activate(by_key["live"].task_id, workspace_id="w-l", session_id="s-l")
    store.finish(by_key["finished"].task_id, "cancelled")

    rows = [view.to_dict() for view in observability.read_tasks(store)]
    groups = fleet.task_groups(rows, now=datetime.now(UTC))

    assert [row["key"] for row in groups.running] == ["live"]
    assert [row["key"] for row in groups.done_since_view] == ["finished"]


def test_done_since_view_respects_the_lower_bound(store: TaskStore) -> None:
    """A task finished long before the view boundary is history, not news."""
    seed(store, [spec("ancient")])
    record = observability.read_tasks(store)[0]
    store.finish(record.task_id, "cancelled")
    rows = [view.to_dict() for view in observability.read_tasks(store)]

    future = datetime.fromtimestamp(time.time() + 3600, tz=UTC)
    groups = fleet.task_groups(rows, now=future, viewed_at=future)

    assert groups.done_since_view == ()


def test_status_list_and_attention_never_construct_a_client(
    task_cli: TaskStore, capsys
) -> None:
    """The regression this exists for: `status` and `list` fanned out over
    every workspace and session on every call. A client that explodes on
    construction is the only assertion strong enough to keep them honest.
    """
    seed(task_cli, [spec(f"task-{index}") for index in range(5)])

    assert cli.cmd_list(_args("list")) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 5

    assert cli.cmd_status(_args("status")) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["task_counts"]["reserved"] == 5

    assert cli.cmd_attention(_args("attention")) == 0
    attention = json.loads(capsys.readouterr().out)
    assert attention["items"] == []
    assert [entry["source"] for entry in attention["degraded"]] == [
        "dirty_closeouts",
        "failing_checks",
    ]


def test_attention_bounds_its_output_at_the_command(
    task_cli: TaskStore, capsys
) -> None:
    """End to end: the emitted queue is bounded, not merely the helper.

    A measured `sightmesh attention` printed 33k rows / 10.8MB because the
    unmet-order read had no LIMIT. The command now emits at most the bound
    plus one row saying how much it withheld.
    """
    escalations = EscalationStore(task_cli.path)
    for index in range(250):
        escalations.expect_order(
            order_id=None,
            sender_session_id="s-lead",
            recipient_session_id="s-2",
            body=f"report {index}",
        )

    assert cli.cmd_attention(_args("attention")) == 0
    queue = json.loads(capsys.readouterr().out)

    kinds = [item["kind"] for item in queue["items"]]
    assert kinds.count("unacked_delivery") == observability.DEFAULT_UNACKED_LIMIT
    assert kinds.count("suppressed_unacked") == 1
    suppressed = next(
        item for item in queue["items"] if item["kind"] == "suppressed_unacked"
    )
    assert "150 older unacknowledged deliveries suppressed" in suppressed["reason"]
    assert "--all" in suppressed["next_action"]


def test_task_reads_are_bounded_to_the_newest_unless_all_is_asked_for(
    task_cli: TaskStore, capsys
) -> None:
    """`list`, `status`, `overview` and `attention` all read the task table
    with no LIMIT, so a long-lived host answered a routine `list` with its
    entire history. The default is the newest N, `--limit` narrows it, and
    `--all` is the named way to ask for everything.
    """
    seed(task_cli, [spec(f"old-{index}") for index in range(250)])
    seed(task_cli, [spec("newest")])

    assert cli.cmd_list(_args("list")) == 0
    bounded = capsys.readouterr()
    rows = json.loads(bounded.out)
    assert len(rows) == observability.DEFAULT_TASK_LIMIT
    assert "newest" in {row["key"] for row in rows}
    # A bounded read that looks complete is worse than a small one.
    assert "pass --all" in bounded.err

    assert cli.cmd_list(_args("list", "--all")) == 0
    everything = capsys.readouterr()
    assert len(json.loads(everything.out)) == 251
    assert everything.err == ""

    assert cli.cmd_status(_args("status", "--limit", "5")) == 0
    assert len(json.loads(capsys.readouterr().out)["tasks"]) == 5


def test_overview_is_task_local_too(task_cli: TaskStore, capsys) -> None:
    """`overview` was the third fan-out surface; it reads the same store as
    `status` so the two can never disagree (kernel-contract, Observability).
    """
    seed(task_cli, [spec("only")])

    assert cli.cmd_overview(_args("overview")) == 0
    output = json.loads(capsys.readouterr().out)

    assert [row["key"] for row in output["running"]] == ["only"]


def test_the_read_model_module_holds_no_executor_client() -> None:
    """Structural guard: a module that cannot import the client cannot fan
    out, however the CLI above it is later rewired.
    """
    import ast

    source = Path(observability.__file__).read_text(encoding="utf-8")
    imported = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("cdesktop" in name for name in imported), imported
    assert not hasattr(observability, "CdesktopClient")


def test_lease_release_and_renew_never_echo_the_capability(
    tmp_path: Path, capsys
) -> None:
    """Shipped defect: both commands returned `to_dict()`, writing a live
    bearer token into whatever transcript ran them - for a token the caller
    had already typed in. There is no benefit to trade against that risk.
    """
    lease_dir = tmp_path / "leases"
    lease = LeaseStore(lease_dir).acquire("owner", tmp_path, ttl_seconds=600)

    renew = argparse.Namespace(
        lease_dir=str(lease_dir),
        lease_action="renew",
        token=lease.token,
        ttl_seconds=600,
        owner=None,
        workspace_id=None,
        json=True,
    )
    assert cli.cmd_lease(renew) == 0
    renewed = capsys.readouterr().out
    assert lease.token not in renewed
    assert "token" not in json.loads(renewed)

    release = argparse.Namespace(
        lease_dir=str(lease_dir),
        lease_action="release",
        token=lease.token,
        owner=None,
        workspace_id=None,
        json=True,
    )
    assert cli.cmd_lease(release) == 0
    released = capsys.readouterr().out
    assert lease.token not in released
    assert "token" not in json.loads(released)


def test_lease_acquire_delivers_the_capability_by_file_not_stdout(
    tmp_path: Path, capsys
) -> None:
    """`acquire` genuinely must hand over a capability once. It does that
    through a 0600 file and prints only the path, per the standing rule that
    a credential value is never printed.
    """
    args = argparse.Namespace(
        lease_dir=str(tmp_path / "leases"),
        lease_action="acquire",
        owner="owner",
        repo=str(tmp_path),
        worktree=None,
        ttl_seconds=600,
        workspace_id=None,
        session_id=None,
        json=True,
    )

    assert cli.cmd_lease(args) == 0
    output = json.loads(capsys.readouterr().out)

    capability = Path(output["capability_path"])
    assert oct(capability.stat().st_mode & 0o777) == "0o600"
    token = capability.read_text(encoding="utf-8")
    assert len(token) >= 32
    assert token not in json.dumps(output)
    assert "token" not in output


def test_emit_rejects_any_credential_shaped_field() -> None:
    """The CLI has one output path, so guarding it turns a future leak into
    a loud failure instead of a silent serialization.
    """
    for payload in (
        {"token": "secret-value"},
        {"lease": {"token": "secret-value"}},
        [{"api_key": "secret-value"}],
    ):
        with pytest.raises(ValueError, match="credential-shaped"):
            cli._emit(payload, True)


def test_emit_allows_fingerprints_and_usage_counters(capsys) -> None:
    """The guard must not be so broad that redacted fingerprints or token
    *counts* trip it, or callers will route around it.
    """
    cli._emit({"token_fp": "ab12", "token_usage": {"total": 5}}, True)

    assert "ab12" in capsys.readouterr().out


def _skew_state(monkeypatch, state: dict, stale: list[str]) -> None:
    monkeypatch.setattr(cli.updates, "read_state", lambda: state)
    monkeypatch.setattr(
        cli.updates, "prune", lambda **_kwargs: {"removed": stale, "retained": []}
    )


def test_doctor_reports_version_skew_with_all_three_versions(monkeypatch) -> None:
    """Skew was invisible until something broke, which is how three cdesktop
    releases accumulated on one host. Naming the installed CLI, the running
    service and the active release in one failing check makes the
    disagreement actionable instead of a mystery (kernel-contract: status,
    doctor and direct reads cannot disagree).
    """
    _skew_state(
        monkeypatch,
        {"active": {"version": "0.2.4-sightmesh.1"}, "pending": None},
        [],
    )

    check = cli._version_skew_check(
        "0.2.5-sightmesh.1", "cdesktop/0.2.4-sightmesh.1 darwin-arm64"
    )

    assert check["ok"] is False
    assert check["detail"]["running_service"] == "0.2.5-sightmesh.1"
    assert check["detail"]["installed_cli"] == "0.2.4-sightmesh.1"
    assert check["detail"]["active"] == "0.2.4-sightmesh.1"


def test_doctor_fails_on_stale_releases_even_when_versions_agree(monkeypatch) -> None:
    """Releases past the prune budget are the disk-filling half of the same
    defect; agreeing versions do not make three retained copies acceptable.
    """
    _skew_state(
        monkeypatch,
        {"active": {"version": "0.2.4-sightmesh.1"}, "pending": None},
        ["/updates/cdesktop-0.2.2", "/updates/cdesktop-0.2.3"],
    )

    check = cli._version_skew_check("0.2.4-sightmesh.1", "cdesktop/0.2.4-sightmesh.1")

    assert check["ok"] is False
    assert len(check["detail"]["stale_releases"]) == 2


def test_doctor_passes_when_every_reported_version_agrees(monkeypatch) -> None:
    """A staged-but-not-yet-active release is expected, not skew; failing on
    it would train operators to ignore the check.
    """
    _skew_state(
        monkeypatch,
        {
            "active": {"version": "0.2.4-sightmesh.1"},
            "pending": {"version": "0.2.5-sightmesh.1"},
        },
        [],
    )

    check = cli._version_skew_check("0.2.4-sightmesh.1", "cdesktop/0.2.4-sightmesh.1")

    assert check["ok"] is True
    assert check["detail"]["staged"] == "0.2.5-sightmesh.1"


class ApprovalClient:
    """A cdesktop whose pending tool action carries a nested credential.

    Agents author these action dicts; the CLI only relays them. The header
    below is exactly the shape that took the whole inbox down.
    """

    def __init__(self, _url=None) -> None:
        self.responses: list[tuple] = []
        self.approval_response: object = {"status": "approved"}

    def workspace_summaries(self, _archived=False):
        return [{"workspace_id": "workspace-a", "latest_process_status": "running"}]

    def workspaces(self):
        return [{"id": "workspace-a", "name": "catapult", "archived": False}]

    def sessions(self, _workspace_id):
        return [{"id": "session-a", "name": "lead", "executor": "CLAUDE_CODE"}]

    def execution_processes(self, _session_id):
        return [{"id": "process-a", "status": "completed", "run_reason": "codingagent"}]

    def pending_approvals(self):
        return [
            {
                "approval_id": "approval-a",
                "execution_process_id": "process-a",
                "tool_name": "Bash",
                "is_question": False,
            }
        ]

    def execution_process(self, _process_id):
        return {"session_id": "session-a"}

    def session(self, _session_id):
        return {"workspace_id": "workspace-a", "executor": "CLAUDE_CODE"}

    def workspace(self, _workspace_id):
        return {"name": "catapult", "archived": False}

    def normalized_snapshot(self, _process_id):
        return {
            "complete": True,
            "patch_count": 1,
            "entries": [
                {
                    "content": {
                        "content": "curl the deploy API",
                        "entry_type": {
                            "type": "tool_use",
                            "status": {
                                "status": "pending_approval",
                                "approval_id": "approval-a",
                            },
                            "action_type": {
                                "action": "run_command",
                                "request": {
                                    "headers": {"Authorization": "Bearer live-secret"}
                                },
                            },
                        },
                    }
                }
            ],
        }

    def respond_to_approval(self, approval_id, process_id, *, approved, reason=None):
        self.responses.append((approval_id, process_id, approved, reason))
        return self.approval_response


@pytest.fixture
def approval_cli(monkeypatch, tmp_path: Path) -> ApprovalClient:
    client = ApprovalClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    monkeypatch.setattr(
        cli.approvals, "approval_db_path", lambda: tmp_path / "audit.db"
    )
    monkeypatch.setattr(cli.escalation, "escalation_db_path", lambda: tmp_path / "e.db")
    return client


def test_a_credential_inside_a_relayed_payload_is_redacted_not_fatal(
    approval_cli: ApprovalClient, capsys
) -> None:
    """One nested `Authorization` header killed the entire inbox.

    The guard exists for dicts this CLI builds - a credential in one of
    those is a defect here. An agent's tool action is a payload we merely
    relay, and refusing to render it hides every other pending request on
    the host. Redacting the value keeps the operator's view and still never
    prints the credential.
    """
    assert cli.cmd_inbox(_args("inbox")) == 0
    output = capsys.readouterr().out

    assert "live-secret" not in output
    assert cli.REDACTED in output
    rows = json.loads(output)
    assert rows[0]["approval_id"] == "approval-a"
    headers = rows[0]["request"]["action"]["request"]["headers"]
    assert headers == {"Authorization": cli.REDACTED}


def test_an_approval_that_went_through_is_never_reported_as_an_error(
    approval_cli: ApprovalClient, capsys
) -> None:
    """The approval succeeded and then `_emit` raised on the pass-through
    payload, so the operator saw a failure for an action cdesktop had
    already carried out - and could reasonably retry it. A completed action
    reports its true result; rendering can only degrade to a warning.
    """

    # Anything cdesktop returns that the CLI cannot render: here a value
    # json refuses to serialize, in the shipped defect a nested credential.
    approval_cli.approval_response = {"finished_at": object()}
    args = _args("approval", "approve", "approval-a", "--allow-non-plan")

    assert cli.cmd_approval(args) == 0

    captured = capsys.readouterr()
    assert approval_cli.responses == [("approval-a", "process-a", True, None)]
    assert "warning: the action succeeded" in captured.err
    assert json.loads(captured.out) == {
        "approval_id": "approval-a",
        "decision": "approved",
        "status": "responded",
    }


def test_approve_reports_the_answered_approval_with_the_credential_removed(
    approval_cli: ApprovalClient, capsys
) -> None:
    """The result echoes the same relayed action the inbox showed, so it
    needs the same treatment: the decision is reported in full, the
    credential is not.
    """
    args = _args("approval", "approve", "approval-a", "--allow-non-plan")

    assert cli.cmd_approval(args) == 0
    output = capsys.readouterr().out

    assert "live-secret" not in output
    result = json.loads(output)
    assert result["decision"]["decision"] == "approved"
    assert result["cdesktop_response"] == {"status": "approved"}
