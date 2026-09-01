"""The one journal every native launch goes through.

A launch is an effect on the world that no amount of retrying can undo, so
the durable row is written *before* the call and only ever advanced
afterwards. One row per ``(task_id, epoch)`` under a primary key is what
makes a duplicate start impossible rather than merely unlikely: the second
caller reads the first caller's row instead of forking a second session.

The reservation carries an expiring lease and the owner that took it. A
crashed owner's lease expires and the next caller adopts the same reserved
identifiers; a live owner's lease refuses the takeover.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .task_store import TaskStore, TaskStoreError

RESERVATION_TTL_SECONDS = 120.0


class EffectBusy(TaskStoreError):
    """Another live owner holds the reservation for this task epoch."""


class EffectConflict(TaskStoreError):
    """The same task epoch was already reserved for a different launch spec."""


def request_hash(launch: Mapping[str, Any]) -> str:
    """Digest a launch spec so drift can never be replayed as a duplicate."""
    canonical = json.dumps(launch, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_owner_instance() -> str:
    """Identify one SightMesh instance for the life of the process."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Effect:
    task_id: str
    epoch: int
    request_hash: str
    state: str
    workspace_id: str | None
    session_id: str | None
    outcome: str | None
    owner_instance: str
    lease_expires_at: float


class EffectJournal:
    """Reserve, launch, and retire the native effect of one task epoch."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def reserve(
        self,
        task_id: str,
        epoch: int,
        request_hash: str,
        owner: str,
        ttl: float = RESERVATION_TTL_SECONDS,
    ) -> tuple[Effect, bool]:
        """Claim the right to launch this epoch; report whether we took over.

        Returns the existing effect untouched when it is already launched or
        terminal, so the caller adopts rather than relaunches.
        """
        now = time.time()
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._row(conn, task_id, epoch)
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO task_effects
                        (task_id, epoch, request_hash, state, owner_instance,
                         lease_expires_at, created_at, updated_at)
                        VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?)
                        """,
                        (
                            str(task_id),
                            int(epoch),
                            str(request_hash),
                            str(owner),
                            now + ttl,
                            now,
                            now,
                        ),
                    )
                    effect = self._require(conn, task_id, epoch)
                    conn.execute("COMMIT")
                    return effect, False
                existing = _decode(row)
                if existing.request_hash != request_hash:
                    raise EffectConflict(
                        f"Task {task_id} epoch {epoch} was reserved for a different "
                        "launch specification"
                    )
                if existing.state != "reserved":
                    conn.execute("COMMIT")
                    return existing, False
                mine = existing.owner_instance == owner
                if not mine and existing.lease_expires_at > now:
                    raise EffectBusy(
                        f"Task {task_id} epoch {epoch} is reserved by another live "
                        "SightMesh instance"
                    )
                conn.execute(
                    "UPDATE task_effects SET owner_instance = ?, "
                    "lease_expires_at = ?, updated_at = ? "
                    "WHERE task_id = ? AND epoch = ?",
                    (str(owner), now + ttl, now, str(task_id), int(epoch)),
                )
                effect = self._require(conn, task_id, epoch)
                conn.execute("COMMIT")
                return effect, not mine
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot reserve task effect: {exc}") from exc

    def mark_launched(
        self, task_id: str, epoch: int, workspace_id: str, session_id: str
    ) -> Effect:
        """Bind the native identifiers a retry must adopt instead of recreate."""
        return self._advance(
            task_id,
            epoch,
            "state = 'launched', workspace_id = ?, session_id = ?",
            (str(workspace_id), str(session_id)),
            frozenset({"reserved", "launched"}),
        )

    def mark_terminal(self, task_id: str, epoch: int, outcome: str) -> Effect:
        """Record the typed outcome; the first terminal write wins."""
        return self._advance(
            task_id,
            epoch,
            "state = 'terminal', outcome = ?",
            (str(outcome),),
            frozenset({"reserved", "launched"}),
        )

    def get(self, task_id: str, epoch: int) -> Effect | None:
        try:
            with self.store.connect() as conn:
                row = self._row(conn, task_id, epoch)
            return _decode(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot read task effect: {exc}") from exc

    def expire_reservations(self, *, now: float | None = None) -> list[Effect]:
        """Retire leases whose owner died before it reached the native call."""
        moment = time.time() if now is None else now
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT * FROM task_effects WHERE state = 'reserved' "
                    "AND lease_expires_at < ? AND session_id IS NULL",
                    (moment,),
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "UPDATE task_effects SET state = 'terminal', outcome = ?, "
                        "updated_at = ? WHERE task_id = ? AND epoch = ?",
                        (
                            "lost:reservation-expired",
                            moment,
                            str(row["task_id"]),
                            int(row["epoch"]),
                        ),
                    )
                expired = [
                    self._require(conn, str(row["task_id"]), int(row["epoch"]))
                    for row in rows
                ]
                conn.execute("COMMIT")
                return expired
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot expire task effects: {exc}") from exc

    def _advance(
        self,
        task_id: str,
        epoch: int,
        assign: str,
        values: tuple[object, ...],
        expect_states: frozenset[str],
    ) -> Effect:
        now = time.time()
        states = sorted(expect_states)
        placeholders = ", ".join("?" for _ in states)
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    f"UPDATE task_effects SET {assign}, updated_at = ? "
                    f"WHERE task_id = ? AND epoch = ? AND state IN ({placeholders})",
                    (*values, now, str(task_id), int(epoch), *states),
                )
                effect = self._require(conn, task_id, epoch)
                conn.execute("COMMIT")
                return effect
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot update task effect: {exc}") from exc

    @staticmethod
    def _row(conn: sqlite3.Connection, task_id: str, epoch: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM task_effects WHERE task_id = ? AND epoch = ?",
            (str(task_id), int(epoch)),
        ).fetchone()

    @classmethod
    def _require(cls, conn: sqlite3.Connection, task_id: str, epoch: int) -> Effect:
        row = cls._row(conn, task_id, epoch)
        if row is None:
            raise TaskStoreError(f"Task effect {task_id}/{epoch} not found")
        return _decode(row)


def _decode(row: sqlite3.Row) -> Effect:
    try:
        return Effect(
            task_id=str(row["task_id"]),
            epoch=int(row["epoch"]),
            request_hash=str(row["request_hash"]),
            state=str(row["state"]),
            workspace_id=row["workspace_id"],
            session_id=row["session_id"],
            outcome=row["outcome"],
            owner_instance=str(row["owner_instance"]),
            lease_expires_at=float(row["lease_expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskStoreError(f"Corrupt task effect record: {exc}") from exc
