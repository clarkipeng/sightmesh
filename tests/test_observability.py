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
from sightmesh.task_store import TaskStore

NOW = datetime(2026, 9, 1, tzinfo=UTC)


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

    assert cli.cmd_list(argparse.Namespace(url=None, json=True)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 5

    assert cli.cmd_status(argparse.Namespace(url=None, port=8377, json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["task_counts"]["reserved"] == 5

    assert cli.cmd_attention(argparse.Namespace(url=None, json=True)) == 0
    attention = json.loads(capsys.readouterr().out)
    assert attention["items"] == []
    assert [entry["source"] for entry in attention["degraded"]] == [
        "dirty_closeouts",
        "failing_checks",
    ]


def test_overview_is_task_local_too(task_cli: TaskStore, capsys) -> None:
    """`overview` was the third fan-out surface; it reads the same store as
    `status` so the two can never disagree (kernel-contract, Observability).
    """
    seed(task_cli, [spec("only")])

    args = argparse.Namespace(url=None, json=True, since=None)
    assert cli.cmd_overview(args) == 0
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
