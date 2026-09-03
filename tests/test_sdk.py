from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sightmesh import execution_routing
from sightmesh import sdk as sdk_module
from sightmesh.cdesktop import CdesktopError
from sightmesh.sdk import BatchError, Command, SightMesh, SightMeshError, WorkerSpec
from sightmesh.task_store import StaleTransition, TaskStore, TaskStoreError


class FakeClient:
    def __init__(self, repo_path):
        self.repo_path = repo_path
        self.effects = {}
        self.launches = []
        self.sent = []
        self.stopped = []
        self.lose_response_once = False
        self.repo_rows = None
        self.processes = {}
        self.snapshots = {}

    def info(self):
        return {"service_capabilities": {"managed_task_launch": 1}}

    def repos(self):
        return self.repo_rows or [
            {"id": "repo-1", "name": "project", "path": str(self.repo_path)}
        ]

    def workspace(self, workspace_id):
        container = self.repo_path.parent / "worktrees" / workspace_id
        (container / "project").mkdir(parents=True, exist_ok=True)
        return {"id": workspace_id, "container_ref": str(container)}

    def providers(self):
        return [
            {
                "id": "default-provider",
                "name": "Default",
                "kind": "Default",
                "enabled": True,
            }
        ]

    def register_repo(self, path, **_kwargs):
        assert path.resolve() == self.repo_path.resolve()
        return self.repos()[0]

    def workspace_launch_request(self, **kwargs):
        return {"workspace": kwargs}

    @staticmethod
    def session_launch_request(**kwargs):
        return {"session": kwargs}

    def managed_launch(self, task_id, epoch, launch):
        key = (task_id, epoch)
        self.launches.append((key, launch))
        effect = self.effects.setdefault(
            key,
            {
                "state": "active",
                "workspace_id": f"workspace-{task_id}",
                "session_id": f"session-{task_id}-{epoch}",
                "created": True,
            },
        )
        if self.lose_response_once:
            self.lose_response_once = False
            raise CdesktopError("response lost after native launch")
        return effect

    def send(
        self,
        session_id,
        prompt,
        sender_session=None,
        *,
        dedupe_key=None,
        intent="continue",
    ):
        row = (session_id, prompt, sender_session, dedupe_key, intent)
        self.sent.append(row)
        return {"queued": True}

    def session_commands(self, _session_id):
        return []

    def execution_processes(self, session_id):
        return self.processes.get(session_id, [])

    def normalized_snapshot(self, process_id):
        return self.snapshots[process_id]

    def stop_workspace(self, workspace_id):
        self.stopped.append(workspace_id)


class FakeOwnership:
    def __init__(self):
        self.records = {}

    def get(self, session_id):
        return self.records.get(session_id)

    def assert_deliverable(self, session_id):
        assert session_id not in self.records

    def retire(self, session_id, *, state, reason, logical_key):
        return self.records.setdefault(
            session_id,
            SimpleNamespace(
                session_id=session_id,
                state=state,
                reason=reason,
                logical_key=logical_key,
                successor_session_id=None,
            ),
        )

    def link_successor(self, session_id, successor_session_id):
        record = self.records[session_id]
        record.successor_session_id = successor_session_id
        return record


@pytest.fixture
def system(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    store = TaskStore(tmp_path / "state.sqlite3")
    client = FakeClient(repo)
    ownership = FakeOwnership()
    mesh = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={},
    )
    return mesh, client, store, ownership


def spec(key="audit", **kwargs):
    values = {
        "key": key,
        "prompt": "Audit the boundary",
        "repo": "project",
        "executor": "CODEX",
    }
    values.update(kwargs)
    return WorkerSpec(**values)


def test_start_is_idempotent_for_one_semantic_key(system):
    mesh, client, _store, _ownership = system

    first = mesh.start(spec())
    replayed = mesh.start(spec())

    assert replayed == first
    assert len(client.launches) == 1


def test_repo_name_prefers_the_canonical_registration(system):
    mesh, client, _store, _ownership = system
    client.repo_rows = [
        {"id": "canonical", "name": "project", "path": str(client.repo_path)},
        {
            "id": "managed",
            "name": "project",
            "path": str(
                client.repo_path.parent
                / ".cdesktop-workspaces"
                / "old-worker"
                / "project"
            ),
        },
    ]

    mesh.start(spec())

    request = client.launches[0][1]["request"]["workspace"]
    assert request["repo_path"] == client.repo_path.resolve()


