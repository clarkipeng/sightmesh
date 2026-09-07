"""Disposable contentless search over confirmed native evidence.

Queries are literal, case-sensitive substrings of 3..256 Unicode characters,
not FTS expressions. Maximum-query overlap preserves cross-frame matches without
a second tokenizer or persisted transcript tail. Snippets come from originals.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .evidence import EvidenceClient, EvidenceUnavailable
from .evidence_stream import (
    MAX_FRAME_BYTES,
    confirmed_range as _confirmed_range,
    timestamp as _timestamp,
    validate_page as _validate_page,
)

MAX_QUERY_CHARACTERS = 256
OVERLAP_BYTES = MAX_QUERY_CHARACTERS * 4
INDEX_VERSION = 1


@dataclass(frozen=True)
class SourceStatus:
    execution_id: str
    task_id: str | None
    repo: str | None
    after_frame: int | None
    raw_end: int
    compressed_end: int
    outcome: str | None
    at_available_end: bool
    error: str | None
    artifact_id: str | None = None
    receipt: dict[str, Any] | None = None

    @property
    def source_key(self) -> str:
        return _source_key(self.execution_id, self.artifact_id)


@dataclass(frozen=True)
class SearchHit:
    execution_id: str
    task_id: str | None
    repo: str | None
    frame_start: int | None
    captured_at: str | None
    raw_start: int
    raw_end: int
    match_start: int
    match_end: int
    snippet: str
    artifact_id: str | None = None


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    sources: tuple[SourceStatus, ...]
    next_after: int | None
    unchecked_sources: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Capture completeness of the selected indexed sources, not all tasks."""
        return bool(self.sources) and all(
            s.outcome == "complete" and s.at_available_end and not s.error
            for s in self.sources
        ) and not self.unchecked_sources


class EvidenceIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        with self._connect() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables and "evidence_index_version" not in tables:
                raise EvidenceUnavailable(
                    "not a versioned evidence index; use a fresh index path"
                )
            if tables:
                versions = conn.execute(
                    "SELECT version FROM evidence_index_version"
                ).fetchall()
                if len(versions) != 1 or versions[0][0] != INDEX_VERSION:
                    raise EvidenceUnavailable("unsupported disposable index version")
                return
            try:
                conn.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE evidence_index_version (version INTEGER NOT NULL);
                    INSERT INTO evidence_index_version VALUES (1);
                    CREATE VIRTUAL TABLE postings USING fts5(
                        text, content='', tokenize='trigram case_sensitive 1'
                    );
                    CREATE TABLE sources (
                        source_key TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
                        artifact_id TEXT, receipt TEXT, task_id TEXT, repo TEXT,
                        after_frame INTEGER, raw_end INTEGER NOT NULL DEFAULT 0,
                        compressed_end INTEGER NOT NULL DEFAULT 0, outcome TEXT,
                        at_available_end INTEGER NOT NULL DEFAULT 0, error TEXT
                    );
                    CREATE INDEX source_task ON sources(task_id);
                    CREATE INDEX source_repo ON sources(repo);
                    CREATE INDEX source_execution ON sources(execution_id);
                    CREATE TABLE frames (
                        rowid INTEGER PRIMARY KEY,
                        source_key TEXT NOT NULL REFERENCES sources(source_key),
                        frame_start INTEGER NOT NULL, frame_end INTEGER NOT NULL,
                        raw_start INTEGER NOT NULL, raw_end INTEGER NOT NULL,
                        window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
                        sha256 TEXT NOT NULL, captured_at TEXT, captured_time REAL,
                        metadata TEXT NOT NULL, UNIQUE(source_key, frame_start)
                    );
                    CREATE INDEX frame_time ON frames(captured_time);
                    COMMIT;
                """)
            except sqlite3.DatabaseError as exc:
                conn.rollback()
                raise EvidenceUnavailable(
                    "SQLite FTS5 with trigram support is required"
                ) from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def status(
        self, execution_id: str, artifact_id: str | None = None
    ) -> SourceStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE source_key=?",
                (_source_key(execution_id, artifact_id),),
            ).fetchone()
        return _status(row) if row is not None else None

    def _register(
        self,
        execution_id: str,
        task_id: str | None,
        repo: str | None,
        artifact_id: str | None = None,
    ) -> None:
        source_key = _source_key(execution_id, artifact_id)
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO sources(source_key,execution_id,task_id,repo,artifact_id) VALUES(?,?,?,?,?)",
                (source_key, execution_id, task_id, repo, artifact_id),
            )
            row = conn.execute(
                "SELECT task_id,repo FROM sources WHERE source_key=?", (source_key,)
            ).fetchone()
            if row is None or (row[0], row[1]) != (task_id, repo):
                raise EvidenceUnavailable(
                    "source identity cannot be rebound to another task or repo"
                )

    def sync(
        self,
        client: EvidenceClient,
        execution_id: str,
        *,
        task_id: str | None = None,
        repo: str | None = None,
        max_pages: int = 1,
    ) -> SourceStatus:
        """Ingest bounded native pages; each transaction preserves a confirmed prefix."""
        if type(max_pages) is not int or not 1 <= max_pages <= 128:
            raise ValueError("max_pages must be between 1 and 128")
        self._register(execution_id, task_id, repo)
        try:
            for _ in range(max_pages):
                state = self.status(execution_id)
                assert state is not None
                page = client.provenance(execution_id, state.after_frame)
                frames = _validate_page(page, state)
                for frame in frames:
                    raw = frame["raw_range"]
                    window_start = max(0, raw["start"] - OVERLAP_BYTES)
                    if raw["start"] == raw["end"]:
                        window_start, body, text = raw["end"], b"", ""
                    else:
                        body = _confirmed_range(
                            client, execution_id, window_start, raw["end"]
                        )
                        window_start, body, text = _utf8_window(window_start, body)
                    self._commit_frame(state, frame, window_start, body, text)
                    state = self.status(execution_id)
                    assert state is not None
                with self._connect() as conn, conn:
                    conn.execute(
                        "UPDATE sources SET at_available_end=?,error=NULL "
                        "WHERE source_key=? AND after_frame IS ?",
                        (
                            int(page["at_available_end"]),
                            execution_id,
                            page["next_after_frame"],
                        ),
                    )
                if page["at_available_end"]:
                    break
        except (EvidenceUnavailable, OSError, ValueError, KeyError, TypeError) as exc:
            self._mark_unavailable(execution_id, type(exc).__name__)
        result = self.status(execution_id)
        assert result is not None
        return result

    def _commit_frame(
        self,
        expected: SourceStatus,
        frame: dict[str, Any],
        window_start: int,
        body: bytes,
        text: str,
    ) -> None:
        raw, compressed = frame["raw_range"], frame["compressed_range"]
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM sources WHERE source_key=?", (expected.source_key,)
            ).fetchone()
            if row is None or _status(row) != expected:
                raise EvidenceUnavailable(
                    "index cursor changed during ingestion; retry from its new position"
                )
            inserted = conn.execute(
                "INSERT INTO frames(source_key,frame_start,frame_end,raw_start,raw_end,"
                "window_start,window_end,sha256,captured_at,captured_time,metadata) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    expected.source_key,
                    compressed["start"],
                    compressed["end"],
                    raw["start"],
                    raw["end"],
                    window_start,
                    window_start + len(body),
                    hashlib.sha256(body).hexdigest(),
                    frame["captured_at"],
                    _timestamp(frame["captured_at"]),
                    json.dumps(frame, sort_keys=True, separators=(",", ":")),
                ),
            )
            if text:
                conn.execute(
                    "INSERT INTO postings(rowid,text) VALUES(?,?)",
                    (inserted.lastrowid, text),
                )
            conn.execute(
                "UPDATE sources SET after_frame=?,raw_end=?,compressed_end=?,outcome=?,"
                "at_available_end=0,error=NULL WHERE source_key=?",
                (
                    compressed["start"],
                    raw["end"],
                    compressed["end"],
                    frame["outcome"],
                    expected.source_key,
                ),
            )

    def _mark_unavailable(self, source_key: str, error: str) -> None:
        # Keep only a failure category, never a transport response or source body.
        with self._connect() as conn, conn:
            conn.execute(
                "UPDATE sources SET error=? WHERE source_key=?", (error, source_key)
            )

    def sync_artifact(
        self,
        client: EvidenceClient,
        execution_id: str,
        artifact_id: str,
        *,
        task_id: str | None = None,
        repo: str | None = None,
    ) -> SourceStatus:
        """Index one referenced UTF-8 occurrence, with bounded streaming memory.

        Native files have no range-read API. The immutable artifact is one local
        index transaction: receipt + full stream digest + postings commit together.
        On interruption retry this artifact, not a guessed confirmed prefix. This
        holds only the disposable index writer lock, never a task-store fence.
        """
        self._register(execution_id, task_id, repo, artifact_id)
        key = _source_key(execution_id, artifact_id)
        try:
            receipt = _artifact_receipt(client, execution_id, artifact_id)
            with self._connect() as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                current = _status(
                    conn.execute(
                        "SELECT * FROM sources WHERE source_key=?", (key,)
                    ).fetchone()
                )
                if current.receipt is not None and current.receipt != receipt:
                    raise EvidenceUnavailable("artifact occurrence facts changed")
                if current.at_available_end:
                    conn.execute(
                        "UPDATE sources SET error=NULL WHERE source_key=?", (key,)
                    )
                else:
                    offset, tail = 0, b""
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    with _artifact_stream(
                        client, execution_id, artifact_id, receipt
                    ) as chunks:
                        for chunk in chunks:
                            decoder.decode(chunk, final=False)
                            start, body, text = _utf8_window(
                                offset - len(tail), tail + chunk
                            )
                            inserted = conn.execute(
                                "INSERT INTO frames(source_key,frame_start,frame_end,raw_start,raw_end,"
                                "window_start,window_end,sha256,captured_at,captured_time,metadata) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (
                                    key,
                                    offset,
                                    offset + len(chunk),
                                    offset,
                                    offset + len(chunk),
                                    start,
                                    start + len(body),
                                    hashlib.sha256(body).hexdigest(),
                                    receipt["captured_at"],
                                    _timestamp(receipt["captured_at"]),
                                    "{}",
                                ),
                            )
                            if text:
                                conn.execute(
                                    "INSERT INTO postings(rowid,text) VALUES(?,?)",
                                    (inserted.lastrowid, text),
                                )
                            tail = (tail + chunk)[-OVERLAP_BYTES:]
                            offset += len(chunk)
                    decoder.decode(b"", final=True)
                    conn.execute(
                        "UPDATE sources SET receipt=?,raw_end=?,outcome='complete',at_available_end=1,error=NULL WHERE source_key=?",
                        (
                            json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                            offset,
                            key,
                        ),
                    )
        except (EvidenceUnavailable, OSError, ValueError, KeyError, TypeError) as exc:
            self._mark_unavailable(key, type(exc).__name__)
        result = self.status(execution_id, artifact_id)
        assert result is not None
        return result

    def search(
        self,
        client: EvidenceClient,
        query: str,
        *,
        task_id: str | None = None,
        repo: str | None = None,
        execution_id: str | None = None,
        captured_after: float | None = None,
        captured_before: float | None = None,
        limit: int = 20,
        after: int = 0,
    ) -> SearchResult:
        """Return bounded original-backed windows and selected-source coverage.

        next_after pages candidate windows, not individual text occurrences.
        This cursor is disposable like the index: restart after a rebuild.
        Artifact windows stream and verify their complete immutable original;
        that costs O(artifact size) per candidate, without retaining its body.
        """
        if not 3 <= len(query) <= MAX_QUERY_CHARACTERS or "\0" in query:
            raise ValueError("query must contain 3..256 Unicode characters without NUL")
        query.encode("utf-8")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(after) is not int
            or after < 0
        ):
            raise ValueError("limit must be 1..100 and after must be nonnegative")
        where, values = ["1=1"], []
        for column, value in (
            ("task_id", task_id),
            ("repo", repo),
            ("execution_id", execution_id),
        ):
            if value is not None:
                where.append(f"s.{column}=?")
                values.append(value)
        source_where, source_values = " AND ".join(where), list(values)
        for op, value in ((">=", captured_after), ("<", captured_before)):
            if value is not None:
                where.append(f"f.captured_time{op}?")
                values.append(value)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.*,s.execution_id,s.task_id,s.repo,s.artifact_id,s.receipt FROM postings p JOIN frames f ON f.rowid=p.rowid "
                "JOIN sources s ON s.source_key=f.source_key WHERE "
                + " AND ".join(where)
                + " AND postings MATCH ? AND f.rowid>? ORDER BY f.rowid LIMIT ?",
                (*values, '"' + query.replace('"', '""') + '"', after, limit + 1),
            ).fetchall()
        # A contentless index cannot turn a stale earlier observation into a
        # current availability claim. Log candidate windows prove only their
        # own bytes, so logs stay unchecked without a deliberate full-source
        # re-verification. An artifact candidate streams and verifies its full
        # immutable original below.
        checked = set()
        next_after = rows[limit - 1]["rowid"] if len(rows) > limit else None
        hits = []
        for row in rows[:limit]:
            try:
                if row["artifact_id"] is None:
                    body = _confirmed_range(
                        client,
                        row["execution_id"],
                        row["window_start"],
                        row["window_end"],
                    )
                else:
                    body = _artifact_window(
                        client,
                        row["execution_id"],
                        row["artifact_id"],
                        json.loads(row["receipt"]),
                        row["window_start"],
                        row["window_end"],
                    )
                if hashlib.sha256(body).hexdigest() != row["sha256"]:
                    raise EvidenceUnavailable("indexed original bytes changed")
                if row["artifact_id"] is not None:
                    checked.add(row["source_key"])
                text = body.decode("utf-8")
                position = text.find(query)
                # A match wholly inside the overlap belongs to its older frame.
                while (
                    position >= 0
                    and row["window_start"]
                    + len(text[: position + len(query)].encode())
                    <= row["raw_start"]
                ):
                    position = text.find(query, position + 1)
                if position < 0:
                    continue
                start = row["window_start"] + len(text[:position].encode())
                hits.append(
                    SearchHit(
                        row["execution_id"],
                        row["task_id"],
                        row["repo"],
                        row["frame_start"] if row["artifact_id"] is None else None,
                        row["captured_at"],
                        row["window_start"],
                        row["window_end"],
                        start,
                        start + len(query.encode()),
                        text[max(0, position - 80) : position + len(query) + 80],
                        row["artifact_id"],
                    )
                )
            except (EvidenceUnavailable, OSError, ValueError) as exc:
                self._mark_unavailable(row["source_key"], type(exc).__name__)
        with self._connect() as conn:
            sources = tuple(
                _status(row)
                for row in conn.execute(
                    "SELECT s.* FROM sources s WHERE "
                    + source_where
                    + " ORDER BY s.execution_id",
                    source_values,
                )
            )
        unchecked = tuple(
            source.source_key
            for source in sources
            if source.source_key not in checked or source.error
        )
        return SearchResult(tuple(hits), sources, next_after, unchecked)

    def rebuild(self, client: EvidenceClient) -> tuple[SourceStatus, ...]:
        """Re-derive the previously indexed prefix; never delete original evidence."""
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            originals = [_status(row) for row in conn.execute("SELECT * FROM sources")]
            conn.execute("INSERT INTO postings(postings) VALUES('delete-all')")
            conn.execute("DELETE FROM frames")
            conn.execute(
                "UPDATE sources SET after_frame=NULL,raw_end=0,compressed_end=0,"
                "outcome=NULL,at_available_end=0,error=NULL"
            )
        result = []
        for original in originals:
            if original.artifact_id is not None:
                result.append(
                    self.sync_artifact(
                        client,
                        original.execution_id,
                        original.artifact_id,
                        task_id=original.task_id,
                        repo=original.repo,
                    )
                )
                continue
            while True:
                state = self.sync(
                    client,
                    original.execution_id,
                    task_id=original.task_id,
                    repo=original.repo,
                )
                if (
                    state.error
                    or state.at_available_end
                    or state.compressed_end >= original.compressed_end
                ):
                    result.append(state)
                    break
        return tuple(result)

    def compact(self) -> None:
        """Compact only this disposable projection, with no original-file mutation."""
        with self._connect() as conn:
            conn.execute("INSERT INTO postings(postings) VALUES('optimize')")
            conn.commit()
            conn.execute("VACUUM")


def _status(row: sqlite3.Row) -> SourceStatus:
    return SourceStatus(
        row["execution_id"],
        row["task_id"],
        row["repo"],
        row["after_frame"],
        row["raw_end"],
        row["compressed_end"],
        row["outcome"],
        bool(row["at_available_end"]),
        row["error"],
        row["artifact_id"],
        json.loads(row["receipt"]) if row["receipt"] else None,
    )


def _source_key(execution_id: str, artifact_id: str | None = None) -> str:
    for value in (execution_id, artifact_id):
        if value is not None and (
            not isinstance(value, str) or not value or "/" in value or len(value) > 200
        ):
            raise ValueError("invalid native source identity")
    return (
        execution_id
        if artifact_id is None
        else f"{execution_id}/artifacts/{artifact_id}"
    )


def _artifact_receipt(
    client: EvidenceClient, execution_id: str, artifact_id: str
) -> dict[str, Any]:
    native = client.artifact_receipt(execution_id, artifact_id)
    if (
        native.get("durability") != "confirmed"
        or native.get("id") != artifact_id
        or native.get("execution_id") != execution_id
    ):
        raise EvidenceUnavailable("unconfirmed artifact occurrence identity")
    fields = (
        "id",
        "execution_id",
        "attachment_id",
        "original_path",
        "original_name",
        "producer_ref",
        "publication_key",
        "captured_at",
        "sha256",
        "size_bytes",
    )
    receipt = {field: native[field] for field in fields}
    if (
        type(receipt["size_bytes"]) is not int
        or not 0 <= receipt["size_bytes"] <= 2**63 - 1
        or not isinstance(receipt["sha256"], str)
        or not re.fullmatch("[0-9a-f]{64}", receipt["sha256"])
    ):
        raise EvidenceUnavailable("invalid artifact byte identity")
    for field in ("attachment_id", "original_path", "captured_at"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise EvidenceUnavailable("missing artifact provenance")
    for field in ("original_name", "producer_ref", "publication_key"):
        if receipt[field] is not None and not isinstance(receipt[field], str):
            raise EvidenceUnavailable("invalid artifact provenance")
    _timestamp(receipt["captured_at"])
    if len(json.dumps(receipt).encode()) > 8192:
        raise EvidenceUnavailable("artifact metadata exceeds consumer bound")
    return receipt


@contextmanager
def _artifact_stream(
    client: EvidenceClient, execution_id: str, artifact_id: str, receipt: dict[str, Any]
) -> Iterator[Iterator[bytes]]:
    with closing(
        client.artifact_chunks(execution_id, artifact_id, chunk_size=MAX_FRAME_BYTES)
    ) as chunks:

        def verified() -> Iterator[bytes]:
            digest, size = hashlib.sha256(), 0
            for chunk in chunks:
                if (
                    not isinstance(chunk, bytes)
                    or not 0 < len(chunk) <= MAX_FRAME_BYTES
                ):
                    raise EvidenceUnavailable("unbounded artifact response chunk")
                size += len(chunk)
                if size > receipt["size_bytes"]:
                    raise EvidenceUnavailable("artifact response exceeds receipt size")
                digest.update(chunk)
                yield chunk
            if size != receipt["size_bytes"] or digest.hexdigest() != receipt["sha256"]:
                raise EvidenceUnavailable(
                    "artifact original no longer matches its receipt"
                )

        yield verified()


def _artifact_window(
    client: EvidenceClient,
    execution_id: str,
    artifact_id: str,
    expected: dict[str, Any],
    start: int,
    end: int,
) -> bytes:
    receipt = _artifact_receipt(client, execution_id, artifact_id)
    if receipt != expected:
        raise EvidenceUnavailable("indexed artifact occurrence facts changed")
    window, offset = bytearray(), 0
    with _artifact_stream(client, execution_id, artifact_id, receipt) as chunks:
        for chunk in chunks:
            first, last = max(start - offset, 0), min(end - offset, len(chunk))
            if first < last:
                window.extend(chunk[first:last])
            offset += len(chunk)
    if len(window) != end - start:
        raise EvidenceUnavailable("artifact window incomplete")
    return bytes(window)


def _utf8_window(start: int, body: bytes) -> tuple[int, bytes, str]:
    skipped = 0
    if start:
        while skipped < min(3, len(body)) and body[skipped] & 0xC0 == 0x80:
            skipped += 1
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(body[skipped:], final=False)
    pending = decoder.getstate()[0]
    end = len(body) - len(pending)
    return start + skipped, body[skipped:end], text
