"""The detector pass inside the durable reconciler (docs/liveness-spec.md).

``test_liveness.py`` covers the pure classifier. This file covers what the
reconciler does with a classification: which rows it writes, which predicates
it arms, and - most of the tests here - the things it must refuse to do.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sightmesh.durable import DurableExecutionReconciler
from sightmesh.escalation import EscalationStore
from sightmesh.liveness import Budget
from sightmesh.task_store import TaskStore
from sightmesh.wakes import finish_with_wake

NOW = 1_700_000_000.0


class FakeClient:
    """Answers only the metadata reads the detector is allowed to make.

    Anything else raising is deliberate: it is how this suite proves the
    detector performs no workspace refresh and no Git fan-out, per the
    contract's "metadata-only" clause.
    """

    def __init__(self) -> None:
        self.processes: dict[str, list[dict[str, Any]]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[Any, ...]] = []
        self.stopped: list[str] = []
        self.calls: list[str] = []

    def execution_processes(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append("execution_processes")
        return self.processes.get(session_id, [])

    def normalized_snapshot(self, process_id: str) -> dict[str, Any]:
        self.calls.append("normalized_snapshot")
        return self.snapshots.get(process_id, {"entries": [], "stream_alive": True})

    def queue_status(self, session_id: str) -> dict[str, Any]:
        self.calls.append("queue_status")
        return {"pending": 0}

    def send(self, session_id: str, prompt: str, sender: Any = None, **kwargs: Any) -> Any:
        self.sent.append((session_id, prompt, kwargs.get("intent")))
        return {"queued": True}

    def stop_execution(self, process_id: str, **_kwargs: Any) -> Any:
        self.stopped.append(process_id)
        return {"stopped": True}

    def managed_effect(self, task_id: str, epoch: int) -> dict[str, Any]:
        return {"state": "active", "workspace_id": "ws", "session_id": "sess"}

    def run(self, session_id: str, *, last_activity: float, **snapshot: Any) -> str:
        pid = f"proc-{session_id}"
        self.processes[session_id] = [
            {
                "id": pid,
                "status": "running",
                "run_reason": "codingagent",
                "updated_at": last_activity,
            }
        ]
        self.snapshots[pid] = {"entries": [], "stream_alive": True, **snapshot}
        return pid


class FakeOwnership:
    def is_quarantined(self, _session_id: str) -> bool:
        return False

    def assert_deliverable(self, _session_id: str) -> None:
        return None


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "state.sqlite3")


def reconciler(client: FakeClient, store: TaskStore, clock: Any) -> DurableExecutionReconciler:
    return DurableExecutionReconciler(
        client,
        task_store=store,
        ownership=FakeOwnership(),
        signal_store=EscalationStore(store.path),
        clock=clock,
    )


def task(
    store: TaskStore, key: str, *, parent: str | None = None, session: str, **spec: Any
):
    ((record, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=parent,
        specs=[{"key": key, "children": spec.pop("children", 0), **spec}],
        max_attempts=3,
    )
    return store.activate(record.task_id, workspace_id=f"ws-{key}", session_id=session)


def wake_rows(store: TaskStore, predicate: str) -> list[sqlite3.Row]:
    with store._database._connect() as conn:  # noqa: SLF001 - test-only introspection
        return conn.execute(
            "SELECT * FROM task_wakes WHERE predicate = ?", (predicate,)
        ).fetchall()


def test_a_manager_yielding_to_running_children_is_never_idle_unreported(store):
    """Requirement 5, and the exemption the spec calls out by name. A manager
    ends its turn and goes quiet *because* its children are working - that is
    the designed shape of the whole system ("managers yield while children
    run"). Without this the fleet's every manager would be flagged the moment
    it dispatched work, which is worse than having no detector at all."""
    manager = task(store, "manager", session="s-manager", children=4)
    task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    client.run("s-manager", last_activity=NOW - 100_000, turn_ended=True)

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert store.get_by_id(manager.task_id).liveness == "live"
    assert wake_rows(store, "any_child_stalled") == []


def test_a_manager_whose_children_all_finished_is_back_in_scope(store):
    """The exemption has to expire, or a manager that silently dies right after
    its last child reports becomes permanently invisible - the cohort is
    complete, nobody advances it, and the run stalls with no signal at all."""
    root = task(store, "root", session="s-root", children=4)
    manager = task(store, "manager", parent=root.task_id, session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    store.finish(child.task_id, "completed", "done")
    client = FakeClient()
    client.run("s-manager", last_activity=NOW - 100_000, turn_ended=True)

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert store.get_by_id(manager.task_id).liveness == "idle_unreported"


def test_crossing_a_budget_flags_the_task_without_touching_its_state(store):
    """Cause 5. A budget is evidence, not a fence: the task keeps running and
    the manager decides. An implementation that blocked or cancelled here would
    be the kernel making the "grinding without converging" judgment the spec
    reserves for a human-shaped decision."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(
        store,
        "child",
        parent=manager.task_id,
        session="s-child",
        budget=Budget(max_turns=1).to_dict(),
        progress_timeout=1500.0,
    )
    client = FakeClient()
    client.run("s-child", last_activity=NOW)

    reconciler(client, store, lambda: NOW).detect_liveness()

    flagged = store.get_by_id(child.task_id)
    assert flagged.over_budget is True
    assert flagged.state == "active", "a budget never terminates a task"
    assert flagged.liveness == "live", "over budget is orthogonal to being stalled"
    assert len(wake_rows(store, "any_child_over_budget")) == 1
    assert client.stopped == []