def test_response_loss_retries_the_same_native_effect(system):
    mesh, client, _store, _ownership = system
    client.lose_response_once = True

    with pytest.raises(BatchError, match="response lost"):
        mesh.start(spec())
    recovered = mesh.start(spec())

    assert recovered.state == "active"
    assert len(client.effects) == 1
    assert client.launches[0][0] == client.launches[1][0]


def test_batch_reserves_all_workers_and_duplicate_keys_fail_before_launch(system):
    mesh, client, _store, _ownership = system

    result = mesh.start_all([spec("audit"), spec("tests")])
    assert result.ok
    assert set(result.items) == {"audit", "tests"}

    launches = len(client.launches)
    with pytest.raises(TaskStoreError, match="duplicate task keys"):
        mesh.start_all([spec("same"), spec("same")])
    assert len(client.launches) == launches


def test_command_batch_validates_every_target_before_sending(system):
    mesh, client, _store, _ownership = system
    mesh.start(spec())

    with pytest.raises(SightMeshError, match="Unknown task"):
        mesh.send_all([Command("audit", "first"), Command("missing", "second")])

    assert client.sent == []


def test_command_identity_is_internal_and_allows_intentional_repeats(system):
    mesh, client, _store, _ownership = system
    mesh.start(spec())
    command = Command("audit", "Run it again")

    mesh.send_all([command])
    mesh.send_all([command])
    mesh.send("audit", "Run it again")

    assert client.sent[0][3] == client.sent[1][3]
    assert client.sent[2][3] != client.sent[0][3]


def test_parent_child_limit_is_fixed_when_parent_starts(system):
    mesh, client, store, ownership = system
    parent = mesh.start(spec("manager", children=1))
    child_mesh = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={"CDESKTOP_SESSION_ID": parent.session_id},
    )

    child_mesh.start(spec("first"))
    with pytest.raises(TaskStoreError, match="child limit is 1"):
        child_mesh.start(spec("second"))


def test_replacements_keep_workspace_and_trip_circuit_breaker(system):
    mesh, _client, _store, _ownership = system
    first = mesh.start(spec())

    second = mesh.replace("audit", "Continue after failure one")
    third = mesh.replace("audit", "Continue after failure two")

    assert second.workspace_id == first.workspace_id
    assert third.workspace_id == first.workspace_id
    assert third.attempts == 3
    with pytest.raises(TaskStoreError, match="circuit breaker"):
        mesh.replace("audit", "Do not launch")


def test_checkpoint_content_stays_in_the_task_worktree(system):
    mesh, client, store, _ownership = system
    started = mesh.start(spec())

    checkpointed = mesh.checkpoint("Tests pass; docs remain", worker="audit")
    record = store.get("operator", "audit")
    assert record is not None
    assert checkpointed.checkpoint == record.checkpoint
    assert checkpointed.checkpoint.startswith(".context/sightmesh/checkpoints/")
    root = client.workspace(started.workspace_id)["container_ref"]
    path = Path(root) / "project" / checkpointed.checkpoint
    assert path.read_text() == "Tests pass; docs remain"
    mesh.replace("audit")
    assert (
        client.launches[-1][1]["request"]["session"]["prompt"]
        == "Tests pass; docs remain"
    )


def test_duplicate_failover_wakeups_reserve_one_successor_epoch(system):
    """Two managers observing the same failure must not burn two epochs.

    Both read version N and both try to replace; the version guard is what
    turns the loser into a visible StaleTransition instead of a second
    silent epoch bump that would double-charge the circuit breaker.
    """
    mesh, _client, store, _ownership = system
    mesh.start(spec())
    task = store.get("operator", "audit")
    assert task is not None

    def race(_index):
        try:
            return store.prepare_replacement(task.task_id, expect_version=task.version)
        except StaleTransition as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(race, range(2)))

    winners = [item for item in outcomes if not isinstance(item, StaleTransition)]
    losers = [item for item in outcomes if isinstance(item, StaleTransition)]
    assert len(winners) == 1 and len(losers) == 1
    assert winners[0].epoch == 2
    assert winners[0].attempts == 2
    assert losers[0].current.epoch == 2


