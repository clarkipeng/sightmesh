from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sightmesh import execution_routing
from sightmesh import sdk as sdk_module
from sightmesh.cdesktop import CdesktopError, CdesktopRejectedError
from sightmesh.sdk import BatchError, Command, SightMesh, SightMeshError, WorkerSpec
from sightmesh.profiles import Profile
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
        self.reject_launch = None

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
        rejection = self.reject_launch
        if rejection is not None:
            self.reject_launch = None
            raise rejection
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

    def managed_effect(self, task_id, epoch):
        """Mirror the real seam so the launch-contract probe can execute."""
        effect = self.effects.get((task_id, epoch))
        if effect is None:
            raise CdesktopError(
                f"GET managed effect {task_id}/{epoch} failed: HTTP 404: not found"
            )
        return effect


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
def system(tmp_path, monkeypatch):
    repo = tmp_path / "project"
    repo.mkdir()
    settings = execution_routing.ExecutionRoutingSettings(
        chains=(
            execution_routing.RouteChain(
                "standard",
                (execution_routing.Route("test", "CODEX", "test", "free"),),
            ),
        )
    )
    monkeypatch.setattr(
        execution_routing.ExecutionRoutingStore, "load", lambda _self: settings
    )
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


def _routed(monkeypatch, first, following=None, *, settings=None):
    """Pin routing to a fixed first hop and a fixed reroute answer.

    Selection over a real pool is exercised in tests/test_execution_routing.py
    and the simulator; here the point is what the *task lifecycle* does with a
    typed outcome, so the selector is held still.
    """
    settings = settings or execution_routing.ExecutionRoutingSettings()
    monkeypatch.setattr(
        execution_routing.ExecutionRoutingStore, "load", lambda _self: settings
    )
    monkeypatch.setattr(
        execution_routing,
        "validate_chain",
        lambda _settings, route_class=None: execution_routing.ValidationResult(
            route_class or "standard", True, None
        ),
    )
    monkeypatch.setattr(
        execution_routing,
        "select_route",
        lambda _settings, **_kwargs: execution_routing.SelectionResult(
            "resolved", first, (), None
        ),
    )
    calls = []

    def advance(_settings, **kwargs):
        calls.append(kwargs)
        return following

    if following is not None:
        monkeypatch.setattr(sdk_module, "advance_route_after_outcome", advance)
    return calls


FIRST_TARGET = execution_routing.SelectedTarget(
    "standard", "terra", "CLAUDE_CODE", "terra", "subscription", "max-a", "max-a"
)
NEXT_TARGET = execution_routing.SelectedTarget(
    "standard", "sol", "CODEX", "gpt-5.6-sol", "subscription", "codex-a", "codex-a"
)


def _mark_outcome(mesh, store, outcome, retry_at=None):
    task = store.get("operator", "audit")
    mesh.journal.mark_terminal(task.task_id, task.epoch, outcome, retry_at)
    return task


def test_a_typed_rate_limit_moves_once_to_the_next_hop(system, monkeypatch):
    """The reroute trigger is the typed outcome on the effect journal and
    nothing else. This pins the whole transition: the failed binding is the one
    handed to the cooler, exactly one new epoch opens, the workspace survives,
    and a second reconcile with no new outcome is a no-op rather than a second
    replacement."""
    mesh, client, store, _ownership = system
    calls = _routed(monkeypatch, FIRST_TARGET, following=execution_routing.SelectionResult(
        "resolved", NEXT_TARGET, (), None
    ))
    started = mesh.start(spec(executor=None))
    _mark_outcome(mesh, store, "rate_limited", retry_at=1893456000.0)

    client.lose_response_once = True
    with pytest.raises(CdesktopError, match="response lost"):
        mesh.reconcile_provider_outcome(started.session_id)
    recovered = mesh.reconcile_provider_outcome(started.session_id)

    assert recovered is not None
    assert recovered.workspace_id == started.workspace_id
    assert recovered.session_id != started.session_id
    assert recovered.attempts == 2
    # One advance, not two: the retry resumed the epoch already prepared
    # (recovery is recorded on the target) instead of burning a second one.
    assert [
        (call["outcome"], call["failed_binding_id"], call["route_class"])
        for call in calls
    ] == [("rate_limited", "max-a", "standard")]
    assert len(client.effects) == 2
    assert mesh.reconcile_provider_outcome(started.session_id) is None

    replacement = client.launches[-1][1]["request"]["session"]
    assert replacement["executor"] == "CODEX"
    assert replacement["model"] == "gpt-5.6-sol"
    assert replacement["provider_id"] == "default-provider"
    assert replacement["prompt"] == "Audit the boundary"
    record = store.get("operator", "audit")
    assert record is not None
    assert record.spec["target"]["route_id"] == "sol"
    assert record.spec["target"]["route_class"] == "standard"


