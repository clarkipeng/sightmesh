from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from urllib.parse import urlsplit

import pytest

from sightmesh.evidence import EvidenceUnavailable
from sightmesh.evidence_index import EvidenceIndex, MAX_FRAME_BYTES
from test_evidence_index_native import EXECUTION, NativeWire, Response


class ArtifactsWire(NativeWire):
    def __init__(self):
        super().__init__()
        self.artifacts = {}
        self.receipts = {}
        self.open_responses = []
        self.edited_body = None

    def publish(self, occurrence, body, name="checkpoint.md"):
        self.artifacts[occurrence] = body
        self.receipts[occurrence] = {
            "id": occurrence,
            "execution_id": EXECUTION,
            "attachment_id": hashlib.sha256(body).hexdigest()[:32],
            "original_path": f"/temporary-worktree/{name}",
            "original_name": name,
            "producer_ref": "task:1",
            "publication_key": f"checkpoint:{occurrence}",
            "captured_at": "2026-09-07T00:00:00Z",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body),
            "durability": "confirmed",
        }

    def open(self, request):
        path = urlsplit(request.full_url).path
        if "/artifacts/" not in path:
            return super().open(request)
        tail = path.split("/artifacts/", 1)[1].split("/")
        occurrence = tail[0]
        if occurrence not in self.artifacts:
            raise EvidenceUnavailable("original unavailable")
        if len(tail) == 2:
            body = (
                self.artifacts[occurrence]
                if self.edited_body is None
                else self.edited_body
            )
        else:
            body = json.dumps(
                {"success": True, "data": self.receipts[occurrence]}
            ).encode()
        response = Response(body)
        self.open_responses.append(response)
        return response


def test_reclaimed_worktree_checkpoint_search_preserves_distinct_native_occurrences(
    tmp_path,
):
    worktree = tmp_path / "checkpoint.md"
    worktree.write_text("retained original proof: job_id.1234")
    wire = ArtifactsWire()
    wire.publish("occurrence-a", worktree.read_bytes(), "first.md")
    wire.publish("occurrence-b", worktree.read_bytes(), "second.md")
    worktree.unlink()  # Only the fixture's redundant working copy is reclaimed.
    index = EvidenceIndex(tmp_path / "index.db")
    for occurrence in wire.artifacts:
        assert index.sync_artifact(
            wire.client(), EXECUTION, occurrence, task_id="task", repo="repo"
        ).at_available_end
    found = index.search(wire.client(), "job_id.1234", task_id="task")
    assert found.complete and len(found.hits) == 2
    assert {hit.artifact_id for hit in found.hits} == set(wire.artifacts)
    assert all(hit.frame_start is None for hit in found.hits)
    assert {source.receipt["original_name"] for source in found.sources} == {
        "first.md",
        "second.md",
    }
    assert len({source.receipt["attachment_id"] for source in found.sources}) == 1
    assert all(response.closed for response in wire.open_responses)


def test_artifact_utf8_boundary_maximum_query_rebuild_and_bodyless_projection(tmp_path):
    wire = ArtifactsWire()
    query = "🙂" * 255 + "末"
    body = (
        b"a" * (MAX_FRAME_BYTES - 501)
        + query.encode()
        + b" unique-secret-body-sentinel"
    )
    wire.publish("artifact", body)
    index = EvidenceIndex(tmp_path / "index.db")
    state = index.sync_artifact(wire.client(), EXECUTION, "artifact", repo="repo")
    assert state.outcome == "complete" and not state.error
    found = index.search(wire.client(), query)
    assert len(found.hits) == 1 and found.hits[0].match_end - found.hits[
        0
    ].match_start == len(query.encode())
    originals = copy.deepcopy(wire.artifacts)
    assert all(s.at_available_end for s in index.rebuild(wire.client()))
    index.compact()
    assert index.search(wire.client(), query).hits == found.hits
    assert wire.artifacts == originals
    with sqlite3.connect(index.path) as conn:
        assert all(row[0] is None for row in conn.execute("SELECT text FROM postings"))
    assert b"unique-secret-body-sentinel" not in index.path.read_bytes()


@pytest.mark.parametrize(
    "corruption", ["truncated", "extra", "changed", "unverified", "identity"]
)
def test_incomplete_or_unverified_artifact_never_commits_postings(tmp_path, corruption):
    wire = ArtifactsWire()
    wire.publish("artifact", b"first searchable phrase" * 4000)
    if corruption == "truncated":
        wire.edited_body = wire.artifacts["artifact"][:-1]
    elif corruption == "extra":
        wire.edited_body = wire.artifacts["artifact"] + b"x"
    elif corruption == "changed":
        wire.edited_body = b"x" + wire.artifacts["artifact"][1:]
    elif corruption == "unverified":
        wire.receipts["artifact"]["durability"] = "unverified"
    else:
        wire.receipts["artifact"]["execution_id"] = "wrong"
    index = EvidenceIndex(tmp_path / "index.db")
    state = index.sync_artifact(wire.client(), EXECUTION, "artifact")
    assert state.error and state.raw_end == 0 and not state.at_available_end
    with sqlite3.connect(index.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0
    assert all(response.closed for response in wire.open_responses)


def test_artifact_missing_or_changed_provenance_after_indexing_is_not_an_empty_complete_result(
    tmp_path,
):
    wire = ArtifactsWire()
    wire.publish("artifact", b"searchable original")
    index = EvidenceIndex(tmp_path / "index.db")
    index.sync_artifact(wire.client(), EXECUTION, "artifact")
    wire.receipts["artifact"]["original_path"] = "/changed"
    changed = index.search(wire.client(), "searchable")
    assert not changed.complete and changed.sources[0].error and not changed.hits
    wire.artifacts.clear()
    missing = index.search(wire.client(), "searchable")
    assert not missing.complete and missing.sources[0].error


def test_empty_and_binary_artifacts_have_honest_distinct_coverage(tmp_path):
    wire = ArtifactsWire()
    wire.publish("empty", b"")
    wire.publish("binary", b"prefix\xff\xff")
    index = EvidenceIndex(tmp_path / "index.db")
    assert index.sync_artifact(wire.client(), EXECUTION, "empty").outcome == "complete"
    assert (
        index.sync_artifact(wire.client(), EXECUTION, "binary").error
        == "UnicodeDecodeError"
    )
    assert not index.search(wire.client(), "nothing").complete


def test_log_and_artifact_sources_share_one_projection_without_cursor_collision(
    tmp_path,
):
    wire = ArtifactsWire()
    wire.append(b"shared literal in log")
    wire.append(b"", outcome="complete")
    wire.publish("artifact", b"shared literal in checkpoint")
    index = EvidenceIndex(tmp_path / "index.db")
    assert index.sync(wire.client(), EXECUTION, task_id="task").outcome == "complete"
    assert (
        index.sync_artifact(
            wire.client(), EXECUTION, "artifact", task_id="task"
        ).outcome
        == "complete"
    )
    found = index.search(wire.client(), "shared literal", execution_id=EXECUTION)
    assert found.complete and len(found.hits) == 2 and len(found.sources) == 2
    assert {hit.artifact_id for hit in found.hits} == {None, "artifact"}
    assert (
        index.status(EXECUTION).after_frame
        == wire.frames[-1]["compressed_range"]["start"]
    )
    assert index.status(EXECUTION, "artifact").after_frame is None
    index.rebuild(wire.client())
    assert index.search(wire.client(), "shared literal").hits == found.hits