def test_quota_failure_moves_once_to_the_next_configured_route(system, monkeypatch):
    mesh, client, store, _ownership = system
    settings = execution_routing.ExecutionRoutingSettings()
    first_target = execution_routing.SelectedTarget(
        "fable", "CLAUDE_CODE", "fable", "subscription", "max-a", "max-a"
    )
    next_target = execution_routing.SelectedTarget(
        "sol", "CODEX", "gpt-5.6-sol", "subscription", "codex-a", "codex-a"
    )
    monkeypatch.setattr(
        execution_routing.ExecutionRoutingStore,
        "load",
        lambda _self: settings,
    )
    monkeypatch.setattr(
        execution_routing,
        "select_route",
        lambda _settings, **_kwargs: execution_routing.SelectionResult(
            "resolved", first_target, (), None
        ),
    )
    exhausted = []

    def reroute(_settings, *, exhausted_binding_id, **_kwargs):
        exhausted.append(exhausted_binding_id)
        return execution_routing.SelectionResult("resolved", next_target, (), None)

    monkeypatch.setattr(sdk_module, "reroute_after_quota_exhaustion", reroute)
    started = mesh.start(spec(executor=None))
    client.processes[started.session_id] = [
        {"id": "failed-fable", "run_reason": "codingagent", "status": "failed"}
    ]
    client.snapshots["failed-fable"] = {
        "entries": [
            {
                "content": {
                    "entry_type": {"type": "assistant_message"},
                    "content": "You've reached your Fable 5 limit.",
                }
            }
        ]
    }

    client.lose_response_once = True
    with pytest.raises(CdesktopError, match="response lost"):
        mesh.reconcile_quota_failure(started.session_id)
    recovered = mesh.reconcile_quota_failure(started.session_id)
    assert recovered is not None
    assert recovered.workspace_id == started.workspace_id
    assert recovered.session_id != started.session_id
    assert recovered.attempts == 2
    assert exhausted == ["max-a"]
    assert len(client.effects) == 2
    assert mesh.reconcile_quota_failure(started.session_id) is None

    replacement = client.launches[-1][1]["request"]["session"]
    assert replacement["executor"] == "CODEX"
    assert replacement["model"] == "gpt-5.6-sol"
    assert replacement["provider_id"] == "default-provider"
    assert replacement["prompt"] == "Audit the boundary"
    record = store.get("operator", "audit")
    assert record is not None
    assert record.spec["target"]["route_id"] == "sol"


def test_non_quota_failure_does_not_replace_a_managed_task(system):
    mesh, client, _store, _ownership = system
    started = mesh.start(spec())
    client.processes[started.session_id] = [
        {"id": "failed-build", "run_reason": "codingagent", "status": "failed"}
    ]
    client.snapshots["failed-build"] = {
        "entries": [
            {
                "content": {
                    "entry_type": {"type": "assistant_message"},
                    "content": "Compilation failed with a type error.",
                }
            }
        ]
    }

    assert mesh.reconcile_quota_failure(started.session_id) is None
    assert len(client.launches) == 1


def test_newer_success_ignores_an_older_quota_failure(system):
    mesh, client, _store, _ownership = system
    started = mesh.start(spec())
    client.processes[started.session_id] = [
        {
            "id": "new-success",
            "run_reason": "codingagent",
            "status": "completed",
            "completed_at": "2026-08-31T02:00:00Z",
        },
        {
            "id": "old-quota",
            "run_reason": "codingagent",
            "status": "failed",
            "completed_at": "2026-08-31T01:00:00Z",
        },
    ]

    assert mesh.reconcile_quota_failure(started.session_id) is None
    assert len(client.launches) == 1


