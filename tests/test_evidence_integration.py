"""HTTP seam regressions; end-to-end retention/index/usage tests live beside their owners."""

from __future__ import annotations

import io
import json

import pytest

from sightmesh.evidence import EvidenceClient, EvidenceUnavailable
from sightmesh.evidence import MAX_JSON_BYTES


class Response(io.BytesIO):
    def __init__(self, body, headers=()):
        super().__init__(body)
        self.headers = dict(headers)


def capability():
    return Response(
        json.dumps(
            {
                "success": True,
                "data": {"service_capabilities": {"execution_evidence": 1}},
            }
        ).encode()
    )


def test_raw_range_reads_the_native_bracket_header_and_rejects_wrong_identity():
    replies = [
        capability(),
        Response(
            b"abc",
            [
                ("x-cdesktop-source-id", "exec"),
                ("x-cdesktop-source-range", "[4, 7)"),
                ("x-cdesktop-source-durability", "confirmed"),
            ],
        ),
    ]
    client = EvidenceClient("http://test", opener=lambda _: replies.pop(0))
    assert client.raw("exec", 4, 8).body == b"abc"
    bad = [
        capability(),
        Response(
            b"x",
            [
                ("x-cdesktop-source-id", "other"),
                ("x-cdesktop-source-range", "[0, 1)"),
                ("x-cdesktop-source-durability", "confirmed"),
            ],
        ),
    ]
    with pytest.raises(EvidenceUnavailable):
        EvidenceClient("http://test", opener=lambda _: bad.pop(0)).raw("exec", 0, 1)


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"success":false,"data":{}}',
        b'{"success":true,"data":[]}',
        b"{" + b"x" * MAX_JSON_BYTES,
    ],
)
def test_native_json_is_bounded_and_requires_the_real_success_envelope(body):
    response = Response(body)
    with pytest.raises(EvidenceUnavailable):
        EvidenceClient("http://test", opener=lambda _: response).enabled()
    assert response.closed


def test_boolean_capability_does_not_enable_native_retention():
    response = Response(
        b'{"success":true,"data":{"service_capabilities":{"execution_evidence":true}}}'
    )
    assert not EvidenceClient("http://test", opener=lambda _: response).enabled()