def test_a_code_failure_blocks_visibly_and_never_reroutes(system):
    """The regression this exists for: the old path scraped the transcript, so
    a worker whose failing test output merely mentioned a rate limit was
    rerouted onto a fresh account. A code or test failure carries no typed
    provider outcome, so it cannot reach the reroute path by construction.

    It must not vanish either. A failed worker process that reports no provider
    signal blocks the task with its typed reason, which wakes the manager -
    the alternative is a task that is neither running nor finished and that
    nobody is told about."""
    mesh, client, store, _ownership = system
    started = mesh.start(spec())
    client.processes[started.session_id] = [
        {"id": "failed-build", "run_reason": "codingagent", "status": "failed"}
    ]
    client.snapshots["failed-build"] = {
        "entries": [
            {
                "content": {
                    "entry_type": {"type": "assistant_message"},
                    "content": "2 tests failed: expected HTTP 429 Too Many Requests",
                }
            }
        ]
    }

    settled = mesh.reconcile_provider_outcome(started.session_id)

    assert settled is not None and settled.state == "blocked"
    assert "worker process failed" in str(store.get("operator", "audit").result)
    # Blocked, not rerouted: no second epoch and no second launch.
    assert mesh.reconcile_provider_outcomes() == []
    assert store.get("operator", "audit").epoch == 1
    assert len(client.launches) == 1


def test_a_live_epoch_is_never_rerouted_before_it_ends(system, monkeypatch):
    """Only a *terminal* effect advances a chain. A launched epoch still owns a
    running session, so rerouting it would fork the work rather than replace
    it."""
    mesh, client, store, _ownership = system
    _routed(monkeypatch, FIRST_TARGET)
    started = mesh.start(spec(executor=None))
    task = store.get("operator", "audit")

    assert mesh.journal.get(task.task_id, task.epoch).state == "launched"
    assert mesh.reconcile_provider_outcome(started.session_id) is None
    assert mesh.reconcile_provider_outcomes() == []
    assert len(client.launches) == 1


def test_an_explicit_profile_stays_recoverable_when_failover_is_on(
    system, monkeypatch, tmp_path
):
    """Contract: explicit profile overrides remain recoverable. The old code
    recorded no route identity for them, so `reconcile` returned None and the
    task sat on an exhausted account forever."""
    mesh, client, store, _ownership = system
    monkeypatch.setattr(
        sdk_module.ProfileStore,
        "get",
        lambda _self, _name: Profile(
            name="pinned",
            executor="CODEX",
            provider_id="default-provider",
            automatic_failover=True,
        ),
    )
    calls = _routed(monkeypatch, FIRST_TARGET, following=execution_routing.SelectionResult(
        "resolved", NEXT_TARGET, (), None
    ))
    started = mesh.start(spec(profile="pinned", executor=None))
    assert store.get("operator", "audit").spec["target"]["route_id"] == "profile:pinned"
    _mark_outcome(mesh, store, "rate_limited")

    recovered = mesh.reconcile_provider_outcome(started.session_id)

    assert recovered is not None and recovered.state == "active"
    assert calls[0]["failed_binding_id"] is None
    assert store.get("operator", "audit").spec["target"]["route_id"] == "sol"


def test_a_profile_with_failover_off_blocks_with_a_reason(system, monkeypatch):
    """`automatic_failover` is the operator saying "this task runs here or not
    at all". Honouring it must still leave a visible reason: silently returning
    None is what made the old bug invisible."""
    mesh, _client, store, _ownership = system
    monkeypatch.setattr(
        sdk_module.ProfileStore,
        "get",
        lambda _self, _name: Profile(
            name="pinned",
            executor="CODEX",
            provider_id="default-provider",
            automatic_failover=False,
        ),
    )
    _routed(monkeypatch, FIRST_TARGET)
    started = mesh.start(spec(profile="pinned", executor=None))
    _mark_outcome(mesh, store, "auth")

    blocked = mesh.reconcile_provider_outcome(started.session_id)

    assert blocked is not None and blocked.state == "blocked"
    assert "automatic failover is off" in str(store.get("operator", "audit").result)


