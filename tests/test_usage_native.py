from __future__ import annotations

import json

import pytest

from sightmesh.evidence_stream import MAX_FRAME_BYTES, MAX_RECORD_BYTES
from sightmesh.usage import derive, derive_native
from test_evidence_index_native import EXECUTION, NativeWire


def log(event):
    return {"Stdout": json.dumps(event)}


def codex(turn="turn-1", last=12, total=112, **changes):
    def tokens(n):
        return {
            "inputTokens": n - 2,
            "outputTokens": 2,
            "cachedInputTokens": 5,
            "reasoningOutputTokens": 1,
            "totalTokens": n,
        }

    params = {
        "threadId": "thread-1",
        "turnId": turn,
        "tokenUsage": {
            "last": tokens(last),
            "total": tokens(total),
            "modelContextWindow": 200000,
        },
    }
    params.update(changes)
    return log(
        {"jsonrpc": "2.0", "method": "thread/tokenUsage/updated", "params": params}
    )


def claude_usage(**changes):
    return {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 40,
        **changes,
    }


def result(**changes):
    return log(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-1",
            "usage": claude_usage(),
            **changes,
        }
    )


def assistant(message="message-1", **changes):
    return log(
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {"id": message, "usage": claude_usage()},
            **changes,
        }
    )


def sample(report, scope, identity=None):
    return next(
        s
        for s in report.samples
        if s.scope == scope and (identity is None or s.identity == identity)
    )


def wire_records(records, *, split=97, outcome="complete"):
    body = b"".join(json.dumps(r, ensure_ascii=False).encode() + b"\n" for r in records)
    wire = NativeWire(
        [body[i : i + split] for i in range(0, len(body), split)], page_size=2
    )
    wire.append(b"", outcome=outcome)
    return wire


def test_codex_replaces_snapshots_without_summing_thread_history_or_cache_twice():
    records = [
        "Ready",
        codex(),
        codex(last=15, total=115),
        codex(last=15, total=115),
        codex(turn="turn-2", last=22, total=137),
        {"JsonPatch": []},
        "Finished",
    ]
    wire = wire_records(records)
    report = derive_native(wire.client(), EXECUTION)
    assert report.complete and not report.warnings
    assert (
        report.execution_tokens is None
    )  # thread lifetime includes an unknown prior baseline
    assert len(report.samples) == 3
    assert (
        sample(report, "last_request_snapshot", ("thread-1", "turn-1")).tokens.total
        == 15
    )
    latest = sample(report, "thread_lifetime").tokens
    assert (
        latest.input,
        latest.output,
        latest.cache_read,
        latest.reasoning,
        latest.total,
    ) == (135, 2, 5, 1, 137)
    assert report.raw_end == len(wire.body)


def test_claude_message_duplicates_placeholder_output_and_result_scopes():
    wire = wire_records(
        [assistant(), assistant(), assistant("message-2"), result(), result()]
    )
    report = derive_native(wire.client(), EXECUTION)
    assert len(report.samples) == 3
    assert sample(report, "message_input").tokens.output is None
    assert sample(report, "message_input").tokens.total is None
    assert report.execution_tokens == 100  # result only, not result plus per-step input
    assert (
        derive([result()]).execution_tokens is None
    )  # no execution identity/capture proof


def test_claude_partial_output_snapshots_replace_and_do_not_count_as_another_result():
    def partial(event):
        return log({"type": "stream_event", "parent_tool_use_id": None, "event": event})

    events = [
        partial({"type": "message_start", "message": {"id": "msg-1"}}),
        partial({"type": "message_delta", "usage": {"output_tokens": 3}}),
        partial({"type": "message_delta", "usage": {"output_tokens": 9}}),
        partial({"type": "message_stop"}),
        result(),
    ]
    report = derive_native(wire_records(events).client(), EXECUTION)
    assert sample(report, "message_stream_output").tokens.output == 9
    assert report.execution_tokens == 100


@pytest.mark.parametrize(
    "missing",
    [
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ],
)
def test_absent_claude_categories_remain_unknown(missing):
    usage = claude_usage()
    usage.pop(missing)
    report = derive_native(wire_records([result(usage=usage)]).client(), EXECUTION)
    assert report.complete and report.execution_tokens is None


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "7", 2**64])
def test_invalid_counts_never_become_known_tokens(invalid):
    report = derive_native(
        wire_records([result(usage=claude_usage(output_tokens=invalid))]).client(),
        EXECUTION,
    )
    assert sample(report, "execution_result").tokens.output is None
    assert report.execution_tokens is None


def test_unknown_identity_is_visible_and_cumulative_reset_is_not_a_negative_delta():
    report = derive([codex(turnId=None), codex(total=90), codex(total=70)])
    assert {"usage_identity_unavailable", "thread_total_decreased_or_reordered"} <= set(
        report.warnings
    )
    assert any(s.identity is None for s in report.samples)
    assert sample(report, "thread_lifetime").tokens.total == 70
    assert report.execution_tokens is None


def test_failure_results_and_unknown_usage_do_not_claim_execution_completeness():
    report = derive_native(
        wire_records(
            [
                result(subtype="error_during_execution", is_error=True),
                log({"type": "other", "usage": {"tokens": 7}}),
            ]
        ).client(),
        EXECUTION,
    )
    assert report.complete  # native capture is complete; accounting is a different fact
    assert report.execution_tokens is None
    assert {"error_result_may_omit_usage", "unsupported_provider_usage"} <= set(
        report.warnings
    )


def test_native_partial_unavailable_legacy_and_page_budget_are_explicit():
    wire = wire_records([result()], outcome="legacy_unknown")
    legacy = derive_native(wire.client(), EXECUTION)
    assert legacy.capture_outcome == "legacy_unknown" and not legacy.complete
    assert legacy.execution_tokens is None
    budget = derive_native(wire.client(), EXECUTION, max_pages=1)
    assert budget.source_error == "page_budget_exhausted"
    wire.fail_after = 100
    unavailable = derive_native(wire.client(), EXECUTION)
    assert (
        unavailable.source_error == "EvidenceUnavailable" and not unavailable.complete
    )
    live = derive_native(NativeWire([b'{"Stdout": "partial']).client(), EXECUTION)
    assert live.source_error == "partial_jsonl_record" and not live.complete


def test_jsonl_record_bound_and_malformed_data_refuse_complete_coverage():
    body = b'{"Stdout":"' + b"x" * MAX_RECORD_BYTES
    wire = NativeWire(
        [body[i : i + MAX_FRAME_BYTES] for i in range(0, len(body), MAX_FRAME_BYTES)]
    )
    wire.append(b"", outcome="complete")
    assert derive_native(wire.client(), EXECUTION).source_error == "EvidenceUnavailable"
    malformed = NativeWire([b"not-json\n"])
    malformed.append(b"", outcome="complete")
    assert (
        derive_native(malformed.client(), EXECUTION).source_error == "JSONDecodeError"
    )


def test_reader_preserves_utf8_split_inside_native_jsonl():
    records = [log({"type": "message", "text": "中🙂文"}), result()]
    wire = wire_records(records, split=1)
    report = derive_native(wire.client(), EXECUTION, max_pages=4096)
    assert report.complete and report.execution_tokens == 100
