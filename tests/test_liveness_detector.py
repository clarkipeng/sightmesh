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

    def add_process(self, session_id: str, **row: Any) -> str:
        """Append one more process row, as a session's history accumulates."""
        pid = str(row.pop("id", f"proc-{session_id}-{len(self.processes.get(session_id, []))}"))
        self.processes.setdefault(session_id, []).append({"id": pid, **row})
        self.snapshots.setdefault(pid, {"entries": [], "stream_alive": True})
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


# ======================================================================
# Locking tests for the review reproductions. Each one replays a shape a
# reviewer built by hand against the pre-fix detector; each names the damage
# the old behavior did, because "what it returns" is not the reason any of
# these exist.
# ======================================================================


def test_a_stale_killed_row_never_marks_a_running_task_lost(store):
    """Repro (killed-history + running): a session accumulates process rows.
    The detector scanned all of them and `classify` checked `lost` before it
    checked whether anything was running, so one `killed` row from a finished
    turn marked a healthy RUNNING task irreversibly lost. `lost` is terminal,
    a lost child gets replaced, and the replacement joins a worker that never
    stopped working - two sessions on one branch."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    client.add_process(
        "s-child",
        id="old",
        status="killed",
        exit_reason="restart",
        run_reason="codingagent",
        updated_at=NOW - 5_000,
    )
    client.add_process(
        "s-child",
        id="new",
        status="running",
        run_reason="codingagent",
        updated_at=NOW - 5,
    )

    reconciler(client, store, lambda: NOW).detect_liveness()

    healthy = store.get_by_id(child.task_id)
    assert healthy.state == "active", "a running task must never be recorded lost"
    assert healthy.liveness == "live"
    assert wake_rows(store, "any_child_lost") == []


def test_a_flaky_snapshot_endpoint_cannot_mint_an_episode_per_tick(store):
    """Repro (flapping): `stream_alive` defaulted to True whenever the snapshot
    read failed, so a flaky endpoint flipped the classification between
    `stalled` and `limbo` on alternate ticks. Every flip counted as an episode
    boundary, which reset `liveness_wakes` to zero, which meant the escalation
    gated at two wakes could never fire: ten ticks produced ten wakes and no
    human was ever told. One unbroken silence is one episode, whatever it gets
    called along the way."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    pid = client.run("s-child", last_activity=NOW - 100_000)
    clock = {"now": NOW}
    pass_ = reconciler(client, store, lambda: clock["now"])

    for tick in range(10):
        # Alternate between a healthy snapshot and one the endpoint refuses.
        client.snapshots[pid] = {
            "entries": [],
            "stream_alive": tick % 2 == 0,
        }
        clock["now"] += 1
        pass_.detect_liveness()

    flapped = store.get_by_id(child.task_id)
    assert flapped.liveness_episode == 1, "one silence is one episode"
    assert len(wake_rows(store, "any_child_stalled")) == 1


def test_an_unreachable_manager_still_produces_a_human_attention_item(store):
    """Repro (swallowed escalation): the escalation re-used the episode's
    dedupe key, and the partial unique index rejects a duplicate while the
    first wake is still pending or claimed. That is exactly the escalation
    case - the manager never consumed the first wake because it is
    unreachable - so `liveness_wakes` stopped at one, the exhaustion check
    never tripped, and the incident vanished. A distinct escalation key gets
    the second wake in; the elapsed-phase check parks the human item even if
    it had not."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    client.run("s-child", last_activity=NOW - 100_000)
    clock = {"now": NOW}
    pass_ = reconciler(client, store, lambda: clock["now"])

    # Only the detector runs, never the pump: the first wake stays pending
    # forever, which is what an unreachable manager looks like from here.
    pass_.detect_liveness()
    assert store.get_by_id(child.task_id).liveness_wakes == 1

    clock["now"] = NOW + 1_501
    pass_.detect_liveness()

    keys = {str(row["dedupe_key"]) for row in wake_rows(store, "any_child_stalled")}
    assert keys == {
        f"{child.task_id}:{child.epoch}:stalled:1",
        f"{child.task_id}:{child.epoch}:stalled:1:escalation",
    }
    parked = [
        item
        for item in EscalationStore(store.path).pending()
        if "after two manager wakes" in item.message
    ]
    assert len(parked) == 1


def test_a_wedged_root_task_reports_to_the_human_queue(store):
    """Repro (no manager): a root task has no parent row, so
    `record_liveness_wakes` returns nothing, `liveness_wakes` never moves and
    the exhaustion path is unreachable. The kernel could see the incident
    perfectly and had no one to tell. A parentless finding is a human's."""
    root = task(store, "root", session="s-root", children=4)
    client = FakeClient()
    client.run("s-root", last_activity=NOW - 100_000)

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert store.get_by_id(root.task_id).liveness == "stalled"
    assert [
        item
        for item in EscalationStore(store.path).pending()
        if "has no manager to wake" in item.message
    ]


