from concurrent.futures import ThreadPoolExecutor

import pytest

from sightmesh.escalation import EscalationStore
from sightmesh.tasks import TaskLaunchError, TaskLaunchStore


def task_store(tmp_path):
    return TaskLaunchStore(EscalationStore(tmp_path / "state.sqlite3"))


def test_duplicate_wakes_and_concurrent_callers_reserve_one_launch(tmp_path):
    path = tmp_path / "state.sqlite3"

    def reserve():
        return TaskLaunchStore(EscalationStore(path)).reserve("stable-task")

    with ThreadPoolExecutor(max_workers=8) as workers:
        reservations = list(workers.map(lambda _: reserve(), range(8)))

    assert sum(item.should_spawn for item in reservations) == 1
    assert {item.task.task_id for item in reservations} == {"stable-task"}
    assert {item.task.spawn_attempts for item in reservations} == {1}


def test_activation_makes_spawn_idempotent(tmp_path):
    store = task_store(tmp_path)
    first = store.reserve("stable-task")
    store.activate(
        "stable-task",
        first.reservation_id,
        workspace_id="workspace-1",
        session_id="session-1",
    )

    duplicate = store.reserve("stable-task")
    assert duplicate.should_spawn is False
    assert duplicate.task.workspace_id == "workspace-1"
    assert duplicate.task.session_id == "session-1"
    assert store.get("stable-task").reservation_id == first.reservation_id


def test_activation_persistence_failure_replays_the_same_capability(tmp_path):
    store = task_store(tmp_path)
    first = store.reserve("stable-task")
    with store.store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_activation BEFORE UPDATE ON task_launches "
            "WHEN NEW.state = 'active' BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
    with pytest.raises(TaskLaunchError, match="crash"):
        store.activate(
            "stable-task", first.reservation_id,
            workspace_id="workspace-1", session_id="session-1",
        )
    assert store.get("stable-task").state == "reserved"

    with store.store._connect() as conn:
        conn.execute("DROP TRIGGER fail_activation")
    recovered = store.activate(
        "stable-task", first.reservation_id,
        workspace_id="workspace-1", session_id="session-1",
    )
    assert recovered.state == "active"


def test_manager_crash_requires_explicit_capability_transfer(tmp_path):
    store = task_store(tmp_path)
    crashed = store.reserve("stable-task")
    assert store.reserve("stable-task").should_spawn is False

    successor = store.transfer_reservation(
        "stable-task", crashed.reservation_id, prior_spawn_absent=True
    )
    with pytest.raises(TaskLaunchError, match="no longer owned"):
        store.activate(
            "stable-task",
            crashed.reservation_id,
            workspace_id="wrong",
            session_id="wrong",
        )
    active = store.activate(
        "stable-task",
        successor.reservation_id,
        workspace_id="workspace-2",
        session_id="session-2",
    )
    assert active.state == "active"


def test_transfer_requires_owner_proof_and_consumes_attempt_budget(tmp_path):
    state = EscalationStore(tmp_path / "state.sqlite3")
    store = TaskLaunchStore(state)
    first = store.reserve("stable-task", max_spawn_attempts=2)

    with pytest.raises(TaskLaunchError, match="destination-owned proof"):
        store.transfer_reservation("stable-task", first.reservation_id)
    assert store.get("stable-task").spawn_attempts == 1

    second = store.transfer_reservation(
        "stable-task", first.reservation_id, prior_spawn_absent=True
    )
    assert second.task.spawn_attempts == 2
    blocked = store.transfer_reservation(
        "stable-task", second.reservation_id, prior_spawn_absent=True
    )
    assert blocked.should_spawn is False
    assert blocked.task.state == "blocked"
    assert len(state.pending()) == 1


def test_manager_failover_preserves_identity_limits_and_is_single_winner(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = TaskLaunchStore(EscalationStore(path))
    first = store.reserve("manager", max_children=7, max_spawn_attempts=4)
    store.activate(
        "manager", first.reservation_id,
        workspace_id="workspace-a", session_id="session-a",
    )

    def failover():
        return TaskLaunchStore(EscalationStore(path)).reserve_failover("session-a")

    with ThreadPoolExecutor(max_workers=8) as workers:
        attempts = list(workers.map(lambda _: failover(), range(8)))

    assert all(item.should_spawn for item in attempts)
    assert len({item.reservation_id for item in attempts}) == 1
    task = store.get("manager")
    assert task.task_id == "manager"
    assert task.max_children == 7
    assert task.max_spawn_attempts == 4
    assert task.spawn_attempts == 2


def test_fixed_child_budget_and_no_recursive_manager(tmp_path):
    store = task_store(tmp_path)
    manager = store.reserve("manager", max_children=1)
    store.activate(
        "manager",
        manager.reservation_id,
        workspace_id="manager-workspace",
        session_id="manager-session",
    )
    assert store.reserve("child-1", parent_task_id="manager").should_spawn
    with pytest.raises(TaskLaunchError, match="child limit"):
        store.reserve("child-2", parent_task_id="manager")
    with pytest.raises(TaskLaunchError, match="own parent or replacement"):
        store.reserve("manager", parent_task_id="manager")


def test_spawn_attempt_circuit_breaker_parks_once(tmp_path):
    state = EscalationStore(tmp_path / "state.sqlite3")
    store = TaskLaunchStore(state)
    for _ in range(3):
        reservation = store.reserve("flaky", max_spawn_attempts=3)
        assert reservation.should_spawn
        store.failed("flaky", reservation.reservation_id)

    blocked = store.reserve("flaky", max_spawn_attempts=3)
    assert blocked.should_spawn is False
    assert blocked.task.state == "blocked"
    assert blocked.task.spawn_attempts == 3
    assert len(state.pending()) == 1
    assert state.pending()[0].dedupe_key == "task-spawn-circuit:flaky"


def test_task_limits_are_immutable_across_retries(tmp_path):
    store = task_store(tmp_path)
    store.reserve("stable-task", max_children=2, max_spawn_attempts=4)
    with pytest.raises(TaskLaunchError, match="different immutable limits"):
        store.reserve("stable-task", max_children=3, max_spawn_attempts=4)


def test_active_task_is_resolved_from_manager_session(tmp_path):
    store = task_store(tmp_path)
    reservation = store.reserve("manager", max_children=2)
    store.activate(
        "manager",
        reservation.reservation_id,
        workspace_id="workspace",
        session_id="manager-session",
    )
    assert store.get_by_session("manager-session").task_id == "manager"
    assert store.get_by_session("unknown") is None