def test_a_budget_wakes_the_manager_exactly_once_per_epoch(store):
    """The flag is a latch and the wake is one-shot. Re-arming every tick would
    reproduce the notification storm the cohort design exists to kill."""
    manager = task(store, "manager", session="s-manager", children=4)
    task(
        store,
        "child",
        parent=manager.task_id,
        session="s-child",
        budget=Budget(max_turns=1).to_dict(),
    )
    client = FakeClient()
    client.run("s-child", last_activity=NOW)
    pass_ = reconciler(client, store, lambda: NOW)

    for _ in range(5):
        pass_.detect_liveness()

    assert len(wake_rows(store, "any_child_over_budget")) == 1


def test_a_task_the_executor_cannot_describe_is_left_exactly_as_found(store):
    """Degraded mode's central guarantee. With no processes, no snapshot, and
    no timestamps, the detector has learned nothing - so it must write nothing.
    Any other behavior means an executor outage silently rewrites the state of
    every task in the fleet."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    before = store.get_by_id(child.task_id)
    client = FakeClient()  # no processes registered at all

    reconciler(client, store, lambda: NOW).detect_liveness()

    after = store.get_by_id(child.task_id)
    assert (after.liveness, after.liveness_episode, after.version) == (
        before.liveness,
        before.liveness_episode,
        before.version,
    )
    assert wake_rows(store, "any_child_stalled") == []


def test_a_liveness_write_does_not_invalidate_a_managers_in_flight_read(store):
    """A guarded transition fails on a version mismatch, so if the detector
    bumped `version` it would make every manager's read-then-replace race the
    reconciler tick - the detector would manufacture exactly the conflicts it
    exists to report."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    observed = store.get_by_id(child.task_id)
    client = FakeClient()
    client.run("s-child", last_activity=NOW - 100_000)

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert store.get_by_id(child.task_id).liveness == "stalled"
    # The manager's optimistic write, planned before the tick, still lands.
    store.prepare_replacement(child.task_id, expect_version=observed.version)


def test_progress_closes_an_open_episode_and_the_next_stall_is_a_new_one(store):
    """Episode identity is what makes dedupe safe to be aggressive. A child
    that stalls, recovers, and stalls again is two incidents; collapsing them
    would leave the manager blind to the second one forever."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    clock = {"now": NOW}
    pass_ = reconciler(client, store, lambda: clock["now"])

    client.run("s-child", last_activity=NOW - 100_000)
    pass_.detect_liveness()
    assert store.get_by_id(child.task_id).liveness_episode == 1

    # The child comes back to life.
    clock["now"] = NOW + 10
    client.run("s-child", last_activity=clock["now"])
    pass_.detect_liveness()
    recovered = store.get_by_id(child.task_id)
    assert recovered.liveness == "live"
    assert recovered.liveness_wakes == 0

    # ...and goes quiet again. A second, distinctly-keyed episode.
    clock["now"] = NOW + 100_000
    pass_.detect_liveness()
    relapsed = store.get_by_id(child.task_id)
    assert relapsed.liveness_episode == 2
    keys = {str(row["dedupe_key"]) for row in wake_rows(store, "any_child_stalled")}
    assert keys == {
        f"{child.task_id}:{child.epoch}:stalled:1",
        f"{child.task_id}:{child.epoch}:stalled:2",
    }


def test_a_liveness_wake_never_consumes_the_cohort_watermark(store):
    """The two wake families share one outbox but must not share one clock. If
    a liveness wake advanced `last_woken_seq`, the child terminal it was
    warning about would arrive with the watermark already past it and the
    cohort wake would never arm - the manager would be told the child is quiet
    and then never told it finished."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    client.run("s-child", last_activity=NOW - 100_000)
    pass_ = reconciler(client, store, lambda: NOW)

    pass_.reconcile_kernel()
    assert len(wake_rows(store, "any_child_stalled")) == 1

    finish_with_wake(store, child.task_id, "completed", "done at last")
    pass_.reconcile_kernel()
    assert len(wake_rows(store, "all_children_terminal")) == 1


def test_the_detector_reads_only_metadata_endpoints(store):
    """"Metadata-only, zero Git" is a contract clause, not a preference: a
    per-task workspace refresh would put a subprocess-bound Git operation on
    the reconciler's hot path for every task in the fleet, every tick."""
    manager = task(store, "manager", session="s-manager", children=4)
    task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    client.run("s-child", last_activity=NOW - 100_000)

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert set(client.calls) <= {
        "execution_processes",
        "normalized_snapshot",
        "queue_status",
    }


def test_a_child_that_reports_itself_blocked_stops_arming_stall_wakes(store):
    """A child can go quiet and *then* explain itself. Once it is blocked, the
    manager has already been woken through `any_child_blocked` with the real
    reason; re-arming the stall episode would wake the manager a second time
    about a silence that is no longer unexplained - and `blocked` tasks can sit
    for a long time by design (a parked approval waiting on a human)."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    client.run("s-child", last_activity=NOW - 100_000)
    clock = {"now": NOW}
    pass_ = reconciler(client, store, lambda: clock["now"])

    pass_.detect_liveness()
    assert len(wake_rows(store, "any_child_stalled")) == 1

    finish_with_wake(store, child.task_id, "blocked", "needs a human decision")
    clock["now"] = NOW + 100_000
    pass_.detect_liveness()

    assert len(wake_rows(store, "any_child_stalled")) == 1
    assert len(wake_rows(store, "any_child_blocked")) == 1
