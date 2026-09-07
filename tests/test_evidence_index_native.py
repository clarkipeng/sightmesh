from __future__ import annotations

import copy
import io
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest

from sightmesh.evidence import EvidenceClient, EvidenceUnavailable
from sightmesh.evidence_index import EvidenceIndex, MAX_FRAME_BYTES, OVERLAP_BYTES


EXECUTION = "11111111-1111-4111-8111-111111111111"


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class NativeWire:
    """Actual native JSON envelopes/headers; the production HTTP client is used."""

    def __init__(self, chunks=(), page_size=2):
        self.body = b""
        self.frames = []
        self.page_size = page_size
        self.requests = []
        self.fail_after = None
        self.durability = "confirmed"
        self.edit_page = lambda page: page
        for chunk in chunks:
            self.append(chunk)

    def append(self, body, outcome=None, captured_at="2026-09-07T00:00:00Z"):
        raw_start = len(self.body)
        compressed_start = (
            self.frames[-1]["compressed_range"]["end"] if self.frames else 0
        )
        self.body += body
        self.frames.append(
            {
                "raw_range": {"start": raw_start, "end": len(self.body)},
                "compressed_range": {
                    "start": compressed_start,
                    "end": compressed_start + len(body) + 100,
                },
                "captured_at": captured_at,
                "control": outcome is not None,
                "outcome": outcome,
                "legacy_sql_row": None,
            }
        )

    def client(self):
        return EvidenceClient("http://native.invalid", opener=self.open)

    def open(self, request):
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        self.requests.append((parsed.path, query))
        if parsed.path == "/api/info":
            data = {"service_capabilities": {"execution_evidence": 1}}
        elif parsed.path.endswith("/raw-log/provenance"):
            anchor = int(query["after_frame"][0]) if "after_frame" in query else None
            index = (
                0
                if anchor is None
                else next(
                    i + 1
                    for i, frame in enumerate(self.frames)
                    if frame["compressed_range"]["start"] == anchor
                )
            )
            frames = self.frames[index : index + self.page_size]
            data = self.edit_page(
                copy.deepcopy(
                    {
                        "frames": frames,
                        "next_after_frame": frames[-1]["compressed_range"]["start"]
                        if frames
                        else anchor,
                        "at_available_end": index + len(frames) == len(self.frames),
                        "outcome": self.frames[index + len(frames) - 1]["outcome"]
                        if index + len(frames)
                        else None,
                        "durability": self.durability,
                    }
                )
            )
        elif parsed.path.endswith("/raw-log"):
            start, end = int(query["start"][0]), int(query["end"][0])
            if self.fail_after is not None and end > self.fail_after:
                raise EvidenceUnavailable("injected unavailable source")
            body = self.body[start:end]
            return Response(
                body,
                {
                    "x-cdesktop-source-id": EXECUTION,
                    "x-cdesktop-source-range": f"[{start}, {start + len(body)})",
                    "x-cdesktop-source-durability": self.durability,
                },
            )
        else:
            raise AssertionError(parsed.path)
        return Response(json.dumps({"success": True, "data": data}).encode())


@pytest.mark.parametrize("mutation", ["metadata_body", "legacy_body", "bool_cursor"])
def test_unknown_metadata_cannot_become_a_hidden_body_copy_or_ambiguous_cursor(
    tmp_path, mutation
):
    wire = NativeWire([b"searchable evidence"], page_size=1)

    def edit(page):
        if mutation == "metadata_body":
            page["frames"][0]["body"] = "must not persist"
        elif mutation == "legacy_body":
            page["frames"][0]["legacy_sql_row"] = {"body": "must not persist"}
        else:
            page["next_after_frame"] = False
        return page

    wire.edit_page = edit
    index = EvidenceIndex(tmp_path / "index.db")
    state = index.sync(wire.client(), EXECUTION)
    assert state.error and state.after_frame is None and state.raw_end == 0
    assert b"must not persist" not in index.path.read_bytes()


def test_cross_frame_utf8_literal_queries_and_empty_frame_cursor(tmp_path):
    text = 'prefix unique.job_id: "一🙂二" cross-frame target suffix'
    encoded = text.encode()
    split = encoded.index("🙂".encode()) + 2
    wire = NativeWire([encoded[:split], b"", encoded[split:]], page_size=1)
    wire.append(b"", outcome="complete")
    index = EvidenceIndex(tmp_path / "index.sqlite")
    first = index.sync(wire.client(), EXECUTION, task_id="task", repo="repo")
    assert first.after_frame == 0 and not first.at_available_end
    complete = index.sync(
        wire.client(), EXECUTION, task_id="task", repo="repo", max_pages=4
    )
    assert complete.after_frame == wire.frames[-1]["compressed_range"]["start"]
    assert complete.raw_end == len(encoded) and complete.outcome == "complete"
    for query in ("unique.job_id", '"一🙂二"', "cross-frame target"):
        result = index.search(wire.client(), query, task_id="task", repo="repo")
        assert result.complete and len(result.hits) == 1
        hit = result.hits[0]
        assert encoded[hit.match_start : hit.match_end].decode() == query
        assert query in hit.snippet
    assert not index.search(wire.client(), "UNIQUE.job_id").hits
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 4
        assert all(row[0] is None for row in db.execute("SELECT text FROM postings"))
    assert not any("body" in row for row in wire.frames)


