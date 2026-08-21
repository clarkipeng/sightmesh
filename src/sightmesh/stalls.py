from __future__ import annotations

from datetime import timedelta
from typing import Any

from .stall_settings import threshold_minutes


def threshold_from_environment() -> timedelta:
    """Return the bounded, operator-configurable no-event threshold."""
    return timedelta(minutes=threshold_minutes())


def _has_active_child(value: object) -> bool:
    """Recognize live command/tool records without maintaining a command allowlist."""
    if isinstance(value, dict):
        state = str(value.get("status") or value.get("state") or "").lower()
        is_process_like = any(
            key in value
            for key in (
                "command",
                "tool",
                "tool_name",
                "process",
                "pid",
                "child_process",
            )
        )
        if is_process_like and state in {
            "running",
            "active",
            "in_progress",
            "executing",
        }:
            return True
        return any(_has_active_child(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_active_child(item) for item in value)
    return False


def is_active_suite_work(snapshot: dict[str, Any]) -> bool:
    """Return true when a live child/tool keeps a suite turn active."""
    return _has_active_child(snapshot)
