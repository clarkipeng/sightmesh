"""Capability-gated, bounded reads from cdesktop's execution-evidence owner."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .fence import open_transport

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_RANGE_BYTES = 1024 * 1024
TRANSFER_BYTES = 64 * 1024


class EvidenceError(RuntimeError):
    pass


class EvidenceUnavailable(EvidenceError):
    pass


@dataclass(frozen=True)
class RawRange:
    source_id: str
    start: int
    end: int
    durability: str
    body: bytes


class EvidenceClient:
    def __init__(
        self, base_url: str, *, opener: Callable[[Request], Any] | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self._opener = opener
        self._enabled: bool | None = None

    def enabled(self) -> bool:
        if self._enabled is None:
            capabilities = self._json("GET", "/api/info").get(
                "service_capabilities", {}
            )
            if not isinstance(capabilities, dict):
                raise EvidenceUnavailable("invalid native capability envelope")
            version = capabilities.get("execution_evidence")
            self._enabled = type(version) is int and version == 1
        return self._enabled

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise EvidenceUnavailable("execution evidence v1 is unavailable")

    @staticmethod
    def _execution_path(execution_id: str) -> str:
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError("execution identity must not be empty")
        return f"/api/execution-processes/{quote(execution_id, safe='')}"

    def raw(self, execution_id: str, start: int, end: int) -> RawRange:
        self._require_enabled()
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= start + MAX_RANGE_BYTES
        ):
            raise ValueError("range must be integer bytes bounded to 1 MiB")
        with self._open(
            "GET",
            self._execution_path(execution_id) + "/raw-log",
            {"start": start, "end": end},
        ) as response:
            body = response.read(end - start + 1)
            if len(body) > end - start or response.read(1):
                raise EvidenceUnavailable("native range exceeds its requested bound")
            source = response.headers.get("x-cdesktop-source-id")
            actual = response.headers.get("x-cdesktop-source-range", "")
            durability = response.headers.get("x-cdesktop-source-durability")
        match = re.fullmatch(r"\[(\d+), (\d+)\)", actual)
        if match is None:
            raise EvidenceUnavailable("missing or invalid source range")
        actual_start, actual_end = (int(value) for value in match.groups())
        if (
            source != execution_id
            or durability not in {"confirmed", "unverified"}
            or actual_start != start
            or actual_end != start + len(body)
            or actual_end > end
        ):
            raise EvidenceUnavailable(
                "native evidence identity/range/durability mismatch"
            )
        return RawRange(source, actual_start, actual_end, durability, body)

    def provenance(
        self, execution_id: str, after_frame: int | None = None
    ) -> dict[str, Any]:
        self._require_enabled()
        if after_frame is not None and (
            type(after_frame) is not int or after_frame < 0
        ):
            raise ValueError("native frame cursor must be a nonnegative integer")
        query = {} if after_frame is None else {"after_frame": after_frame}
        return self._json(
            "GET", self._execution_path(execution_id) + "/raw-log/provenance", query
        )

    def artifact(
        self,
        execution_id: str,
        *,
        publication_key: str,
        name: str,
        body: bytes,
        original_path: str,
        producer_ref: str,
    ) -> dict[str, Any]:
        self._require_enabled()
        if (
            not isinstance(body, bytes)
            or not name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise ValueError("artifact requires bytes and a safe original filename")
        boundary = uuid.uuid4().hex
        while boundary.encode() in body:
            boundary = uuid.uuid4().hex
        filename = name.replace("\\", "\\\\").replace('"', '\\"')

        def multipart() -> Iterator[bytes]:
            yield f'--{boundary}\r\nContent-Disposition: form-data; name="artifact"; filename="{filename}"\r\n\r\n'.encode()
            for start in range(0, len(body), TRANSFER_BYTES):
                yield body[start : start + TRANSFER_BYTES]
            yield f"\r\n--{boundary}--\r\n".encode()

        with self._open(
            "POST",
            self._execution_path(execution_id) + "/artifacts",
            {
                "publication_key": publication_key,
                "original_path": original_path,
                "producer_ref": producer_ref,
            },
            multipart(),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        ) as response:
            return self._decode(response)

    def artifact_chunks(
        self, execution_id: str, occurrence_id: str, chunk_size: int = TRANSFER_BYTES
    ) -> Iterator[bytes]:
        if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_RANGE_BYTES:
            raise ValueError("chunk size must be bounded")
        self._require_enabled()
        path = (
            self._execution_path(execution_id)
            + f"/artifacts/{quote(occurrence_id, safe='')}/file"
        )
        with self._open("GET", path) as response:
            while chunk := response.read(chunk_size):
                if len(chunk) > chunk_size:
                    raise EvidenceUnavailable(
                        "artifact transport exceeded requested chunk bound"
                    )
                yield chunk

    def artifact_receipt(self, execution_id: str, occurrence_id: str) -> dict[str, Any]:
        self._require_enabled()
        return self._json(
            "GET",
            self._execution_path(execution_id)
            + f"/artifacts/{quote(occurrence_id, safe='')}",
        )

    @staticmethod
    def _decode(response: Any) -> dict[str, Any]:
        body = response.read(MAX_JSON_BYTES + 1)
        if len(body) > MAX_JSON_BYTES or response.read(1):
            raise EvidenceUnavailable("native metadata exceeds consumer bound")
        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise EvidenceUnavailable("invalid native JSON metadata") from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("success") is not True
            or not isinstance(envelope.get("data"), dict)
        ):
            raise EvidenceUnavailable(
                "invalid or unsuccessful native metadata envelope"
            )
        return envelope["data"]

    def _json(
        self, method: str, path: str, query: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._open(method, path, query) as response:
            return self._decode(response)

    def _open(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        body: bytes | Iterable[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        url = self.base_url + path + (("?" + urlencode(query)) if query else "")
        request = Request(url, data=body, method=method, headers=dict(headers or {}))
        if self._opener is not None:
            return open_transport(self._opener, request)
        return open_transport(urlopen, request, timeout=15)
