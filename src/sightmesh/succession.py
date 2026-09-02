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
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import execution_routing
from .escalation import EscalationStore, escalate, redact_credentials
from .pool import core as pool_core

OWNERSHIP_VERSION = 1

RETIRED = "retired"
SUPERSEDED = "superseded"
TERMINAL_STATES = {RETIRED, SUPERSEDED}

COMMAND_TERMINAL_STATES = {"done", "failed", "cancelled"}

# Enough captured failure text to diagnose the route, bounded so a runaway
# provider dump cannot become the escalation body.
FREE_FAILURE_DETAIL_LIMIT = 400


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
    """Durable terminal ownership in the escalation SQLite store.

    ``path`` remains the legacy JSON location solely so an existing install is
    migrated on first open; new ownership records never write JSON.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_ownership_path()
        self._store = EscalationStore()
        self._migrate_legacy_file()

    def get(self, session_id: str) -> TerminalOwnership | None:
        try:
            with self._store._connect() as conn:
                row = conn.execute(
                    "SELECT session_id, state, reason, retired_at, logical_key, "
                    "successor_session_id FROM terminal_ownerships WHERE session_id = ?",
                    (str(session_id),),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise SuccessionError(f"Cannot read ownership record: {exc}") from exc
        if row is None:
            return None
        try:
            return TerminalOwnership(**dict(row))
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
        try:
            with self._store._connect() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO terminal_ownerships
                    (session_id, state, reason, retired_at, logical_key, successor_session_id)
                    VALUES (?, ?, ?, ?, ?, NULL)""",
                    (
                        record.session_id,
                        record.state,
                        record.reason,
                        record.retired_at,
                        record.logical_key,
                    ),
                )
                row = conn.execute(
                    "SELECT session_id, state, reason, retired_at, logical_key, "
                    "successor_session_id FROM terminal_ownerships WHERE session_id = ?",
                    (record.session_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise SuccessionError(f"Cannot retire session {session_id}: {exc}") from exc
        return self._record_from_row(row, session_id)

    def link_successor(
        self, session_id: str, successor_session_id: str
    ) -> TerminalOwnership:
        source = str(session_id)
        successor = str(successor_session_id)
        if source == successor:
            raise SuccessionError(f"Session {source} cannot be its own successor")
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT session_id, state, reason, retired_at, logical_key, "
                    "successor_session_id FROM terminal_ownerships WHERE session_id = ?",
                    (source,),
                ).fetchone()
                if row is None:
                    raise SuccessionError(
                        f"Session {source} has no terminal ownership record to link"
                    )
                if row["successor_session_id"] == successor:
                    return self._record_from_row(row, source)
                if row["successor_session_id"] is not None:
                    raise SuccessionError(
                        f"Session {source} already has successor "
                        f"{row['successor_session_id']}; refusing {successor}"
                    )

                seen = {source}
                current: str | None = successor
                while current is not None:
                    if current in seen:
                        raise SuccessionError("Successor link would create a cycle")
                    seen.add(current)
                    next_row = conn.execute(
                        "SELECT successor_session_id FROM terminal_ownerships "
                        "WHERE session_id = ?",
                        (current,),
                    ).fetchone()
                    current = next_row["successor_session_id"] if next_row else None

                conn.execute(
                    """UPDATE terminal_ownerships SET successor_session_id = ?
                    WHERE session_id = ? AND successor_session_id IS NULL""",
                    (successor, source),
                )
                row = conn.execute(
                    "SELECT session_id, state, reason, retired_at, logical_key, "
                    "successor_session_id FROM terminal_ownerships WHERE session_id = ?",
                    (source,),
                ).fetchone()
        except SuccessionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise SuccessionError(f"Cannot link successor for {session_id}: {exc}") from exc
        updated = self._record_from_row(row, source)
        if updated.successor_session_id != successor:
            raise SuccessionError(
                f"Session {session_id} already has successor "
                f"{updated.successor_session_id}; refusing {successor_session_id}"
            )
        return updated

    def _migrate_legacy_file(self) -> None:
        """Import a v1 JSON store before retiring it, so recovery is repeatable."""
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SuccessionError(
                f"Cannot read ownership state {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != OWNERSHIP_VERSION:
            raise SuccessionError(f"Unsupported ownership state version: {self.path}")
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            raise SuccessionError(f"Unsupported ownership state version: {self.path}")
        try:
            records = [
                TerminalOwnership(**row)
                for row in sessions.values()
                if isinstance(row, dict)
            ]
        except (TypeError, SuccessionError) as exc:
            raise SuccessionError(f"Corrupt ownership state {self.path}: {exc}") from exc
        if len(records) != len(sessions):
            raise SuccessionError(f"Corrupt ownership state {self.path}")
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    """INSERT OR IGNORE INTO terminal_ownerships
                    (session_id, state, reason, retired_at, logical_key, successor_session_id)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            record.session_id,
                            record.state,
                            record.reason,
                            record.retired_at,
                            record.logical_key,
                            record.successor_session_id,
                        )
                        for record in records
                    ],
                )
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise SuccessionError(f"Cannot migrate ownership state {self.path}: {exc}") from exc
        try:
            os.replace(self.path, self.path.with_name(f"{self.path.name}.migrated"))
        except FileNotFoundError:
            # Another opener completed the same idempotent migration.
            pass
        except OSError as exc:
            raise SuccessionError(f"Cannot retire ownership state {self.path}: {exc}") from exc

    @staticmethod
    def _record_from_row(
        row: sqlite3.Row | None, session_id: str
    ) -> TerminalOwnership:
        if row is None:
            raise SuccessionError(f"Ownership record disappeared for session {session_id}")
        try:
            return TerminalOwnership(**dict(row))
        except (TypeError, SuccessionError) as exc:
            raise SuccessionError(
                f"Corrupt ownership record for session {session_id}: {exc}"
            ) from exc


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
        if hasattr(client, "interrupt_command"):
            client.interrupt_command(str(row["id"]))
            cancelled += 1
        elif row.get("execution_process_id"):
            client.stop_execution(
                str(row["execution_process_id"]),
                dedupe_key=f"quarantine:{row['id']}",
            )
            cancelled += 1

    return HandoffResult(
        source_session_id=source,
        successor_session_id=successor,
        logical_key=key,
        spawned=spawned,
        forwarded_commands=forwarded,
        cancelled_commands=cancelled,
    )


# ---------------------------------------------------------------- outcome reroute

#: The typed provider outcomes that may advance a route chain. Everything else
#: - a definitive rejection, a lost launch, and every repository, test, or code
#: failure - is a task-level result with no provider outcome at all, so it can
#: never reach this path. That is what makes "code failures never reroute" true
#: by construction rather than by pattern-matching what a worker printed.
REROUTE_OUTCOMES = frozenset({"rate_limited", "auth", "provider_down"})

#: Spellings written before the outcomes were typed. A task that was already
#: mid-failover when the upgrade landed carries ``quota`` in its target and its
#: journal, and refusing to read it would strand exactly the work an upgrade is
#: most likely to interrupt.
LEGACY_OUTCOMES = {"quota": "rate_limited"}

#: Everything a reconcile sweep should look for, current and legacy alike.
SWEEPABLE_OUTCOMES = REROUTE_OUTCOMES | frozenset(LEGACY_OUTCOMES)


def routing_outcome(value: object) -> str | None:
    """The reroute outcome a stored value names, or ``None`` if it names none.

    One place decides what counts, so every caller reads legacy and current
    spellings identically and no branch can be forgotten.
    """
    outcome = str(value or "")
    outcome = LEGACY_OUTCOMES.get(outcome, outcome)
    return outcome if outcome in REROUTE_OUTCOMES else None


def cool_provider_outcome(
    settings: execution_routing.ExecutionRoutingSettings,
    *,
    outcome: str,
    binding_id: str,
    route_class: str | None = None,
    route_id: str | None = None,
    retry_at: float | None = None,
) -> tuple[str, ...]:
    """Cool exactly the accounts one typed outcome condemns. Returns their ids.

    Safe to call any number of times for one outcome: pool cooling is
    monotonic, so a repeat is a no-op rather than an extension. That is what
    lets the caller cool *before* it records the outcome, closing the window
    where a crash in between left an exhausted account uncooled forever.

    The cooldown itself lives in pool state, the single source of account
    truth, so every later selection - including one after a restart - observes
    it. Only opaque binding ids are touched; credentials are never read here.

    A capacity or auth outcome is about one account, so one account cools. A
    ``provider_down`` is about the provider behind the route, so every account
    in that route's pool cools for the short window - otherwise the chain would
    walk the same dead provider account by account.
    """
    if outcome not in REROUTE_OUTCOMES:
        raise SuccessionError(f"Outcome {outcome!r} does not cool an account")
    route = settings.route(route_class, route_id) if route_id else None
    if outcome == "provider_down" and route is not None and route.account_pool:
        pool = pool_core.load_pool()
        cooled = tuple(
            str(account["id"])
            for account in pool_core.accounts_for(pool, route.account_pool)
            if account.get("id")
        )
        for account_id in cooled:
            pool_core.set_cooldown(account_id, pool_core.SHORT_COOLDOWN)
        return cooled
    if outcome == "rate_limited":
        if retry_at is not None:
            pool_core.cool_until_timestamp(binding_id, max(retry_at, time.time()))
        else:
            pool_core.set_cooldown(binding_id)
    else:
        pool_core.set_cooldown(binding_id, pool_core.SHORT_COOLDOWN)
    return (binding_id,)


def advance_route_after_outcome(
    settings: execution_routing.ExecutionRoutingSettings,
    *,
    outcome: str,
    route_class: str | None = None,
    failed_binding_id: str | None = None,
) -> execution_routing.SelectionResult:
    """Pick the next hop of a class chain after a typed provider outcome.

    A pure read: the condemned accounts were already cooled by
    :func:`cool_provider_outcome` when the outcome was recorded, so running
    this twice cannot extend a cooldown or move an account twice.

    Selection stays inside ``route_class`` and passes no model preference, so
    the walk lands on the next eligible *account of the same route* before it
    ever reaches the next route. Retrying the same model on a second account
    before switching models therefore needs no retry counter: it falls out of
    pool order.
    """
    if outcome not in REROUTE_OUTCOMES:
        raise SuccessionError(f"Outcome {outcome!r} does not advance a route chain")
    return execution_routing.select_route(
        settings,
        route_class=route_class,
        exclude_account_ids=(
            frozenset({failed_binding_id}) if failed_binding_id else frozenset()
        ),
    )


# ---------------------------------------------------------------- free route failure


@dataclass(frozen=True)
class FreeRouteFailure:
    route_id: str
    outcome: str
    detail: str
    escalation: dict[str, Any]
    selection: execution_routing.SelectionResult | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "outcome": self.outcome,
            "detail": self.detail,
            "escalation": self.escalation,
            "selection": self.selection.to_dict() if self.selection else None,
        }


def _failure_detail(output: str) -> str:
    """The first meaningful line of captured output, redacted and bounded."""
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if stripped:
            return redact_credentials(stripped)[:FREE_FAILURE_DETAIL_LIMIT]
    return "no output captured"


def escalate_free_route_failure(
    client: Any,
    settings: execution_routing.ExecutionRoutingSettings,
    *,
    route_id: str,
    child_session_id: str,
    child_workspace_id: str | None = None,
    parent_session_id: str | None = None,
    route_class: str | None = None,
    output: str = "",
    store: EscalationStore | None = None,
) -> FreeRouteFailure:
    """Make a free route's terminal failure visible, and never quietly billed.

    A free route owns no account by construction, so there is no binding to
    cool and nothing for `advance_route_after_outcome` to act on - a
    terminal failure would otherwise leave the worker blocked with no signal
    at all. This gives it exactly one visible outcome: an escalation carrying
    the route id and outcome class, delivered to a live parent or parked in
    the decision inbox by :func:`escalate`, which has no third result.

    Degrading onto a route that bills is a separate decision the operator has
    to make: it happens only under `fallbackOnFreeFailure`, and the escalation
    names the route that was chosen either way. Selection is a pure read, so
    computing it first costs nothing and lets one message carry the whole
    story rather than leaving the operator to correlate two.
    """
    outcome = execution_routing.classify_free_failure(output)
    detail = _failure_detail(output)

    selection: execution_routing.SelectionResult | None = None
    if settings.fallback_on_free_failure:
        selection = execution_routing.select_route(
            settings,
            route_class=route_class,
            exclude_route_ids=frozenset({route_id}),
        )

    if selection is None:
        consequence = (
            "fallbackOnFreeFailure is off, so no billed account was used and "
            "this session stays blocked until you route it"
        )
    elif selection.status == "resolved" and selection.target is not None:
        consequence = (
            f"fallbackOnFreeFailure moved it to route {selection.target.route_id} "
            f"({selection.target.billing_class})"
        )
    else:
        consequence = (
            f"fallbackOnFreeFailure found no other route ({selection.reason})"
        )

    record = escalate(
        client,
        child_session_id=str(child_session_id),
        child_workspace_id=child_workspace_id,
        parent_session_id=parent_session_id,
        message=(
            f"DECISION: free route {route_id} failed ({outcome}); "
            f"{consequence}. Detail: {detail}"
        ),
        store=store,
        dedupe_key=f"free-route-failure:{child_session_id}:{route_id}:{outcome}",
    )
    return FreeRouteFailure(
        route_id=str(route_id),
        outcome=outcome,
        detail=detail,
        escalation=record,
        selection=selection,
    )
