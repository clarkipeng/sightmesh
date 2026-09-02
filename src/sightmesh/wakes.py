"""Durable wakes: one outbox row per satisfied parent predicate.

A manager waits on a predicate over its children, not on a stream of
per-child mail. The child's terminal transition and the parent's wake row
are written in one transaction on one connection, so a crash cannot land
between "the child finished" and "the parent was told" -- the two facts are
the same fact. Delivery is a separate, retryable pump; the reconciler is its
safety net.

Consolidating the cohort into one payload is also what removes the old
per-child notification path, and with it the ``intent="replace"`` that used
to interrupt a manager's turn whenever a child blocked.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .cdesktop import CdesktopClient, CdesktopError
from .succession import OwnershipStore, QuarantinedSessionError
from .task_store import LIVE_STATES, TaskRecord, TaskStore, TaskStoreError

LOGGER = logging.getLogger("sightmesh.wakes")

CLAIM_SECONDS = 60.0


@dataclass(frozen=True)
class Wake:
    wake_id: str
    parent_task_id: str
    predicate: str
    dedupe_key: str
    event_seq: int | None
    state: str
    claim_expires_at: float | None
    payload: str | None


def dedupe_key(parent_task_id: str, predicate: str) -> str:
    return f"{parent_task_id}:{predicate}"


def finish_with_wake(
    store: TaskStore,
    task_id: str,
    state: str,
    result: str | None = None,
    *,
    expect_version: int | None = None,
) -> tuple[TaskRecord, list[str]]:
    """Finish a task and record any parent wake it satisfies, atomically."""
    try:
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = store.finish(
                task_id, state, result, expect_version=expect_version, conn=conn
            )
            created: list[str] = []
            if record.parent_task_id:
                # One child terminal/blocked transition is one cohort event:
                # bump the parent's monotonic counter in the same transaction
                # so a wake's watermark can prove, later, whether the manager
                # has already been woken for it.
                conn.execute(
                    "UPDATE managed_tasks SET child_event_seq = child_event_seq + 1 "
                    "WHERE task_id = ?",
                    (str(record.parent_task_id),),
                )
                created = record_wakes(conn, record.parent_task_id)
            conn.execute("COMMIT")
            return record, created
    except TaskStoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise TaskStoreError(f"Cannot finish managed task: {exc}") from exc


def satisfied_predicates(conn: sqlite3.Connection, parent_task_id: str) -> list[str]:
    """Evaluate both wait predicates purely over durable child rows.

    Deriving them from stored state rather than from the transition that just
    happened is what lets the reconciler repair pre-migration history with the
    same code the live path uses.
    """
    placeholders = ", ".join("?" for _ in LIVE_STATES)
    total, live, blocked = conn.execute(
        "SELECT COUNT(*), "
        f"SUM(state IN ({placeholders})), "
        "SUM(state = 'blocked') "
        "FROM managed_tasks WHERE parent_task_id = ?",
        (*sorted(LIVE_STATES), str(parent_task_id)),
    ).fetchone()
    predicates: list[str] = []
    if blocked:
        predicates.append("any_child_blocked")
    if total and not live:
        predicates.append("all_children_terminal")
    return predicates


def record_wakes(conn: sqlite3.Connection, parent_task_id: str) -> list[str]:
    """Arm one pending wake per satisfied predicate past the watermark; else nothing.

    A wake is due only while the parent has seen a child event its manager has
    not yet been woken for: ``child_event_seq > last_woken_seq``. That single
    comparison is what makes both invariants true by construction. A reconciler
    re-scanning an unchanged, already-delivered cohort finds the counters equal
    and arms nothing; a genuinely new child event (or a resolved, never
    -delivered wake that left the watermark where it was) leaves the counter
    ahead and re-arms. The partial unique index still collapses concurrent
    duplicate signals for the same un-consumed cohort event to one live row.
    """
    row = conn.execute(
        "SELECT child_event_seq, last_woken_seq FROM managed_tasks WHERE task_id = ?",
        (str(parent_task_id),),
    ).fetchone()
    if row is None:
        return []
    child_event_seq = int(row["child_event_seq"])
    if child_event_seq <= int(row["last_woken_seq"]):
        return []
    created: list[str] = []
    for predicate in satisfied_predicates(conn, parent_task_id):
        key = dedupe_key(parent_task_id, predicate)
        wake_id = str(uuid.uuid4())
        now = time.time()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO task_wakes
            (wake_id, parent_task_id, predicate, dedupe_key, event_seq,
             state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (wake_id, str(parent_task_id), predicate, key, child_event_seq, now, now),
        )
        if cursor.rowcount:
            created.append(wake_id)
    return created


class WakeDelivery:
    """Claim, consolidate, and deliver pending wakes; safe to run repeatedly."""

    def __init__(
        self,
        client: CdesktopClient,
        store: TaskStore,
        ownership: OwnershipStore | None = None,
        *,
        claim_seconds: float = CLAIM_SECONDS,
    ) -> None:
        self.client = client
        self.store = store
        self.ownership = ownership if ownership is not None else OwnershipStore()
        self.claim_seconds = claim_seconds

    def pump(self) -> int:
        """Deliver every claimable wake; return how many left the outbox."""
        delivered = 0
        for wake in self.claim():
            try:
                delivered += int(self._deliver(wake))
            except CdesktopError as exc:
                # The claim lease expires and the reconciler retries; the row
                # stays visible in the outbox rather than being dropped here.
                LOGGER.warning("Cannot deliver wake %s: %s", wake.wake_id, exc)
        return delivered

    def claim(self) -> list[Wake]:
        now = time.time()
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT * FROM task_wakes WHERE state = 'pending' "
                    "OR (state = 'claimed' AND claim_expires_at < ?) "
                    "ORDER BY created_at",
                    (now,),
                ).fetchall()
                claimed = [_decode(row) for row in rows]
                for wake in claimed:
                    conn.execute(
                        "UPDATE task_wakes SET state = 'claimed', "
                        "claim_expires_at = ?, updated_at = ? WHERE wake_id = ?",
                        (now + self.claim_seconds, now, wake.wake_id),
                    )
                conn.execute("COMMIT")
                return claimed
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot claim task wakes: {exc}") from exc

    def _deliver(self, wake: Wake) -> bool:
        parent = self.store.get_by_id(wake.parent_task_id)
        if parent is None:
            return self._resolve(wake, "parent task no longer exists")
        if not parent.holder_session_id:
            return self._resolve(wake, f"parent {parent.key} has no holder session")
        if parent.state not in LIVE_STATES:
            # A terminal parent refuses machine mail; delivering here would send
            # a continuation into a session that has already ended.
            return self._resolve(wake, f"parent {parent.key} is {parent.state}")
        try:
            self.ownership.assert_deliverable(parent.holder_session_id)
        except QuarantinedSessionError as exc:
            # A retired or superseded holder session can never resume; parking
            # the wake (and, with live re-arm, letting a fresh cohort event wake
            # the successor) is the only safe outcome.
            return self._resolve(wake, f"parent {parent.key} session is retired: {exc}")
        children = self.store.children(parent.task_id)
        if any(
            child.holder_session_id == parent.holder_session_id for child in children
        ):
            return self._resolve(
                wake, f"parent {parent.key} holds one of its own child sessions"
            )
        payload = _payload(wake.predicate, parent, children)
        self.client.send(
            parent.holder_session_id,
            payload,
            None,
            dedupe_key=wake.wake_id,
            intent="continue",
        )
        self._settle(wake, "delivered", payload)
        return True

    def _resolve(self, wake: Wake, reason: str) -> bool:
        """Park a suppressed delivery with its reason; never return silently."""
        LOGGER.info("Wake %s resolved without delivery: %s", wake.wake_id, reason)
        self._settle(wake, "resolved", f"suppressed: {reason}")
        return False

    def _settle(self, wake: Wake, state: str, payload: str) -> None:
        now = time.time()
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE task_wakes SET state = ?, payload = ?, "
                    "claim_expires_at = NULL, updated_at = ? WHERE wake_id = ?",
                    (state, payload, now, wake.wake_id),
                )
                if state == "delivered" and wake.event_seq is not None:
                    # Only a real delivery advances the watermark. A resolved
                    # (suppressed) wake leaves it where it was, so the same
                    # cohort event re-arms on the next pass. MAX keeps an
                    # out-of-order delivery from ever moving it backwards.
                    conn.execute(
                        "UPDATE managed_tasks SET "
                        "last_woken_seq = MAX(last_woken_seq, ?), updated_at = ? "
                        "WHERE task_id = ?",
                        (int(wake.event_seq), now, wake.parent_task_id),
                    )
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot settle task wake: {exc}") from exc


def _payload(predicate: str, parent: TaskRecord, children: list[TaskRecord]) -> str:
    lines = [f"COHORT {predicate}: {parent.key}"]
    for child in children:
        line = f"- {child.key}: {child.state}"
        if child.result:
            line += f" | {child.result}"
        lines.append(line)
    return "\n".join(lines)


def _decode(row: Any) -> Wake:
    return Wake(
        wake_id=str(row["wake_id"]),
        parent_task_id=str(row["parent_task_id"]),
        predicate=str(row["predicate"]),
        dedupe_key=str(row["dedupe_key"]),
        event_seq=row["event_seq"],
        state=str(row["state"]),
        claim_expires_at=row["claim_expires_at"],
        payload=row["payload"],
    )
