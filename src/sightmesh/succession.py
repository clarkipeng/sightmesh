"""Durable session ownership: explicit retirement, quarantine, succession.

Ordinary process completion or failure is never retirement: an active manager
whose turn completed stays resumable for callbacks and recovery. A session
becomes undeliverable only through an explicit ownership transition recorded
here (retired or superseded). The terminal record is persisted before any
other side effect, so a crash mid-transfer still leaves the session
quarantined and a later reconcile sweep finishes cancellation - delivery can
never race a superseded session back into a shared worktree.

Nothing in this module reads or stores credentials. Successor linkage carries
only opaque session ids and command dedupe keys.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import execution_routing
from .pool import core as pool_core

OWNERSHIP_VERSION = 1

RETIRED = "retired"
SUPERSEDED = "superseded"
TERMINAL_STATES = {RETIRED, SUPERSEDED}

COMMAND_TERMINAL_STATES = {"done", "failed", "cancelled"}


class SuccessionError(RuntimeError):
    pass


class QuarantinedSessionError(SuccessionError):
    def __init__(self, record: TerminalOwnership) -> None:
        self.record = record
        successor = (
            f"; successor is {record.successor_session_id}"
            if record.successor_session_id
            else "; no successor recorded"
        )
        super().__init__(
            f"Session {record.session_id} is {record.state} "
            f"({record.reason}) and can never be resumed{successor}"
        )


def default_ownership_path() -> Path:
    return Path.home() / ".local" / "state" / "sightmesh" / "ownership.json"


@dataclass(frozen=True)
class TerminalOwnership:
    session_id: str
    state: str
    reason: str
    retired_at: str
    logical_key: str | None = None
    successor_session_id: str | None = None

    def __post_init__(self) -> None:
        if self.state not in TERMINAL_STATES:
            raise SuccessionError(f"Unsupported terminal state: {self.state}")
        if self.successor_session_id == self.session_id:
            raise SuccessionError(
                f"Session {self.session_id} cannot be its own successor"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OwnershipStore:
    """Durable, atomically written record of terminal session ownership."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_ownership_path()

    def get(self, session_id: str) -> TerminalOwnership | None:
        row = self._read().get(str(session_id))
        if not isinstance(row, dict):
            return None
        try:
            return TerminalOwnership(**row)
        except (TypeError, SuccessionError) as exc:
            raise SuccessionError(
                f"Corrupt ownership record for session {session_id}: {exc}"
            ) from exc

    def is_quarantined(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    def assert_deliverable(self, session_id: str) -> None:
        record = self.get(session_id)
        if record is not None:
            raise QuarantinedSessionError(record)

    def retire(
        self,
        session_id: str,
        *,
        state: str = RETIRED,
        reason: str,
        logical_key: str | None = None,
    ) -> TerminalOwnership:
        """Persist the terminal transition. First terminal record wins forever."""
        existing = self.get(session_id)
        if existing is not None:
            return existing
        record = TerminalOwnership(
            session_id=str(session_id),
            state=state,
            reason=reason,
            retired_at=datetime.now(UTC).isoformat(),
            logical_key=logical_key,
        )
        self._put(record)
        return record

    def link_successor(
        self, session_id: str, successor_session_id: str
    ) -> TerminalOwnership:
        record = self.get(session_id)
        if record is None:
            raise SuccessionError(
                f"Session {session_id} has no terminal ownership record to link"
            )
        if record.successor_session_id == successor_session_id:
            return record
        if record.successor_session_id is not None:
            raise SuccessionError(
                f"Session {session_id} already has successor "
                f"{record.successor_session_id}; refusing {successor_session_id}"
            )
        updated = replace(record, successor_session_id=str(successor_session_id))
        self._put(updated)
        return updated

    def _put(self, record: TerminalOwnership) -> None:
        sessions = self._read()
        sessions[record.session_id] = record.to_dict()
        self._write(sessions)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SuccessionError(
                f"Cannot read ownership state {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != OWNERSHIP_VERSION:
            raise SuccessionError(f"Unsupported ownership state version: {self.path}")
        sessions = payload.get("sessions")
        return dict(sessions) if isinstance(sessions, dict) else {}

    def _write(self, sessions: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=".ownership.", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(
                    {"version": OWNERSHIP_VERSION, "sessions": sessions},
                    stream,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)


def resolve_live_successor(store: OwnershipStore, session_id: str) -> str | None:
    """Follow the successor chain to the first non-quarantined session."""
    seen = {str(session_id)}
    current = store.get(session_id)
    while current is not None:
        successor = current.successor_session_id
        if not successor or successor in seen:
            return None
        seen.add(successor)
        record = store.get(successor)
        if record is None:
            return successor
        current = record
    return str(session_id)


# ---------------------------------------------------------------- handoff


@dataclass(frozen=True)
class HandoffResult:
    source_session_id: str
    successor_session_id: str
    logical_key: str
    spawned: bool
    forwarded_commands: int
    cancelled_commands: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def transfer_ownership(
    client: Any,
    store: OwnershipStore,
    *,
    source_session_id: str,
    spawn: Callable[[], str],
    reason: str,
    logical_key: str | None = None,
) -> HandoffResult:
    """Quarantine the source and hand its live intent to exactly one successor.

    Safe to re-run after a crash at any point: the terminal record is written
    first, the successor is spawned only while none is recorded, forwarding
    reuses each command's dedupe key so cdesktop's idempotent enqueue collapses
    duplicates, and source commands are cancelled last so an interrupted run
    can still re-read and forward them.
    """
    source = str(source_session_id)
    key = logical_key or f"handoff:{source}"
    record = store.retire(source, state=SUPERSEDED, reason=reason, logical_key=key)
    key = record.logical_key or key

    open_commands = [
        row
        for row in client.session_commands(source)
        if str(row.get("state") or row.get("status") or "pending")
        not in COMMAND_TERMINAL_STATES
    ]

    if record.successor_session_id:
        successor, spawned = record.successor_session_id, False
    else:
        successor = str(spawn())
        if not successor:
            raise SuccessionError("Successor spawn did not return a session id")
        store.link_successor(source, successor)
        spawned = True
    store.assert_deliverable(successor)

    forwarded = 0
    for row in open_commands:
        body = str(row.get("body") or row.get("prompt") or "")
        if not body:
            continue
        dedupe_key = str(row.get("dedupe_key") or f"{key}:command:{row.get('id')}")
        client.send(successor, body, None, dedupe_key=dedupe_key, intent="continue")
        forwarded += 1

    cancelled = 0
    for row in open_commands:
        client.interrupt_command(str(row["id"]))
        cancelled += 1

    return HandoffResult(
        source_session_id=source,
        successor_session_id=successor,
        logical_key=key,
        spawned=spawned,
        forwarded_commands=forwarded,
        cancelled_commands=cancelled,
    )


# ---------------------------------------------------------------- quota reroute


def reroute_after_quota_exhaustion(
    settings: execution_routing.ExecutionRoutingSettings,
    *,
    exhausted_binding_id: str,
    preferred_model: str | None = None,
    cooldown_seconds: int | None = None,
    retry_at: float | None = None,
) -> execution_routing.SelectionResult:
    """Durably cool the exhausted binding, then pick the next route without it.

    The cooldown lives in pool state, the single source of account truth, so
    every later selection - including one after a restart - observes it. Only
    the opaque binding id is touched; credentials are never read here.
    """
    if retry_at is not None:
        pool_core.cool_until_timestamp(exhausted_binding_id, max(retry_at, time.time()))
    elif cooldown_seconds is not None:
        pool_core.set_cooldown(exhausted_binding_id, cooldown_seconds)
    else:
        pool_core.set_cooldown(exhausted_binding_id)
    return execution_routing.select_route(
        settings,
        preferred_model=preferred_model,
        exclude_account_ids=frozenset({exhausted_binding_id}),
    )
