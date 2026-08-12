from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def routing_path() -> Path:
    return Path.home() / ".config" / "agent-deck" / "bridge.json"


def enabled_workspaces() -> set[str]:
    data = _read()
    values = data.get("enabled_workspaces", [])
    return {value for value in values if isinstance(value, str)}


def _read() -> dict[str, object]:
    path = routing_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, object]) -> None:
    path = routing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="bridge.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def enable(workspace_id: str) -> None:
    data = _read()
    values = enabled_workspaces()
    values.add(workspace_id)
    data["enabled_workspaces"] = sorted(values)
    _write(data)


def disable(workspace_id: str) -> None:
    data = _read()
    values = enabled_workspaces()
    values.discard(workspace_id)
    data["enabled_workspaces"] = sorted(values)
    _write(data)


def peer_identity(session_id: str) -> str | None:
    values = _read().get("peer_ids", {})
    if not isinstance(values, dict):
        return None
    value = values.get(session_id)
    return value if isinstance(value, str) and value else None


def set_peer_identity(session_id: str, peer_id: str) -> None:
    data = _read()
    values = data.get("peer_ids", {})
    peers = dict(values) if isinstance(values, dict) else {}
    peers[session_id] = peer_id
    data["peer_ids"] = peers
    _write(data)


def clear_peer_identity(session_id: str) -> None:
    data = _read()
    values = data.get("peer_ids", {})
    peers = dict(values) if isinstance(values, dict) else {}
    peers.pop(session_id, None)
    data["peer_ids"] = peers
    _write(data)
