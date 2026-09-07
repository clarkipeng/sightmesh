"""SDK checkpoint retention against native-wire-shaped fake HTTP only."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sightmesh.evidence import EvidenceClient
from sightmesh.retention import CheckpointRetention
from sightmesh.sdk import SightMesh
from sightmesh.task_store import TaskStore
from test_sdk import FakeClient, spec, system


class Response:
    def __init__(self, body): self.body = body; self.headers = {}; self.at = 0
    def read(self, n=-1):
        end = len(self.body) if n < 0 else min(len(self.body), self.at + n)
        result = self.body[self.at:end]; self.at = end; return result
    def __enter__(self): return self
    def __exit__(self, *_): return False


class NativeHttp:
    def __init__(self): self.records = {}; self.calls = []; self.lose_once = False
    def __call__(self, request):
        parsed = urlsplit(request.full_url); query = parse_qs(parsed.query)
        if parsed.path == "/api/info": return Response(b'{"data":{"service_capabilities":{"execution_evidence":1}}}')
        if parsed.path.endswith("/artifacts") and request.method == "POST":
            key = query["publication_key"][0]; body = request.data.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            self.calls.append(key); self.records.setdefault(key, body)
            if self.lose_once:
                self.lose_once = False; raise OSError("response lost after publication")
            return Response(("{" + f'"data":{{"id":"{key}","execution_id":"exec-1","publication_key":"{key}","producer_ref":"{query["producer_ref"][0]}","original_path":"{query["original_path"][0]}","durability":"confirmed","sha256":"{hashlib.sha256(body).hexdigest()}","size_bytes":{len(body)}}}' + "}").encode())
        occurrence = parsed.path.rsplit("/", 1)[-1]
        if parsed.path.endswith("/file"): occurrence = parsed.path.rsplit("/", 2)[-2]; return Response(self.records[occurrence])
        body = self.records[occurrence]
        return Response(("{" + f'"data":{{"id":"{occurrence}","execution_id":"exec-1","durability":"confirmed","sha256":"{hashlib.sha256(body).hexdigest()}"}}' + "}").encode())


def test_sdk_checkpoint_recovers_native_retention_and_replays_only_the_pending_operation(system):
    _mesh, client, store, ownership = system
    native = NativeHttp(); evidence = EvidenceClient("http://native", opener=native)
    retention = CheckpointRetention(store.path, evidence, "exec-1")
    mesh = SightMesh(client=client, store=store, ownership=ownership, checkpoint_retention=retention, environment={})
    started = mesh.start(spec())
    first = mesh.checkpoint("same bytes", worker="audit")
    local = Path(client.workspace(started.workspace_id)["container_ref"]) / "project" / first.checkpoint
    local.unlink()
    mesh.replace("audit")
    assert client.launches[-1][1]["request"]["session"]["prompt"] == "same bytes"
    second = mesh.checkpoint("same bytes", worker="audit")
    assert second.checkpoint != first.checkpoint
    assert len(native.calls) == 2
