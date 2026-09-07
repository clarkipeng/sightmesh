"""The real SDK/store/retention/client, with only native HTTP and process I/O faked."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from sightmesh import sdk as sdk_module
from sightmesh import sqlite_durability
from sightmesh.evidence import EvidenceClient, EvidenceUnavailable
from sightmesh.sdk import CheckpointPending, SightMesh, SightMeshError
from sightmesh.task_store import StaleTransition, TaskStoreError
from test_sdk import spec, system


class Response(io.BytesIO):
    headers = {}


class NativeHttp:
    def __init__(self):
        self.records, self.calls, self.responses = {}, [], []
        self.lose_once = False
        self.sabotage = lambda receipt: receipt
        self.on_publish = lambda: None
        self.download = lambda body: body

    def __call__(self, request):
        parsed, query = (
            urlsplit(request.full_url),
            parse_qs(urlsplit(request.full_url).query),
        )
        if parsed.path == "/api/info":
            data = {"service_capabilities": {"execution_evidence": 1}}
        elif parsed.path.endswith("/artifacts") and request.method == "POST":
            payload = (
                request.data
                if isinstance(request.data, bytes)
                else b"".join(request.data)
            )
            mime = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {request.get_header('Content-type')}\r\nMIME-Version: 1.0\r\n\r\n".encode()
                + payload
            )
            parts = list(mime.iter_parts())
            assert len(parts) == 1
            body, name = parts[0].get_payload(decode=True), parts[0].get_filename()
            key, execution_id = query["publication_key"][0], parsed.path.split("/")[-2]
            identity = (execution_id, key)
            self.calls.append(identity)
            receipt = {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(identity))),
                "execution_id": execution_id,
                "publication_key": key,
                "producer_ref": query["producer_ref"][0],
                "original_path": query["original_path"][0],
                "original_name": name,
                "durability": "confirmed",
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
                "attachment_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(body).hexdigest())
                ),
                "captured_at": "2026-09-07T00:00:00Z",
            }
            if identity in self.records:
                assert self.records[identity] == (receipt, body)
            self.records[identity] = (receipt, body)
            self.on_publish()
            if self.lose_once:
                self.lose_once = False
                raise OSError("response lost after publication")
            data = self.sabotage(copy.deepcopy(receipt))
        else:
            parts = parsed.path.split("/")
            occurrence = parts[-2] if parts[-1] == "file" else parts[-1]
            execution = parts[3]
            receipt, body = next(
                (r, b)
                for (e, _), (r, b) in self.records.items()
                if e == execution and r["id"] == occurrence
            )
            if parts[-1] == "file":
                response = Response(self.download(body))
                self.responses.append(response)
                return response
            data = self.sabotage(copy.deepcopy(receipt))
        response = Response(json.dumps({"success": True, "data": data}).encode())
        self.responses.append(response)
        return response


def native_system(system):
    _, client, store, ownership = system
    native = NativeHttp()
    original = client.managed_launch

    def launch(task_id, epoch, request):
        effect = original(task_id, epoch, request)
        session = effect["session_id"]
        client.processes[session] = [
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, session)),
                "session_id": session,
                "run_reason": "codingagent",
                "status": "running",
                "created_at": "2026-09-07T00:00:00Z",
            }
        ]
        return effect

    client.managed_launch = launch
    mesh = SightMesh(
        client=client,
        store=store,
        ownership=ownership,
        evidence_client=EvidenceClient("http://native.invalid", opener=native),
        environment={},
    )
    return mesh, client, store, native


def task_for(store, worker):
    return store.get("operator", worker.key)


def operation_for(store, worker):
    return store.checkpoint_operation(
        task_for(store, worker).task_id, worker.checkpoint
    )


def test_sdk_native_checkpoint_recovers_after_worktree_disappears_and_keeps_equal_operations_distinct(
    system,
):
    mesh, client, store, native = native_system(system)
    started = mesh.start(spec())
    first = mesh.checkpoint("same bytes", worker="audit")
    op = operation_for(store, first)
    Path(op.facts["original_path"]).unlink()
    original_workspace = client.workspace
    client.workspace = lambda _: {"id": started.workspace_id, "container_ref": None}
    assert (
        mesh._read_checkpoint(store.get_by_id(task_for(store, first).task_id))
        == "same bytes"
    )
    client.workspace = original_workspace
    mesh.replace("audit")
    assert client.launches[-1][1]["request"]["session"]["prompt"] == "same bytes"
    second = mesh.checkpoint("same bytes", worker="audit")
    assert second.checkpoint != first.checkpoint
    assert operation_for(store, second).receipt["id"] != op.receipt["id"]
    assert (
        operation_for(store, second).receipt["attachment_id"]
        == op.receipt["attachment_id"]
    )
    assert all(response.closed for response in native.responses)


def test_lost_publication_response_retries_explicit_id_on_pinned_execution(system):
    mesh, client, store, native = native_system(system)
    started = mesh.start(spec())
    native.lose_once = True
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("retry me", worker="audit")
    saved = store.pending_checkpoint_operations(task_for(store, started).task_id)
    assert len(saved) == 1 and saved[0].operation_id == pending.value.operation_id
    original_execution = saved[0].facts["execution_id"]
    client.processes[started.session_id][0]["id"] = str(uuid.uuid4())
    done = mesh.checkpoint(
        "retry me", worker="audit", operation_id=pending.value.operation_id
    )
    op = operation_for(store, done)
    assert op.durable_commit and op.facts["execution_id"] == original_execution
    assert native.calls[0] == native.calls[1]
    version = store.get_by_id(task_for(store, started).task_id).version
    mesh.checkpoint("retry me", worker="audit", operation_id=op.operation_id)
    assert store.get_by_id(task_for(store, started).task_id).version == version
    assert len(native.calls) == 2  # committed retry verifies its receipt with GET


def test_sqlite_reference_failure_rolls_back_checkpoint_and_retry_reuses_native_occurrence(
    system,
):
    mesh, _, store, native = native_system(system)
    started = mesh.start(spec())
    with store.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_reference BEFORE UPDATE OF receipt ON task_checkpoint_operations BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("durable reference", worker="audit")
    assert store.get_by_id(task_for(store, started).task_id).checkpoint is None
    assert (
        store.pending_checkpoint_operations(task_for(store, started).task_id)[0].receipt
        is None
    )
    with store.connect() as conn:
        conn.execute("DROP TRIGGER fail_reference")
    done = mesh.checkpoint(
        "durable reference", worker="audit", operation_id=pending.value.operation_id
    )
    assert (
        operation_for(store, done).durable_commit and native.calls[0] == native.calls[1]
    )


def test_post_commit_barrier_failure_never_acknowledges_but_is_recoverable(
    system, monkeypatch
):
    mesh, _, store, native = native_system(system)
    started = mesh.start(spec())
    confirm = sqlite_durability.confirm_database

    def fail_after_reference(conn, path):
        if conn.execute(
            "SELECT 1 FROM task_checkpoint_operations WHERE receipt IS NOT NULL"
        ).fetchone():
            raise OSError("ancestor barrier failed")
        confirm(conn, path)

    monkeypatch.setattr(sqlite_durability, "confirm_database", fail_after_reference)
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("committed, not acknowledged", worker="audit")
    saved = store.checkpoint_operation_by_id(pending.value.operation_id)
    assert saved.receipt is not None and Path(saved.facts["original_path"]).exists()
    monkeypatch.setattr(sqlite_durability, "confirm_database", confirm)
    mesh.checkpoint(
        "committed, not acknowledged", worker="audit", operation_id=saved.operation_id
    )
    assert len(native.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_name", "wrong.md"),
        ("original_path", "/wrong"),
        ("producer_ref", "wrong:1"),
        ("execution_id", str(uuid.UUID(int=1))),
        ("publication_key", "wrong"),
        ("size_bytes", True),
        ("durability", "unverified"),
        ("id", "not-native-id"),
    ],
)
def test_all_native_receipt_facts_are_required_before_task_acknowledgement(
    system, field, value
):
    mesh, _, store, native = native_system(system)
    started = mesh.start(spec())
    native.sabotage = lambda receipt: {**receipt, field: value}
    with pytest.raises(CheckpointPending):
        mesh.checkpoint("verify all facts", worker="audit")
    assert store.get_by_id(task_for(store, started).task_id).checkpoint is None
    assert Path(
        store.pending_checkpoint_operations(task_for(store, started).task_id)[0].facts[
            "original_path"
        ]
    ).exists()


def test_late_native_receipt_cannot_revive_completed_task(system):
    mesh, _, store, native = native_system(system)
    started = mesh.start(spec())
    native.on_publish = lambda: mesh.complete("already done", worker="audit")
    with pytest.raises(StaleTransition):
        mesh.checkpoint("late bytes", worker="audit")
    task = store.get_by_id(task_for(store, started).task_id)
    assert task.state == "completed" and task.checkpoint is None
    assert store.pending_checkpoint_operations(task_for(store, started).task_id)


def test_retry_cannot_rebind_to_changed_bytes_or_another_task(system):
    mesh, _, store, native = native_system(system)
    mesh.start(spec())
    mesh.start(spec("other"))
    native.lose_once = True
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("original", worker="audit")
    for text, task in (("changed", "audit"), ("original", "other")):
        with pytest.raises(SightMeshError, match="does not match"):
            mesh.checkpoint(text, worker=task, operation_id=pending.value.operation_id)
    assert len(native.calls) == 1


def test_corrupt_working_copy_falls_back_to_verified_native_original(system):
    mesh, _, store, native = native_system(system)
    mesh.start(spec())
    done = mesh.checkpoint("retained body", worker="audit")
    operation = operation_for(store, done)
    Path(operation.facts["original_path"]).write_bytes(b"changed")
    assert (
        mesh._read_checkpoint(store.get_by_id(task_for(store, done).task_id))
        == "retained body"
    )
    native.download = lambda body: body[:-1]
    with pytest.raises(EvidenceUnavailable):
        mesh._read_checkpoint(store.get_by_id(task_for(store, done).task_id))


def test_default_sdk_discovers_native_client_without_retention_adapter(
    system, monkeypatch
):
    mesh, client, store, native = native_system(system)
    mesh.evidence_client = None
    client.base_url = "http://native.invalid"
    client.info = lambda: {
        "service_capabilities": {"managed_task_launch": 1, "execution_evidence": 1}
    }
    monkeypatch.setattr(
        sdk_module, "EvidenceClient", lambda url: EvidenceClient(url, opener=native)
    )
    mesh.start(spec())
    done = mesh.checkpoint("ordinary SDK path", worker="audit")
    assert operation_for(store, done).durable_commit


def test_existing_checkpoint_cli_can_retry_the_explicit_operation(
    system, monkeypatch, capsys
):
    from sightmesh.cli import parser
    from sightmesh.cli import tasks

    mesh, _, store, native = native_system(system)
    mesh.start(spec())
    native.lose_once = True
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("CLI recovery", worker="audit")
    args = parser().parse_args(
        [
            "checkpoint",
            "CLI recovery",
            "--worker",
            "audit",
            "--operation-id",
            pending.value.operation_id,
        ]
    )
    monkeypatch.setattr(tasks, "_mesh", lambda _: mesh)
    assert tasks.cmd_checkpoint(args) == 0
    assert store.checkpoint_operation_by_id(pending.value.operation_id).durable_commit
    capsys.readouterr()


def test_checkpoint_commit_requires_the_fence_to_be_actually_held(system):
    mesh, _, store, native = native_system(system)
    started = mesh.start(spec())
    native.lose_once = True
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("held fence", worker="audit")
    operation = store.checkpoint_operation_by_id(pending.value.operation_id)
    receipt = next(iter(native.records.values()))[0]
    with store.task_lock(operation.task_id) as fence:
        with fence.external_io():
            with pytest.raises(TaskStoreError, match="fence"):
                store.checkpoint_with_occurrence(operation, receipt, fence=fence)
    assert task_for(store, started).checkpoint is None


def test_native_receipt_does_not_override_weak_task_writer_policy(system, monkeypatch):
    mesh, _, store, native = native_system(system)
    started = mesh.start(spec())
    actual_open = store._database._open

    def weak_open():
        conn = actual_open()
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    native.on_publish = lambda: monkeypatch.setattr(store._database, "_open", weak_open)
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("native confirmed, reference unconfirmed", worker="audit")
    monkeypatch.setattr(store._database, "_open", actual_open)
    native.on_publish = lambda: None
    assert task_for(store, started).checkpoint is None
    operation = store.checkpoint_operation_by_id(pending.value.operation_id)
    assert operation.receipt is None and not operation.durable_commit
    mesh.checkpoint(
        "native confirmed, reference unconfirmed",
        worker="audit",
        operation_id=operation.operation_id,
    )
    assert native.calls[0] == native.calls[1]


def test_prepared_operation_facts_cannot_be_rebound_directly_in_the_store(system):
    mesh, _, store, native = native_system(system)
    started = mesh.start(spec())
    native.lose_once = True
    with pytest.raises(CheckpointPending) as pending:
        mesh.checkpoint("immutable intent", worker="audit")
    operation = store.checkpoint_operation_by_id(pending.value.operation_id)
    with store.task_lock(operation.task_id) as fence:
        with pytest.raises(TaskStoreError, match="immutable facts"):
            store.prepare_checkpoint_operation(
                task_for(store, started),
                operation.operation_id,
                operation.checkpoint,
                {**operation.facts, "original_path": "/changed"},
                fence=fence,
            )
    assert store.checkpoint_operation_by_id(operation.operation_id) == operation