def test_a_replacement_starts_with_a_clean_liveness_record(store):
    """Repro (inherited bookkeeping): `prepare_replacement` bumped the epoch
    and reset none of the liveness fields. The successor inherited an episode
    that had already spent both its wakes, so its own stall could never be
    reported; it inherited the `over_budget` latch, so a brand-new worker was
    announced as over budget; and it inherited `liveness_since`, dating its
    silence from its predecessor's. A replacement is a fresh subject."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(
        store,
        "child",
        parent=manager.task_id,
        session="s-child",
        budget=Budget(max_turns=1).to_dict(),
    )
    client = FakeClient()
    client.run("s-child", last_activity=NOW - 100_000)
    pass_ = reconciler(client, store, lambda: NOW)
    pass_.detect_liveness()
    exhausted = store.get_by_id(child.task_id)
    assert (exhausted.liveness, exhausted.over_budget) == ("stalled", True)

    successor = store.prepare_replacement(child.task_id)

    assert successor.epoch == child.epoch + 1
    assert successor.liveness == "live"
    assert successor.liveness_episode == 0
    assert successor.liveness_wakes == 0
    assert successor.liveness_since is None
    assert successor.liveness_evidence is None
    assert successor.over_budget is False
    assert successor.checkpoint_at is None


def test_one_hostile_payload_never_costs_the_other_tasks_their_pass(store):
    """Repro (pass isolation): `reconcile_kernel` caught only TaskStoreError,
    so a TypeError or ValueError out of one task's executor payload escaped
    the whole pass - and with it wake delivery and reservation expiry, which
    run after the detector. One bad row silenced the entire fleet."""
    manager = task(store, "manager", session="s-manager", children=4)
    poisoned = task(store, "poison", parent=manager.task_id, session="s-poison")
    healthy = task(store, "healthy", parent=manager.task_id, session="s-healthy")

    class Hostile(FakeClient):
        def execution_processes(self, session_id: str):
            if session_id == "s-poison":
                raise TypeError("processes is not a list")
            return super().execution_processes(session_id)

    client = Hostile()
    client.run("s-healthy", last_activity=NOW - 100_000)
    counts = reconciler(client, store, lambda: NOW).reconcile_kernel()

    assert store.get_by_id(poisoned.task_id).liveness == "live", "no verdict from junk"
    assert store.get_by_id(healthy.task_id).liveness == "stalled"
    # The pump ran: the finding did not just get recorded, it got delivered.
    assert counts["wakes_delivered"] >= 1
    assert [row for row in client.sent if row[0] == "s-manager"]


def test_a_malformed_stored_policy_is_one_attention_item_not_a_dead_pass(store):
    """Repro (poisoned spec_json): `detection_policy` read the timeouts
    straight off the durable row, so a stored `progress_timeout` of 0 raised
    out of the fleet pass - or, worse, made every silence instantly stalled.
    A task nobody can watch is itself the incident."""
    manager = task(store, "manager", session="s-manager", children=4)
    task(store, "poison", parent=manager.task_id, session="s-poison", progress_timeout=0)
    healthy = task(store, "healthy", parent=manager.task_id, session="s-healthy")
    client = FakeClient()
    client.run("s-poison", last_activity=NOW - 100_000)
    client.run("s-healthy", last_activity=NOW - 100_000)

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert store.get_by_id(healthy.task_id).liveness == "stalled"
    assert [
        item
        for item in EscalationStore(store.path).pending()
        if "unusable detection policy" in item.message
    ]


def test_a_restart_never_manufactures_progress_from_a_missing_baseline(store):
    """Repro (restart baseline / baseline poisoning): the output-byte baseline
    was written from every pass, including failed reads that reported zero
    bytes. A failed read followed by a successful one looked like growth,
    growth reads as progress, progress CLOSES the episode and resets the wake
    count - so a flapping executor latched a wedged child into permanent
    apparent health. Only a complete read may set the baseline, and a tick
    with no baseline to compare against says so instead of guessing."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")

    class Flapping(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False

        def execution_processes(self, session_id: str):
            if self.fail and session_id == "s-child":
                raise RuntimeError("executor unavailable")
            return super().execution_processes(session_id)

    client = Flapping()
    pid = client.run("s-child", last_activity=NOW - 100_000)
    client.processes["s-child"][0]["output_bytes"] = 4_096
    clock = {"now": NOW}
    pass_ = reconciler(client, store, lambda: clock["now"])

    # First observation: bytes are reported but nothing to compare them with.
    pass_.detect_liveness()
    assert store.get_by_id(child.task_id).liveness == "live", "no verdict without a baseline"

    clock["now"] += 1
    pass_.detect_liveness()
    stalled = store.get_by_id(child.task_id)
    assert stalled.liveness == "stalled"
    assert stalled.liveness_wakes == 1

    # Now the endpoint flaps. The failed read must not overwrite the baseline,
    # so the read after it cannot look like 4096 bytes of fresh output.
    client.fail = True
    clock["now"] += 1
    pass_.detect_liveness()
    client.fail = False
    clock["now"] += 1
    pass_.detect_liveness()

    still_wedged = store.get_by_id(child.task_id)
    assert still_wedged.liveness == "stalled", "a flapping endpoint is not progress"
    assert still_wedged.liveness_episode == 1
    assert still_wedged.liveness_wakes == 1
    assert client.calls.count("execution_processes") >= 4
    _ = pid


def test_a_child_that_ended_its_turn_cleanly_is_still_watched(store):
    """Repro (between turns): on the shipped client a coding-agent process is
    `running` only during a turn. The detector answered `unknown` for anything
    without a running process, before it ever read a timestamp - so a worker
    that finished a turn and went silent forever stayed `live`, armed nothing,
    and blocked its manager's cohort. That is the primary case, not an edge."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    client = FakeClient()
    client.add_process(
        "s-child",
        id="finished",
        status="completed",
        run_reason="codingagent",
        updated_at=NOW - 100_000,
    )

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert store.get_by_id(child.task_id).liveness == "stalled"
    assert len(wake_rows(store, "any_child_stalled")) == 1


def test_a_blocked_child_stops_exempting_its_manager_from_detection(store):
    """Repro (invisible deadlock): the manager exemption counted every LIVE
    state, so a child sitting `blocked` on a human made its manager look like
    it was legitimately yielding - forever. The whole subtree went quiet with
    nothing in the system flagging it. Only a child that is actually running
    is a reason to stop watching the manager waiting on it."""
    root = task(store, "root", session="s-root", children=4)
    manager = task(store, "manager", parent=root.task_id, session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    finish_with_wake(store, child.task_id, "blocked", "needs a human")
    client = FakeClient()
    client.run("s-manager", last_activity=NOW - 100_000)

    reconciler(client, store, lambda: NOW).detect_liveness()

    assert store.get_by_id(manager.task_id).liveness == "stalled"


def test_a_reservation_that_never_launched_becomes_an_attention_item(store):
    """Why: a reserved task has no session, so there is nothing to read and it
    used to be skipped entirely - and, because it also exempted its manager,
    a reservation that never launched wedged the subtree behind it with no
    signal anywhere. It cannot be classified, so it is escalated."""
    manager = task(store, "manager", session="s-manager", children=4)
    ((reserved, _inserted),) = store.reserve_all(
        scope="operator",
        parent_task_id=manager.task_id,
        specs=[{"key": "never-launched", "children": 0}],
        max_attempts=3,
    )
    client = FakeClient()
    later = reserved.created_at + 100_000

    reconciler(client, store, lambda: later).detect_liveness()

    assert [
        item
        for item in EscalationStore(store.path).pending()
        if "reserved without ever launching" in item.message
    ]


def test_a_finished_task_cannot_acquire_a_budget_finding(store):
    """Why: `record_over_budget` had no state guard, so a late tick could
    latch the flag on a task that had already completed - an attention item
    and a wake about a worker that is not there any more."""
    manager = task(store, "manager", session="s-manager", children=4)
    child = task(store, "child", parent=manager.task_id, session="s-child")
    store.finish(child.task_id, "completed", "done")

    assert store.record_over_budget(child.task_id) is False
    assert store.get_by_id(child.task_id).over_budget is False


def test_a_large_fleet_is_covered_across_ticks_rather_than_starving_delivery(store):
    """Why: the pass is synchronous inside a two-second bridge tick and costs
    executor round-trips per task, ahead of wake delivery and reservation
    expiry. Uncapped, a large fleet turns the detector into the thing that
    stops managers being woken at all. Capped without a cursor, the tail of
    the fleet would simply never be looked at."""
    manager = task(store, "manager", session="s-manager", children=12)
    children = [
        task(store, f"child{index}", parent=manager.task_id, session=f"s-child{index}")
        for index in range(9)
    ]
    client = FakeClient()
    for index in range(9):
        client.run(f"s-child{index}", last_activity=NOW - 100_000)
    pass_ = DurableExecutionReconciler(
        client,
        task_store=store,
        ownership=FakeOwnership(),
        signal_store=EscalationStore(store.path),
        clock=lambda: NOW,
        tasks_per_pass=3,
    )

    pass_.detect_liveness()
    seen_after_one = sum(
        1 for child in children if store.get_by_id(child.task_id).liveness == "stalled"
    )
    assert seen_after_one <= 3, "the cap has to actually cap"

    for _ in range(5):
        pass_.detect_liveness()
    assert all(
        store.get_by_id(child.task_id).liveness == "stalled" for child in children
    ), "the cursor has to actually advance"
