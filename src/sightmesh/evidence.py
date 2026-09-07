"""Typed, bounded reads from cdesktop's execution-evidence owner."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .fence import open_transport


class EvidenceError(RuntimeError): pass
class EvidenceUnavailable(EvidenceError): pass


@dataclass(frozen=True)
class RawRange:
    source_id: str
    start: int
    end: int
    durability: str
    body: bytes


class EvidenceClient:
    """Small client which refuses the seam unless v1 is advertised."""
    def __init__(self, base_url: str, *, opener: Callable[[Request], Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self._opener = opener
        self._enabled: bool | None = None

    def enabled(self) -> bool:
        if self._enabled is None:
            data = self._json("GET", "/api/info")
            self._enabled = data.get("service_capabilities", {}).get("execution_evidence") == 1
        return self._enabled

    def raw(self, execution_id: str, start: int, end: int) -> RawRange:
        if not self.enabled(): raise EvidenceUnavailable("execution evidence v1 is unavailable")
        if not (0 <= start <= end <= start + 1048576): raise ValueError("range must be bounded to 1 MiB")
        with self._open("GET", f"/api/execution-processes/{execution_id}/raw-log", {"start": start, "end": end}) as response:
            body = response.read(end - start + 1)
            if len(body) > end - start or response.read(1):
                raise EvidenceUnavailable("native range exceeds its requested bound")
            headers = response.headers
            source = headers.get("x-cdesktop-source-id")
            actual = headers.get("x-cdesktop-source-range", "")
            durability = headers.get("x-cdesktop-source-durability")
        match = re.fullmatch(r"\[(\d+), (\d+)\)", actual)
        if match is None: raise EvidenceUnavailable("missing or invalid source range")
        actual_start, actual_end = (int(value) for value in match.groups())
        if source != execution_id or durability not in {"confirmed", "unverified"} or actual_start != start or actual_end != start + len(body) or actual_end > end:
            raise EvidenceUnavailable("native evidence identity/range/durability mismatch")
        return RawRange(source, actual_start, actual_end, durability, body)

    def provenance(self, execution_id: str, after_frame: str | int | None = None) -> dict[str, Any]:
        if not self.enabled(): raise EvidenceUnavailable("execution evidence v1 is unavailable")
        query = {} if after_frame is None else {"after_frame": after_frame}
        return self._json("GET", f"/api/execution-processes/{execution_id}/raw-log/provenance", query)

    def artifact(self, execution_id: str, *, publication_key: str, name: str, body: bytes, original_path: str, producer_ref: str) -> dict[str, Any]:
        # Kept injectable at the HTTP boundary in tests; multipart is deliberately
        # small and does not copy an artifact into a local evidence store.
        if not self.enabled(): raise EvidenceUnavailable("execution evidence v1 is unavailable")
        boundary = "sightmesh-evidence"
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"artifact\"; filename=\"{name}\"\r\n\r\n".encode(), body, f"\r\n--{boundary}--\r\n".encode()]
        with self._open("POST", f"/api/execution-processes/{execution_id}/artifacts", {"publication_key": publication_key, "original_path": original_path, "producer_ref": producer_ref}, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}) as response:
            return json.loads(response.read())["data"]

    def artifact_bytes(self, execution_id: str, occurrence_id: str) -> bytes:
        if not self.enabled(): raise EvidenceUnavailable("execution evidence v1 is unavailable")
        with self._open("GET", f"/api/execution-processes/{execution_id}/artifacts/{occurrence_id}/file") as response: return response.read()

    def _json(self, method: str, path: str, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._open(method, path, query) as response: decoded = json.loads(response.read())
        return decoded.get("data", decoded)

    def _open(self, method: str, path: str, query: Mapping[str, Any] | None = None, body: bytes | None = None, headers: Mapping[str, str] | None = None):
        url = self.base_url + path + (("?" + urlencode(query)) if query else "")
        request = Request(url, data=body, method=method, headers=dict(headers or {}))
        return open_transport(self._opener, request) if self._opener else open_transport(urlopen, request)
