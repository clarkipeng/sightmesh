from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sightmesh.cdesktop import CdesktopError
from sightmesh.sdk import BatchError, Command, SightMesh, SightMeshError, WorkerSpec
from sightmesh.task_store import TaskStore, TaskStoreError


class FakeClient:
    def __init__(self, repo_path):
        self.repo_path = repo_path
        self.effects = {}
        self.launches = []
        self.sent = []
        self.stopped = []
        self.lose_response_once = False

    def info(self):
        return {"service_capabilities": {"managed_task_launch": 1}}

    def repos(self):
        return [{"id": "repo-1", "name": "project", "path": str(self.repo_path)}]

    def workspace(self, workspace_id):
        container = self.repo_path.parent / "worktrees" / workspace_id
        (container / "project").mkdir(parents=True, exist_ok=True)
        return {"id": workspace_id, "container_ref": str(container)}

    def providers(self):
        return []

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
    mesh, _client, store, _ownership = system
    mesh.start(spec())
    task = store.get("operator", "audit")
    assert task is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        replacements = list(
            pool.map(lambda _: store.prepare_replacement(task.task_id), range(2))
        )

    assert {replacement.epoch for replacement in replacements} == {2}
    assert {replacement.attempts for replacement in replacements} == {2}


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
