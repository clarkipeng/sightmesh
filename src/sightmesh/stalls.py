from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .cdesktop import CdesktopClient, CdesktopError
from .stall_settings import threshold_minutes

LOGGER = logging.getLogger("sightmesh.stalls")

def threshold_from_environment() -> timedelta:
    """Return the bounded, operator-configurable no-event threshold."""
    return timedelta(minutes=threshold_minutes())


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _latest_event_time(value: object) -> datetime | None:
    latest: datetime | None = None
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"timestamp", "created_at", "updated_at", "time"}:
                candidate = _parse_time(item)
                if candidate and (latest is None or candidate > latest):
                    latest = candidate
            nested = _latest_event_time(item)
            if nested and (latest is None or nested > latest):
                latest = nested
    elif isinstance(value, list):
        for item in value:
            candidate = _latest_event_time(item)
            if candidate and (latest is None or candidate > latest):
                latest = candidate
    return latest


def _has_active_child(value: object) -> bool:
    """Recognize live command/tool records without maintaining a command allowlist."""
    if isinstance(value, dict):
        state = str(value.get("status") or value.get("state") or "").lower()
        is_process_like = any(
            key in value
            for key in ("command", "tool", "tool_name", "process", "pid", "child_process")
        )
        if is_process_like and state in {"running", "active", "in_progress", "executing"}:
            return True
        return any(_has_active_child(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_active_child(item) for item in value)
    return False


def _event_signature(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot.get("entries", []), sort_keys=True, separators=(",", ":"))


@dataclass
class _ObservedProcess:
    signature: str
    last_event_at: datetime


class StallDetector:
    """Detect one truly idle execution and hand it to native recovery exactly once."""

    def __init__(
        self,
        *,
        threshold: timedelta | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.threshold = threshold or threshold_from_environment()
        self.now = now or (lambda: datetime.now(UTC))
        self.observed: dict[str, _ObservedProcess] = {}
        self.recovering: set[str] = set()
        self.notified: set[str] = set()

    def reconcile(self, client: CdesktopClient, session: dict[str, Any]) -> None:
        parent_session_id = session.get("parent_session_id")
        if not isinstance(parent_session_id, str) or not parent_session_id:
            return
        session_id = str(session["id"])
        processes = client.execution_processes(session_id)
        running_ids = {
            str(process["id"])
            for process in processes
            if process.get("status") == "running"
            and process.get("run_reason") != "devserver"
        }
        for process_id in set(self.observed) - running_ids:
            self.observed.pop(process_id, None)
            self.recovering.discard(process_id)
            self.notified.discard(process_id)

        for process in processes:
            if str(process.get("id")) not in running_ids:
                continue
            self._reconcile_process(client, session, parent_session_id, process)

    def _reconcile_process(
        self,
        client: CdesktopClient,
        session: dict[str, Any],
        parent_session_id: str,
        process: dict[str, Any],
    ) -> None:
        process_id = str(process["id"])
        try:
            snapshot = client.normalized_snapshot(process_id)
        except CdesktopError as exc:
            LOGGER.warning("Cannot inspect execution %s for stalls: %s", process_id, exc)
            return
        now = self.now()
        signature = _event_signature(snapshot)
        recorded_event = _latest_event_time(snapshot)
        observed = self.observed.get(process_id)
        if observed is None:
            # The first read is a warm baseline, never stale evidence. cdesktop
            # leaves a running stream partial until it emits Finished, so using
            # process start time here would kill a cold active stream at once.
            self.observed[process_id] = _ObservedProcess(
                signature=signature,
                last_event_at=recorded_event or now,
            )
            return
        if signature != observed.signature or _has_active_child(snapshot):
            observed.signature = signature
            observed.last_event_at = recorded_event or now
            return
        if recorded_event and recorded_event > observed.last_event_at:
            observed.last_event_at = recorded_event
            return
        if now - observed.last_event_at < self.threshold:
            return

        # This is deliberately the normal killed-child path. cdesktop releases
        # the claimed command and dispatches its durable recovery work itself.
        if process_id not in self.recovering:
            client.stop_execution(process_id)
            self.recovering.add(process_id)
        if process_id in self.notified:
            return
        dedupe_key = f"stall:{process_id}:parent"
        client.send(
            parent_session_id,
            "STALL: child execution produced no session events past the configured "
            "threshold and was handed to native killed-child recovery.",
            str(session["id"]),
            dedupe_key=dedupe_key,
        )
        self.notified.add(process_id)
        LOGGER.warning(
            "Marked session %s stalled through execution %s and notified parent %s",
            session["id"],
            process_id,
            parent_session_id,
        )
