from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RepowireError(RuntimeError):
    pass


def auth_token() -> str | None:
    return os.environ.get("REPOWIRE_AUTH_TOKEN") or None


def reply(
    correlation_id: str,
    message: str,
    *,
    from_peer: str,
    question: bool,
    base_url: str = "http://127.0.0.1:8377",
) -> Any:
    endpoint = "/answer" if question else "/ack"
    payload = {"correlation_id": correlation_id}
    if question:
        payload["text"] = message
    else:
        payload.update({"message": message, "from_peer": from_peer})
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RepowireError(f"POST {endpoint} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RepowireError(f"Cannot reach Repowire at {base_url}: {exc}") from exc
    return json.loads(raw) if raw else None