def test_exhausted_route_chain_blocks_without_spawning(system, monkeypatch):
    mesh, client, store, _ownership = system
    target = execution_routing.SelectedTarget(
        "fable", "CLAUDE_CODE", "fable", "subscription", "max-a", "max-a"
    )
    monkeypatch.setattr(
        execution_routing,
        "select_route",
        lambda _settings, **_kwargs: execution_routing.SelectionResult(
            "resolved", target, (), None
        ),
    )
    monkeypatch.setattr(
        execution_routing.ExecutionRoutingStore,
        "load",
        lambda _self: execution_routing.ExecutionRoutingSettings(),
    )
    monkeypatch.setattr(
        sdk_module,
        "reroute_after_quota_exhaustion",
        lambda *_args, **_kwargs: execution_routing.SelectionResult(
            "blocked", None, (), "routes_exhausted"
        ),
    )
    started = mesh.start(spec(executor=None))
    client.processes[started.session_id] = [
        {"id": "failed-fable", "run_reason": "codingagent", "status": "failed"}
    ]
    client.snapshots["failed-fable"] = {
        "entries": [
            {
                "content": {
                    "entry_type": {"type": "assistant_message"},
                    "content": "You've reached your Fable 5 limit.",
                }
            }
        ]
    }

    blocked = mesh.reconcile_quota_failure(started.session_id)

    assert blocked is not None and blocked.state == "blocked"
    assert len(client.launches) == 1
    record = store.get("operator", "audit")
    assert record is not None and record.result is not None
    assert "routes_exhausted" in record.result


def test_routed_start_requires_one_enabled_default_provider(system, monkeypatch):
    mesh, client, _store, _ownership = system
    client.providers = list
    target = execution_routing.SelectedTarget(
        "fable", "CLAUDE_CODE", "fable", "subscription", "max-a", "max-a"
    )
    monkeypatch.setattr(
        execution_routing,
        "select_route",
        lambda _settings, **_kwargs: execution_routing.SelectionResult(
            "resolved", target, (), None
        ),
    )

    with pytest.raises(SightMeshError, match="one enabled cdesktop Default provider"):
        mesh.start(spec(executor=None))


def test_completion_notifies_only_the_recorded_parent(system):
    mesh, client, store, ownership = system
    parent = mesh.start(spec("manager", children=1))
    child_mesh = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={"CDESKTOP_SESSION_ID": parent.session_id},
    )
    child = child_mesh.start(spec("child"))
    client.sent.clear()

    running_child = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={"CDESKTOP_SESSION_ID": child.session_id},
    )
    running_child.complete("all checks pass")

    assert len(client.sent) == 1
    assert client.sent[0][0] == parent.session_id
    assert client.sent[0][0] != child.session_id


def test_a_duplicate_completion_is_an_idempotent_no_op(system):
    """A worker that repeats `complete` after a lost response, or a manager
    that completes a child the reconciler already closed, must not see an
    error and must not send the parent a second wake."""
    mesh, client, store, ownership = system
    parent = mesh.start(spec("manager", children=1))
    child_mesh = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={"CDESKTOP_SESSION_ID": parent.session_id},
    )
    child = child_mesh.start(spec("child"))
    client.sent.clear()
    running_child = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={"CDESKTOP_SESSION_ID": child.session_id},
    )

    first = running_child.complete("all checks pass")
    replayed = running_child.complete("all checks pass")

    assert replayed == first
    assert len(client.sent) == 1


def test_a_late_completion_on_a_terminal_task_is_a_visible_error(system):
    """Idempotence is only for an identical target. A `complete` arriving
    after the task was cancelled changes the outcome and must be refused."""
    mesh, _client, _store, _ownership = system
    mesh.start(spec())
    mesh.cancel("audit")

    with pytest.raises(StaleTransition, match="cancelled"):
        mesh.complete("too late", worker="audit")


def test_a_blocked_child_never_replaces_the_parent_turn(system):
    """The old per-child mail sent intent='replace' whenever a child blocked,
    interrupting the manager mid-cohort. Every wake now continues the turn."""
    mesh, client, store, ownership = system
    parent = mesh.start(spec("manager", children=1))
    child_mesh = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={"CDESKTOP_SESSION_ID": parent.session_id},
    )
    child = child_mesh.start(spec("child"))
    client.sent.clear()

    SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        environment={"CDESKTOP_SESSION_ID": child.session_id},
    ).blocked("needs a decision")

    assert [row[4] for row in client.sent] == ["continue"]


def test_a_crash_between_launch_and_activation_adopts_the_same_session(system):
    """The journal row is written before the native call, so a retry after a
    crash at activation must reuse the recorded workspace and session rather
    than forking a second native run for the same epoch."""
    mesh, client, store, _ownership = system
    activate = store.activate

    def crash(*_args, **_kwargs):
        raise RuntimeError("the process died before activation")

    store.activate = crash
    with pytest.raises(RuntimeError):
        mesh.start(spec())
    store.activate = activate

    started = mesh.start(spec())

    assert len(client.launches) == 1
    effect = mesh.journal.get(store.get("operator", "audit").task_id, 1)
    assert (effect.workspace_id, effect.session_id) == (
        started.workspace_id,
        started.session_id,
    )