def test_exhausted_route_chain_blocks_without_spawning(system, monkeypatch):
    mesh, client, store, _ownership = system
    _routed(monkeypatch, FIRST_TARGET, following=execution_routing.SelectionResult(
        "blocked", None, (), "routes_exhausted"
    ))
    started = mesh.start(spec(executor=None))
    _mark_outcome(mesh, store, "provider_down")

    blocked = mesh.reconcile_provider_outcome(started.session_id)

    assert blocked is not None and blocked.state == "blocked"
    assert len(client.launches) == 1
    record = store.get("operator", "audit")
    assert record is not None and record.result is not None
    assert "routes_exhausted" in record.result


def test_dispatch_fails_closed_when_the_class_chain_has_no_usable_hop(
    system, monkeypatch
):
    """`validate_chain` is an explicit pre-dispatch gate, not a side effect of
    selection: an unusable class must be refused before any epoch, effect row,
    or native call exists."""
    mesh, client, store, _ownership = system
    monkeypatch.setattr(
        execution_routing.ExecutionRoutingStore,
        "load",
        lambda _self: execution_routing.ExecutionRoutingSettings(),
    )

    with pytest.raises(SightMeshError, match="standard"):
        mesh.start(spec(executor=None))

    assert client.launches == []
    assert store.get("operator", "audit") is None


def _free_route(route_id):
    """A hop that needs no pool account, so routing is exercised for real.

    Selection resolves a free route without ever loading pool state, which lets
    these tests run the *real* `class_for` -> `validate_chain` -> `select_route`
    path instead of monkeypatching the gate they exist to check.
    """
    return execution_routing.Route(
        id=route_id, executor="CODEX", model=route_id, billing_class="free"
    )


def _configure_chains(monkeypatch, **chains):
    settings = execution_routing.ExecutionRoutingSettings(
        chains=tuple(
            execution_routing.RouteChain(route_class, routes)
            for route_class, routes in chains.items()
        )
    )
    monkeypatch.setattr(
        execution_routing.ExecutionRoutingStore, "load", lambda _self: settings
    )
    return settings


def test_a_fanning_out_top_level_manager_takes_the_deep_class(system, monkeypatch):
    """Scope and risk pick the class once, at dispatch. A top-level supervised
    task that fans work out is the one shape where weak judgement multiplies
    across children, so it gets the deep chain; everything else stays
    standard.

    Runs the real validate/select path rather than stubbing it: stubbing the
    gate is what let the promotion ship against an install where the promoted
    class had no chain at all."""
    mesh, _client, store, _ownership = system
    _configure_chains(
        monkeypatch, standard=(_free_route("terra"),), deep=(_free_route("fable"),)
    )

    mesh.start(spec(key="manager", executor=None, children=4))
    mesh.start(spec(key="worker", executor=None, children=0))

    assert store.get("operator", "manager").spec["target"]["route_class"] == "deep"
    assert store.get("operator", "manager").spec["target"]["route_id"] == "fable"
    assert store.get("operator", "worker").spec["target"]["route_class"] == "standard"
    assert store.get("operator", "worker").spec["target"]["route_id"] == "terra"


def test_a_promotion_onto_an_unconfigured_class_falls_back_to_the_default(
    system, monkeypatch, caplog
):
    """Regression guard for the upgrade that broke every existing install: the
    v1->v2 migration fills only `standard`, `class_for` promotes any fanning-out
    top-level manager to `deep`, and the fail-closed gate then refused a start
    that had worked the day before.

    Promotion is this module's judgement, not the operator's instruction, so a
    promoted class with no chain must degrade visibly onto the default rather
    than refuse the work."""
    mesh, _client, store, _ownership = system
    _configure_chains(monkeypatch, standard=(_free_route("terra"),))

    with caplog.at_level("WARNING", logger="sightmesh.sdk"):
        mesh.start(spec(key="manager", executor=None, children=4))

    target = store.get("operator", "manager").spec["target"]
    assert (target["route_class"], target["route_id"]) == ("standard", "terra")
    assert "deep" in caplog.text and "manager" in caplog.text


