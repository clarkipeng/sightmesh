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
import logging
import sqlite3
import time
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from .cdesktop import CdesktopError, is_effect_not_found
from .task_store import TaskStore, TaskStoreError

LOGGER = logging.getLogger("sightmesh.effects")

RESERVATION_TTL_SECONDS = 120.0


class _UnknowableEffect(RuntimeError):
    """The executor could neither confirm nor deny a native session this tick.

    A 5xx, a ``URLError``, or a timeout is absence of proof of life, not proof
    of death. Raising it (rather than reporting "absent") is what stops the
    expiry sweep from retiring a reservation whose native session is still
    running; only a definitive 404 is treated as real absence.
    """


class EffectBusy(TaskStoreError):
    """Another live owner holds the reservation for this task epoch."""


class EffectConflict(TaskStoreError):
    """The same task epoch was already reserved for a different launch spec."""


class EffectTerminal(TaskStoreError):
    """This task epoch already reached a typed terminal outcome.

    A terminal effect is a launch barrier, not an adoptable reservation: the
    epoch is finished and the caller must advance to a new epoch (``replace``)
    rather than relaunch the one that ended.
    """


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
    #: Provider-advertised reset for a capacity outcome, when one was sent.
    #: Absent for every other outcome, and never inferred.
    retry_at: float | None = None


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

        The checks are ordered by *state first*, then hash, so a finished or
        launched epoch is resolved before drift is even considered:

        * ``terminal`` -> ``EffectTerminal`` (a launch barrier, whatever the
          hash: this epoch is over and must be advanced, never relaunched);
        * ``launched`` -> adopt the existing native identifiers, whatever the
          hash (the launch already happened);
        * ``reserved`` + expired lease -> fenced takeover;
        * ``reserved`` + live lease + other owner -> ``EffectBusy``;
        * ``reserved`` + live lease + different hash -> ``EffectConflict``;
        * ``reserved`` + live lease + same owner and hash -> adopt.
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
                if existing.state == "terminal":
                    raise EffectTerminal(
                        f"Task {task_id} epoch {epoch} already reached the terminal "
                        f"outcome {existing.outcome!r}; advance the epoch to relaunch"
                    )
                if existing.state == "launched":
                    conn.execute("COMMIT")
                    return existing, False
                # Only a live ``reserved`` row remains.
                if existing.lease_expires_at > now:
                    if existing.owner_instance != owner:
                        raise EffectBusy(
                            f"Task {task_id} epoch {epoch} is reserved by another "
                            "live SightMesh instance"
                        )
                    if existing.request_hash != request_hash:
                        raise EffectConflict(
                            f"Task {task_id} epoch {epoch} was reserved for a "
                            "different launch specification"
                        )
                took_over = existing.lease_expires_at <= now
                conn.execute(
                    "UPDATE task_effects SET owner_instance = ?, "
                    "lease_expires_at = ?, updated_at = ? "
                    "WHERE task_id = ? AND epoch = ?",
                    (str(owner), now + ttl, now, str(task_id), int(epoch)),
                )
                effect = self._require(conn, task_id, epoch)
                conn.execute("COMMIT")
                return effect, took_over
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

    def mark_terminal(
        self,
        task_id: str,
        epoch: int,
        outcome: str,
        retry_at: float | None = None,
    ) -> Effect:
        """Record the typed outcome; the first terminal write wins.

        ``retry_at`` is written alongside it so a later reconcile can cool the
        exhausted account until the provider's own reset without having to
        re-derive it from anything.
        """
        return self._advance(
            task_id,
            epoch,
            "state = 'terminal', outcome = ?, retry_at = ?",
            (str(outcome), None if retry_at is None else float(retry_at)),
            frozenset({"reserved", "launched"}),
        )

    def with_outcomes(self, outcomes: Collection[str]) -> list[Effect]:
        """Current-epoch effects that ended on one of these typed outcomes.

        Joined against the task's own epoch so a superseded epoch's outcome can
        never re-trigger the reconcile that already advanced past it, and
        restricted to tasks that can still move: a completed or cancelled task
        has nothing left to reroute, and neither has one whose attempt circuit
        breaker has tripped - a terminal effect stays visible forever, so
        without that bound the sweep would re-attempt a doomed replacement on
        every tick for the life of the task.
        """
        if not outcomes:
            return []
        wanted = sorted(str(outcome) for outcome in outcomes)
        placeholders = ", ".join("?" for _ in wanted)
        try:
            with self.store.connect() as conn:
                rows = conn.execute(
                    "SELECT e.* FROM task_effects AS e "
                    "JOIN managed_tasks AS t "
                    "  ON t.task_id = e.task_id AND t.epoch = e.epoch "
                    f"WHERE e.state = 'terminal' AND e.outcome IN ({placeholders}) "
                    "AND t.state IN ('active', 'blocked') "
                    "AND t.attempts < t.max_attempts "
                    "ORDER BY e.updated_at",
                    tuple(wanted),
                ).fetchall()
            return [_decode(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot read task effects: {exc}") from exc

    def get(self, task_id: str, epoch: int) -> Effect | None:
        try:
            with self.store.connect() as conn:
                row = self._row(conn, task_id, epoch)
            return _decode(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot read task effect: {exc}") from exc

    def expire_reservations(
        self, client: Any | None = None, *, now: float | None = None
    ) -> list[Effect]:
        """Adopt-or-lose every reservation whose lease has run out.

        A 15s native-launch timeout makes the "session created but
        ``mark_launched`` never ran" window ordinary, so an expired reservation
        is not evidence the native session is gone. For each expired row the
        journal asks the executor: if it reports an active session, the effect
        is adopted (``launched`` + the task activated); only a genuinely absent
        or lost session is retired ``lost:reservation-expired``.
        """
        moment = time.time() if now is None else now
        try:
            with self.store.connect() as conn:
                candidates = [
                    (str(row["task_id"]), int(row["epoch"]))
                    for row in conn.execute(
                        "SELECT task_id, epoch FROM task_effects "
                        "WHERE state = 'reserved' AND lease_expires_at < ?",
                        (moment,),
                    ).fetchall()
                ]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot expire task effects: {exc}") from exc

        lost: list[Effect] = []
        for task_id, epoch in candidates:
            try:
                with self.store.task_lock(task_id) as fence:
                    current = self.store.get_by_id(task_id)
                    if current is None:
                        retired = self._retire_reservation(task_id, epoch, moment)
                        if retired is not None:
                            lost.append(retired)
                        continue
                    if current.epoch != epoch:
                        continue
                    version = current.version
                    with fence.external_io():
                        native = self._native_effect(client, task_id, epoch)
                    current = self.store.get_by_id(task_id)
                    if (
                        current is None
                        or current.epoch != epoch
                        or current.version != version
                    ):
                        if native and native.get("workspace_id"):
                            self.mark_terminal(task_id, epoch, "superseded")
                            client.stop_workspace(str(native["workspace_id"]))
                        continue
                    if native is not None:
                        workspace_id = native.get("workspace_id")
                        session_id = native.get("session_id")
                        if (
                            native.get("state") == "active"
                            and workspace_id
                            and session_id
                        ):
                            # Native is live behind the lease: adopt it rather than
                            # orphan the running session.
                            self.mark_launched(
                                task_id, epoch, str(workspace_id), str(session_id)
                            )
                            self.store.activate(
                                task_id,
                                workspace_id=str(workspace_id),
                                session_id=str(session_id),
                                fence=fence,
                            )
                            continue
                    retired = self._retire_reservation(task_id, epoch, moment)
                    if retired is not None:
                        lost.append(retired)
            except _UnknowableEffect as exc:
                # The executor could not answer. Leave the reservation intact
                # for the next tick rather than orphan a possibly-live session.
                LOGGER.info(
                    "Leaving reservation %s/%s reserved: executor unreachable: %s",
                    task_id,
                    epoch,
                    exc,
                )
            except TaskStoreError as exc:
                LOGGER.info(
                    "Skipping expired reservation %s/%s this tick: %s",
                    task_id,
                    epoch,
                    exc,
                )
                continue
        return lost

    @staticmethod
    def _native_effect(
        client: Any | None, task_id: str, epoch: int
    ) -> dict[str, Any] | None:
        """Ask the executor whether a native session stands behind this epoch.

        Returns the native effect when one is live, ``None`` only on a
        definitive not-found (404) - real absence that may retire the
        reservation - and raises :class:`_UnknowableEffect` on any error that
        cannot prove absence (5xx, ``URLError``, timeout), so the caller leaves
        the reservation intact instead of orphaning a running session.
        """
        lookup = getattr(client, "managed_effect", None) if client else None
        if lookup is None:
            return None
        try:
            native = lookup(task_id, epoch)
        except CdesktopError as exc:
            # Real absence is whatever the executor's own miss shape says
            # (HTTP 400 "Managed task effect not found" on the pinned seam,
            # 404 on a corrected one); anything else (unreachable executor,
            # 5xx, timeout) is not proof of death.
            if is_effect_not_found(exc):
                return None
            raise _UnknowableEffect(str(exc)) from exc
        if not isinstance(native, Mapping):
            return None
        if str(native.get("state") or "") in {"", "missing", "not_found", "lost"}:
            return None
        return dict(native)

    def _retire_reservation(
        self, task_id: str, epoch: int, moment: float
    ) -> Effect | None:
        """Mark one still-reserved, still-expired effect lost; skip if it moved."""
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE task_effects SET state = 'terminal', outcome = ?, "
                    "updated_at = ? WHERE task_id = ? AND epoch = ? "
                    "AND state = 'reserved' AND lease_expires_at < ?",
                    ("lost:reservation-expired", moment, task_id, epoch, moment),
                )
                effect = (
                    self._require(conn, task_id, epoch) if cursor.rowcount else None
                )
                conn.execute("COMMIT")
                return effect
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot expire task effect: {exc}") from exc

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
                cursor = conn.execute(
                    f"UPDATE task_effects SET {assign}, updated_at = ? "
                    f"WHERE task_id = ? AND epoch = ? AND state IN ({placeholders})",
                    (*values, now, str(task_id), int(epoch), *states),
                )
                if cursor.rowcount != 1:
                    # A no-op UPDATE means the row is missing or already past the
                    # states this advance is allowed to move; surfacing it stops
                    # a terminal effect from being silently relaunched or
                    # re-marked.
                    effect = self.get_within(conn, task_id, epoch)
                    raise EffectTerminal(
                        f"Task {task_id} epoch {epoch} is "
                        f"{effect.state if effect else 'missing'}; "
                        "this effect transition no longer applies"
                    )
                effect = self._require(conn, task_id, epoch)
                conn.execute("COMMIT")
                return effect
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot update task effect: {exc}") from exc

    @classmethod
    def get_within(
        cls, conn: sqlite3.Connection, task_id: str, epoch: int
    ) -> Effect | None:
        row = cls._row(conn, task_id, epoch)
        return _decode(row) if row is not None else None

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
            retry_at=(float(row["retry_at"]) if row["retry_at"] is not None else None),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskStoreError(f"Corrupt task effect record: {exc}") from exc