def test_resume_rereads_only_bounded_overlap_and_never_duplicates(tmp_path):
    first = b"a" * (MAX_FRAME_BYTES - 3) + b"job"
    wire = NativeWire([first], page_size=1)
    index = EvidenceIndex(tmp_path / "index.sqlite")
    initial = index.sync(wire.client(), EXECUTION)
    assert initial.at_available_end and initial.outcome is None
    wire.append(b".id distinct second match")
    wire.requests.clear()
    index.sync(wire.client(), EXECUTION)
    result = index.search(wire.client(), "job.id")
    assert len(result.hits) == 1 and not result.complete
    reads = [q for path, q in wire.requests if path.endswith("/raw-log")]
    assert (
        reads and min(int(q["start"][0]) for q in reads) == len(first) - OVERLAP_BYTES
    )
    assert (
        max(int(q["end"][0]) - int(q["start"][0]) for q in reads)
        <= MAX_FRAME_BYTES + OVERLAP_BYTES
    )
    for _ in range(2):
        index.sync(wire.client(), EXECUTION)
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 2


def test_literal_match_survives_every_utf8_byte_boundary(tmp_path):
    body = 'before🙂一.word"after'.encode()
    for split in range(1, len(body)):
        wire = NativeWire([body[:split], body[split:]])
        index = EvidenceIndex(tmp_path / f"split-{split}.sqlite")
        index.sync(wire.client(), EXECUTION)
        result = index.search(wire.client(), '🙂一.word"')
        assert len(result.hits) == 1, split
        assert (
            body[result.hits[0].match_start : result.hits[0].match_end].decode()
            == '🙂一.word"'
        )


def test_maximum_query_is_searchable_across_full_frame_boundary(tmp_path):
    query = "🙂" * 255 + "末"
    prefix = b"x" * (MAX_FRAME_BYTES - 1022)
    body = prefix + query.encode() + b" suffix"
    wire = NativeWire([body[:MAX_FRAME_BYTES], body[MAX_FRAME_BYTES:]])
    index = EvidenceIndex(tmp_path / "index.sqlite")
    index.sync(wire.client(), EXECUTION)
    result = index.search(wire.client(), query)
    assert len(result.hits) == 1 and result.hits[0].match_start == len(prefix)


def test_native_failure_preserves_committed_prefix_then_recovers(tmp_path):
    wire = NativeWire([b"first finding", b"second finding"])
    wire.fail_after = len(b"first finding")
    index = EvidenceIndex(tmp_path / "index.sqlite")
    failed = index.sync(wire.client(), EXECUTION)
    assert (
        failed.error and failed.after_frame == 0 and failed.raw_end == wire.fail_after
    )
    result = index.search(wire.client(), "finding")
    assert len(result.hits) == 1 and not result.complete and result.sources[0].error
    wire.fail_after = None
    recovered = index.sync(wire.client(), EXECUTION)
    assert recovered.error is None and recovered.raw_end == len(wire.body)
    assert len(index.search(wire.client(), "finding").hits) == 2


def test_postings_and_cursor_roll_back_together(tmp_path):
    wire = NativeWire([b"rollback sentinel"])
    index = EvidenceIndex(tmp_path / "index.sqlite")
    with sqlite3.connect(index.path) as db:
        db.execute(
            "CREATE TRIGGER reject_cursor BEFORE UPDATE OF after_frame ON sources "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        index.sync(wire.client(), EXECUTION)
    assert index.status(EXECUTION).after_frame is None
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0
        db.execute("DROP TRIGGER reject_cursor")
    assert index.sync(wire.client(), EXECUTION).after_frame == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(durability="unverified"),
        lambda p: p.update(next_after_frame=9),
        lambda p: p["frames"][0]["raw_range"].update(start=1),
        lambda p: p["frames"][0]["compressed_range"].update(start=1),
        lambda p: p.update(outcome="complete"),
    ],
)
def test_invalid_provenance_cannot_advance_cursor(tmp_path, mutation):
    wire = NativeWire([b"original evidence"])

    def mutate(page):
        mutation(page)
        return page

    wire.edit_page = mutate
    index = EvidenceIndex(tmp_path / "index.sqlite")
    result = index.sync(wire.client(), EXECUTION)
    assert result.error and result.after_frame is None


def test_rebuild_compaction_filters_and_missing_originals_are_honest(tmp_path):
    wire = NativeWire([b"unique original provenance sentence not kept as a body"])
    wire.append(b"", outcome="legacy_unknown", captured_at=None)
    index = EvidenceIndex(tmp_path / "index.sqlite")
    index.sync(wire.client(), EXECUTION, task_id="t1", repo="repo")
    query = "original provenance"
    before = index.search(wire.client(), query)
    assert before.hits and not before.complete
    assert not index.search(wire.client(), query, task_id="other").sources
    assert not index.search(wire.client(), query, captured_after=2_000_000_000).hits
    original = wire.body
    index.rebuild(wire.client())
    index.compact()
    assert index.search(wire.client(), query) == before
    assert wire.body == original and original not in index.path.read_bytes()
    wire.fail_after = 0
    unavailable = index.search(wire.client(), query)
    assert (
        not unavailable.hits
        and unavailable.sources[0].error
        and not unavailable.complete
    )


def test_explicit_query_limits_and_foreign_database_refusal(tmp_path):
    index = EvidenceIndex(tmp_path / "index.sqlite")
    for query in ("", "ab", "a" * 257, "nul\0query"):
        with pytest.raises(ValueError):
            index.search(NativeWire().client(), query)
    assert not index.search(NativeWire().client(), "unknown").complete
    other = tmp_path / "task.sqlite"
    with sqlite3.connect(other) as db:
        db.execute("CREATE TABLE managed_tasks(value TEXT)")
        db.execute("INSERT INTO managed_tasks VALUES('preserved')")
    with pytest.raises(EvidenceUnavailable):
        EvidenceIndex(other)
    with sqlite3.connect(other) as db:
        assert (
            db.execute("SELECT value FROM managed_tasks").fetchone()[0] == "preserved"
        )
