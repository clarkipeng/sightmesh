"""Derive scoped usage observations from original native LogMsg records.

No prices, guessed request identities, or sums across incompatible scopes.
Codex `last` is a request snapshot and `total` is thread lifetime, not execution
consumption. Claude assistant output is a placeholder; only result/stream
observations report output. See docs/execution-evidence.md for provider contracts.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .evidence import EvidenceClient
from .evidence_stream import MAX_RECORD_BYTES, NativeRecords


@dataclass(frozen=True)
class Tokens:
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None
    reasoning: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class UsageSample:
    provider: str
    scope: str
    identity: tuple[str, ...] | None
    tokens: Tokens
    parent_tool_use_id: str | None = None


@dataclass(frozen=True)
class UsageReport:
    samples: tuple[UsageSample, ...]
    warnings: tuple[str, ...]
    source_id: str | None = None
    raw_end: int = 0
    after_frame: int | None = None
    capture_outcome: str | None = None
    at_available_end: bool = False
    source_error: str | None = None
    derivation_error: str | None = None

    @property
    def complete(self) -> bool:
        return (
            self.capture_outcome == "complete"
            and self.at_available_end
            and not self.source_error
        )

    @property
    def derivation_complete(self) -> bool:
        """Whether provider JSONL was fully and boundedly derived from capture."""
        return self.complete and not self.derivation_error

    @property
    def execution_tokens(self) -> int | None:
        """Only an explicit complete Claude main-loop result can establish this.

        This is not a billed total, and excludes any separately scoped subagent
        work. Unknown provider/capture semantics are never represented as zero.
        """
        results = [s for s in self.samples if s.scope == "execution_result"]
        if not self.derivation_complete or self.warnings or len(results) != 1:
            return None
        return results[0].tokens.total


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**64 - 1 else None


def _identity(*parts: Any) -> tuple[str, ...] | None:
    return tuple(parts) if all(isinstance(p, str) and p for p in parts) else None


def _codex(value: Any) -> Tokens:
    data = _mapping(value)
    # Cache read is included in input; reasoning is included in output.
    return Tokens(
        *(
            _count(data.get(k))
            for k in (
                "inputTokens",
                "outputTokens",
                "cachedInputTokens",
                "cacheCreationTokens",
                "reasoningOutputTokens",
                "totalTokens",
            )
        )
    )


def _claude(value: Any, *, output: bool) -> Tokens:
    data = _mapping(value)
    counts = [
        _count(data.get(k))
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    ]
    if not output:
        counts[1] = None
    total = sum(counts) if all(c is not None for c in counts) else None
    return Tokens(*counts, total=total)


def derive(
    records: Iterable[Mapping[str, Any] | str], *, source_id: str | None = None
) -> UsageReport:
    """Read serialized LogMsg shapes, keeping latest observations by true identity.

    Unknown identities remain visible as unattributed samples; they cannot enter
    an aggregate. JsonPatch/UI replacements are intentionally not usage sources.
    """
    samples: dict[tuple[Any, ...], UsageSample] = {}
    warnings: set[str] = set()
    stream_messages: dict[str | None, str] = {}
    pending_stdout = ""
    discarding_stdout = False
    derivation_error: str | None = None

    def provider_error(error: str) -> None:
        nonlocal derivation_error
        if derivation_error is None:
            derivation_error = error

    def save(sample: UsageSample) -> None:
        if sample.identity is None:
            warnings.add("usage_identity_unavailable")
            key = ("unattributed", len(samples))
        else:
            key = (sample.provider, sample.scope, *sample.identity)
        previous = samples.get(key)
        if previous is not None and sample.scope == "thread_lifetime":
            before, after = previous.tokens.total, sample.tokens.total
            if before is not None and after is not None and after < before:
                warnings.add("thread_total_decreased_or_reordered")
        samples[key] = sample

    def consume_provider_line(line: str) -> None:
        try:
            event = _mapping(json.loads(line))
        except (ValueError, TypeError):
            if line.lstrip().startswith("{"):
                warnings.add("unparsed_provider_json")
                provider_error("malformed_provider_json")
            return
        consume_event(event)

    def consume_event(event: Mapping[str, Any]) -> None:
        nonlocal stream_messages
        if event.get("method") == "thread/tokenUsage/updated":
            params = _mapping(event.get("params"))
            usage = _mapping(params.get("tokenUsage"))
            save(
                UsageSample(
                    "codex",
                    "last_request_snapshot",
                    _identity(params.get("threadId"), params.get("turnId")),
                    _codex(usage.get("last")),
                )
            )
            save(
                UsageSample(
                    "codex",
                    "thread_lifetime",
                    _identity(params.get("threadId")),
                    _codex(usage.get("total")),
                )
            )
            return
        parent = event.get("parent_tool_use_id")
        if parent is not None and not isinstance(parent, str):
            warnings.add("invalid_parent_identity")
            return
        kind = event.get("type")
        if kind == "assistant":
            message = _mapping(event.get("message"))
            save(
                UsageSample(
                    "claude",
                    "message_input",
                    _identity(message.get("id")),
                    _claude(message.get("usage"), output=False),
                    parent,
                )
            )
        elif kind == "result":
            tokens = _claude(event.get("usage"), output=True)
            if event.get("subtype") != "success" or event.get("is_error") is True:
                warnings.add("error_result_may_omit_usage")
                tokens = replace(tokens, total=None)
            save(
                UsageSample(
                    "claude",
                    "execution_result",
                    _identity(source_id),
                    tokens,
                    parent,
                )
            )
        elif kind == "stream_event":
            partial = _mapping(event.get("event"))
            if partial.get("type") == "message_start":
                message = _mapping(partial.get("message"))
                message_id = message.get("id")
                if _identity(message_id) is not None:
                    stream_messages[parent] = message_id
            elif partial.get("type") == "message_delta":
                # These are cumulative output snapshots, not additive deltas.
                save(
                    UsageSample(
                        "claude",
                        "message_stream_output",
                        _identity(stream_messages.get(parent)),
                        Tokens(
                            output=_count(
                                _mapping(partial.get("usage")).get("output_tokens")
                            )
                        ),
                        parent,
                    )
                )
            elif partial.get("type") == "message_stop":
                stream_messages.pop(parent, None)
        elif "usage" in event or "tokenUsage" in event:
            warnings.add("unsupported_provider_usage")

    def consume_stdout(chunk: str) -> None:
        nonlocal pending_stdout, discarding_stdout
        while chunk:
            if discarding_stdout:
                _, newline, chunk = chunk.partition("\n")
                if not newline:
                    return
                discarding_stdout = False
                continue
            part, newline, chunk = chunk.partition("\n")
            pending_stdout += part
            if len(pending_stdout.encode()) > MAX_RECORD_BYTES:
                provider_error("provider_jsonl_record_exceeds_bound")
                pending_stdout = ""
                if not newline:
                    discarding_stdout = True
                    return
            elif newline:
                consume_provider_line(pending_stdout.rstrip("\r"))
                pending_stdout = ""
            else:
                return

    for record in records:
        if isinstance(record, str) and record in {"Ready", "Finished"}:
            continue
        outer = _mapping(record)
        if len(outer) != 1:
            warnings.add("unsupported_native_record")
            continue
        if "Stdout" not in outer:
            if not set(outer) <= {"Stderr", "JsonPatch", "SessionId", "MessageId"}:
                warnings.add("unsupported_native_record")
            continue
        stdout = outer["Stdout"]
        if not isinstance(stdout, str):
            warnings.add("invalid_native_stdout")
            continue
        consume_stdout(stdout)
    if pending_stdout or discarding_stdout:
        provider_error("partial_provider_jsonl_record")
    return UsageReport(
        tuple(samples.values()), tuple(sorted(warnings)), source_id,
        derivation_error=derivation_error,
    )


def derive_native(
    client: EvidenceClient, execution_id: str, *, max_pages: int = 128
) -> UsageReport:
    records = NativeRecords(client, execution_id, max_pages=max_pages)
    report = derive(records, source_id=execution_id)
    return replace(
        report,
        raw_end=records.raw_end,
        after_frame=records.after_frame,
        capture_outcome=records.outcome,
        at_available_end=records.at_available_end,
        source_error=records.error,
    )