def test_a_hundred_concurrent_starts_launch_once(system):
    """Every manager fanning out re-runs `start` for its whole cohort; the
    journal is what stops that from creating a session per caller."""
    mesh, client, _store, _ownership = system

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: mesh.start_all([spec()]), range(100)))

    assert len(client.effects) == 1
    assert {key for result in results for key in result.items} <= {"audit"}


def test_the_contract_probe_reports_an_advertised_only_runtime(system):
    """cdesktop 0.2.7 has no lookup to probe, and an unprobed capability must
    say so rather than pass as if it had been executed."""
    mesh, _client, _store, _ownership = system

    assert mesh._require_contract() == "advertised-only"


def test_the_contract_probe_executes_the_managed_launch_lookup(system):
    """When the lookup exists, a well-formed not-found answer is the proof
    the seam is actually wired, not merely advertised."""
    mesh, client, _store, _ownership = system
    looked_up = []

    def managed_effect(task_id, epoch):
        looked_up.append((task_id, epoch))
        return {"state": "missing"}

    client.managed_effect = managed_effect

    assert mesh._require_contract() == "lookup"
    assert looked_up == [(sdk_module.CONTRACT_PROBE_TASK_ID, 1)]


def test_a_probe_that_finds_the_sentinel_reserved_fails_the_contract(system):
    """A runtime answering the sentinel with a live effect is not speaking
    this contract, and starting work against it would corrupt a real task."""
    mesh, client, _store, _ownership = system
    client.managed_effect = lambda _task_id, _epoch: {"state": "active"}

    with pytest.raises(SightMeshError, match="reserved sentinel effect"):
        mesh.start(spec())


def test_start_waits_for_capacity_then_refuses_with_a_typed_error(monkeypatch, tmp_path):
    """#88: direct launches bypass the executor's queued-dispatch cap, so an
    unbounded fan-out starves every queued wake/resume. The kernel now admits
    launches against its own managed-concurrency cap and refuses loudly (not
    silently stampeding) when it stays full."""
    from sightmesh import sdk as sdk_mod
    from sightmesh.task_store import TaskRecord

    class Store:
        def count_running(self):
            return 4

    mesh = sdk_mod.SightMesh.__new__(sdk_mod.SightMesh)
    mesh.store = Store()
    monkeypatch.setenv("SIGHTMESH_MAX_ACTIVE_WORKERS", "4")
    monkeypatch.setenv("SIGHTMESH_LAUNCH_WAIT_SECONDS", "0")
    monkeypatch.setattr(sdk_mod.time, "sleep", lambda *_: None)
    task = TaskRecord(
        task_id="t", scope="operator", key="w", parent_task_id=None, state="reserved",
        epoch=1, attempts=1, max_attempts=3, child_limit=0, spec={}, workspace_id=None,
        holder_session_id=None, checkpoint=None, result=None, created_at=0.0, updated_at=0.0,
        **{f: 0 for f in TaskRecord.__dataclass_fields__ if f not in {
            "task_id","scope","key","parent_task_id","state","epoch","attempts","max_attempts",
            "child_limit","spec","workspace_id","holder_session_id","checkpoint","result",
            "created_at","updated_at"}},
    )
    with pytest.raises(sdk_mod.SightMeshError, match="capacity"):
        mesh._wait_for_launch_capacity(task)


def test_tasks_launch_unattended_by_default(system):
    # Workers used to start with ACCEPT_EDITS, so every shell call parked on a
    # human approval and the whole mesh stalled the moment nobody was watching.
    # Kernel tasks always run in their own worktree, so unattended is the safe
    # default and the only one that never waits on a person.
    mesh, client, _store, _ownership = system

    mesh.start(spec())

    (_key, launch), = client.launches
    workspace = launch["request"]["workspace"]
    assert workspace["permission_policy"] == "BYPASS_PERMISSIONS"
    assert workspace["use_worktree"] is True


def test_unknown_permission_policy_is_rejected(system):
    mesh, _client, _store, _ownership = system

    with pytest.raises(SightMeshError, match="Unknown permission policy"):
        mesh.start(spec(permission="YOLO"))
