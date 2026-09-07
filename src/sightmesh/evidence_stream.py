"""Bounded, confirmed native frame traversal shared by disposable consumers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Protocol

from .evidence import EvidenceClient, EvidenceUnavailable

MAX_FRAME_BYTES = 64 * 1024
MAX_PAGE_FRAMES = 128
MAX_RECORD_BYTES = 1024 * 1024


class Position(Protocol):
    raw_end: int
    compressed_end: int
    after_frame: int | None
    outcome: str | None


def integer(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise EvidenceUnavailable("invalid native integer")
    return value


def timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceUnavailable("invalid capture time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise EvidenceUnavailable("capture time has no timezone")
    return parsed.timestamp()


def validate_page(page: dict[str, Any], state: Position) -> list[dict[str, Any]]:
    if page["durability"] != "confirmed" or type(page["at_available_end"]) is not bool:
        raise EvidenceUnavailable("unconfirmed or invalid provenance page")
    frames = page["frames"]
    if not isinstance(frames, list) or len(frames) > MAX_PAGE_FRAMES:
        raise EvidenceUnavailable("unbounded provenance page")
    raw_end, compressed_end, outcome, cursor = (
        state.raw_end,
        state.compressed_end,
        state.outcome,
        state.after_frame,
    )
    fields = {
        "raw_range",
        "compressed_range",
        "captured_at",
        "control",
        "outcome",
        "legacy_sql_row",
    }
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != fields:
            raise EvidenceUnavailable("unknown native frame metadata contract")
        raw, compressed = frame["raw_range"], frame["compressed_range"]
        if set(raw) != {"start", "end"} or set(compressed) != {"start", "end"}:
            raise EvidenceUnavailable("unknown native range contract")
        start, end = integer(raw["start"]), integer(raw["end"])
        first, last = integer(compressed["start"]), integer(compressed["end"])
        if (
            outcome is not None
            or start != raw_end
            or not start <= end <= start + MAX_FRAME_BYTES
            or first != compressed_end
            or last <= first
        ):
            raise EvidenceUnavailable("noncontiguous native provenance")
        if type(frame["control"]) is not bool or frame["outcome"] not in {
            None,
            "complete",
            "unavailable",
            "legacy_unknown",
        }:
            raise EvidenceUnavailable("invalid native capture outcome")
        timestamp(frame["captured_at"])
        legacy = frame["legacy_sql_row"]
        if legacy is not None:
            if not isinstance(legacy, dict) or set(legacy) != {
                "row_id",
                "inserted_at",
                "reported_byte_size",
                "original_bytes",
            }:
                raise EvidenceUnavailable("unknown legacy metadata contract")
            if not isinstance(legacy["inserted_at"], str):
                raise EvidenceUnavailable("invalid legacy insertion time")
            for field in ("row_id", "reported_byte_size"):
                if (
                    type(legacy[field]) is not int
                    or not -(2**63) <= legacy[field] < 2**63
                ):
                    raise EvidenceUnavailable("invalid legacy SQL integer")
            integer(legacy["original_bytes"])
        if frame["outcome"] is not None and (not frame["control"] or start != end):
            raise EvidenceUnavailable("terminal capture seal contains ordinary bytes")
        if len(json.dumps(frame).encode()) > 8192:
            raise EvidenceUnavailable("oversized native metadata")
        raw_end, compressed_end, outcome, cursor = end, last, frame["outcome"], first
    next_cursor = page["next_after_frame"]
    if next_cursor is not None:
        integer(next_cursor)
    if next_cursor != cursor or page["outcome"] != outcome:
        raise EvidenceUnavailable("native page cursor/outcome mismatch")
    if (not frames or outcome is not None) and not page["at_available_end"]:
        raise EvidenceUnavailable(
            "native page cannot advance or has data after its seal"
        )
    return frames


def confirmed_range(
    client: EvidenceClient, execution_id: str, start: int, end: int
) -> bytes:
    result = client.raw(execution_id, start, end)
    if (
        result.source_id != execution_id
        or result.start != start
        or result.end != end
        or len(result.body) != end - start
        or result.durability != "confirmed"
    ):
        raise EvidenceUnavailable("native source range is incomplete or unconfirmed")
    return result.body


@dataclass
class NativeRecords:
    """Single-pass JSONL reader. No source copies, persisted tail, or implicit retry.

    Stop at a configured page/record bound with explicit incomplete coverage.
    A caller can rederive from the originals; a partial record is never dropped
    and reported as a complete parse. Arbitrary non-JSON legacy bytes are unknown.
    """

    client: EvidenceClient
    execution_id: str
    max_pages: int = 128
    raw_end: int = 0
    compressed_end: int = 0
    after_frame: int | None = None
    outcome: str | None = None
    at_available_end: bool = False
    error: str | None = None
    _used: bool = False

    def __iter__(self) -> Iterator[dict[str, Any] | str]:
        if self._used:
            raise ValueError("native record traversal is single-use")
        self._used = True
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= 4096:
            raise ValueError("max_pages must be 1..4096")
        pending = bytearray()
        try:
            for _ in range(self.max_pages):
                page = self.client.provenance(self.execution_id, self.after_frame)
                for frame in validate_page(page, self):
                    raw = frame["raw_range"]
                    chunk = (
                        confirmed_range(
                            self.client, self.execution_id, raw["start"], raw["end"]
                        )
                        if raw["end"] > raw["start"]
                        else b""
                    )
                    for part in chunk.splitlines(keepends=True):
                        if len(pending) + len(part) > MAX_RECORD_BYTES:
                            raise EvidenceUnavailable(
                                "native JSONL record exceeds consumer bound"
                            )
                        pending.extend(part)
                        if part.endswith(b"\n"):
                            yield _record(bytes(pending))
                            pending.clear()
                    self.raw_end, self.compressed_end = (
                        raw["end"],
                        frame["compressed_range"]["end"],
                    )
                    self.after_frame, self.outcome = (
                        frame["compressed_range"]["start"],
                        frame["outcome"],
                    )
                self.at_available_end = page["at_available_end"]
                if self.at_available_end:
                    if pending and self.outcome is not None:
                        yield _record(bytes(pending))
                        pending.clear()
                    if pending:
                        self.error = "partial_jsonl_record"
                    return
            self.error = "page_budget_exhausted"
        except (EvidenceUnavailable, OSError, ValueError, KeyError, TypeError) as exc:
            self.error = type(exc).__name__


def _record(body: bytes) -> dict[str, Any] | str:
    record = json.loads(body)
    if isinstance(record, str) and record in {"Ready", "Finished"}:
        return record
    if not isinstance(record, dict):
        raise EvidenceUnavailable("native record is not a LogMsg object")
    return record