@pytest.mark.parametrize("override", ("executor", "profile"))
def test_an_explicit_class_with_no_chain_is_refused_before_an_override_can_start(
    system, monkeypatch, override
):
    """Falling open is only ever right for a promotion. An operator who names
    `deep` asked for that chain specifically, so silently running the work on
    `standard` would answer a different question than the one they asked."""
    mesh, client, store, _ownership = system
    _configure_chains(monkeypatch, standard=(_free_route("terra"),))
    if override == "profile":
        monkeypatch.setattr(
            sdk_module.ProfileStore,
            "get",
            lambda _self, _name: Profile(
                name="selected",
                executor="CODEX",
                provider_id="default-provider",
            ),
        )

    with pytest.raises(SightMeshError, match="deep"):
        mesh.start(
            spec(
                key="deepwork",
                executor="CODEX" if override == "executor" else None,
                profile="selected" if override == "profile" else None,
                route_class="deep",
            )
        )

    assert store.get("operator", "deepwork") is None
    assert client.launches == []


def test_routed_start_requires_one_enabled_default_provider(system, monkeypatch):
    mesh, client, _store, _ownership = system
    client.providers = list
    target = execution_routing.SelectedTarget(
        "standard", "fable", "CLAUDE_CODE", "fable", "subscription", "max-a", "max-a"
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


def test_the_contract_probe_fails_closed_without_an_executable_lookup(system):
    """An advertised capability with no lookup to probe used to pass as
    `advertised-only`, which let a launch path fail late against a runtime
    that never actually implemented it. There is no take-its-word answer:
    either the probe runs or the launch does not."""
    mesh, client, _store, _ownership = system
    client.managed_effect = None

    with pytest.raises(SightMeshError, match="no effect lookup"):
        mesh._require_contract()


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


def test_a_rejected_launch_records_its_outcome_and_cools_the_binding_once(
    system, monkeypatch
):
    """The cooldown belongs with the write that records the outcome, not with
    the reroute that later reads it.

    A terminal effect stays readable forever, so a reconcile that cooled on
    every read would push an account's cooldown further out on every tick and
    never let it come back.
    """
    mesh, client, store, _ownership = system
    _routed(monkeypatch, FIRST_TARGET)
    cooled = []
    monkeypatch.setattr(
        sdk_module,
        "cool_provider_outcome",
        lambda _settings, **kwargs: cooled.append(kwargs) or ("max-a",),
    )
    client.reject_launch = CdesktopRejectedError("nope", status=429, retry_at=1234.0)

    with pytest.raises(BatchError):
        mesh.start(spec(executor=None))

    task = store.get("operator", "audit")
    assert task.state == "blocked"
    assert mesh.journal.get(task.task_id, 1).outcome == "rate_limited"
    assert [(c["outcome"], c["binding_id"], c["retry_at"]) for c in cooled] == [
        ("rate_limited", "max-a", 1234.0)
    ]

    # Reading the same outcome again does not cool again.
    mesh.reconcile_provider_outcomes()
    assert len(cooled) == 1


def test_a_definitive_rejection_blocks_instead_of_rerouting(system, monkeypatch):
    """A 409 means the request itself was refused; retrying it on another
    account would fail identically. Only capacity, auth, and a provider being
    down name a different account as the fix."""
    mesh, client, store, _ownership = system
    _routed(monkeypatch, FIRST_TARGET)
    client.reject_launch = CdesktopRejectedError("conflict", status=409)

    with pytest.raises(BatchError):
        mesh.start(spec(executor=None))

    task = store.get("operator", "audit")
    assert mesh.journal.get(task.task_id, 1).outcome == "rejected:409"
    assert mesh.reconcile_provider_outcomes() == []
    assert store.get("operator", "audit").epoch == 1


def test_an_upgrade_that_adds_a_spec_field_is_not_a_changed_specification(system):
    """A manager re-runs `start` for its whole cohort, so a task reserved by an
    earlier version is re-described by a newer one on the very next tick.

    A field that version could not have recorded describes no disagreement
    about the work; treating its absence as one would make every in-flight task
    unreachable after an upgrade.
    """
    mesh, _client, store, _ownership = system
    started = mesh.start(spec())
    task = store.get("operator", "audit")
    legacy = {k: v for k, v in task.spec.items() if k != "route_class"}
    with store.connect() as conn:
        conn.execute(
            "UPDATE managed_tasks SET spec_json = ? WHERE task_id = ?",
            (json.dumps(legacy, sort_keys=True, separators=(",", ":")), task.task_id),
        )

    replayed = mesh.start(spec())

    assert replayed.session_id == started.session_id
