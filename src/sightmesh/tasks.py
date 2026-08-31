"""Durable, idempotent task launch reservations.

The row is orchestration metadata, not a scheduler or transcript store.  A
caller must commit a unique reservation before a launch side effect; every
later caller observes that row and therefore cannot manufacture another
workspace for the same logical task.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass

from .escalation import EscalationStore, EscalationStoreError


class TaskLaunchError(ValueError):
    pass


@dataclass(frozen=True)
class TaskLaunch:
    task_id: str
    state: str
    reservation_id: str | None
    workspace_id: str | None
    session_id: str | None
    parent_task_id: str | None
    max_children: int
    child_count: int
    spawn_attempts: int
    max_spawn_attempts: int
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result.pop("reservation_id", None)
        return result


@dataclass(frozen=True)
class LaunchReservation:
    task: TaskLaunch
    reservation_id: str | None
    should_spawn: bool


class TaskLaunchStore:
    def __init__(self, store: EscalationStore | None = None) -> None:
        self.store = store or EscalationStore()

    def get(self, task_id: str) -> TaskLaunch | None:
        try:
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM task_launches WHERE task_id = ?", (task_id,)
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise TaskLaunchError(f"Cannot read task {task_id}: {exc}") from exc
        return TaskLaunch(**dict(row)) if row else None

    def get_by_session(self, session_id: str) -> TaskLaunch | None:
        try:
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM task_launches WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise TaskLaunchError(
                f"Cannot read task for session {session_id}: {exc}"
            ) from exc
        return TaskLaunch(**dict(row)) if row else None

    def reserve(
        self,
        task_id: str,
        *,
        parent_task_id: str | None = None,
        max_children: int = 0,
        max_spawn_attempts: int = 3,
    ) -> LaunchReservation:
        task_id = task_id.strip()
        if not task_id:
            raise TaskLaunchError("task_id must not be empty")
        if parent_task_id == task_id:
            raise TaskLaunchError("A task cannot be its own parent or replacement")
        if max_children < 0 or max_spawn_attempts < 1:
            raise TaskLaunchError(
                "Task budgets must be non-negative and attempts positive"
            )
        reservation_id = str(uuid.uuid4())
        now = time.time()
        try:
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM task_launches WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row is not None:
                    task = TaskLaunch(**dict(row))
                    if (
                        task.parent_task_id != parent_task_id
                        or task.max_children != max_children
                        or task.max_spawn_attempts != max_spawn_attempts
                    ):
                        raise TaskLaunchError(
                            f"Task {task_id} already exists with different immutable limits"
                        )
                    if task.state in {"reserved", "active", "blocked"}:
                        conn.execute("COMMIT")
                        return LaunchReservation(task, None, False)
                    if task.spawn_attempts >= task.max_spawn_attempts:
                        conn.execute(
                            "UPDATE task_launches SET state = 'blocked', updated_at = ? "
                            "WHERE task_id = ?",
                            (now, task_id),
                        )
                        conn.execute("COMMIT")
                        blocked = self.get(task_id)
                        assert blocked is not None
                        self._park_blocked(blocked)
                        return LaunchReservation(blocked, None, False)
                    conn.execute(
                        "UPDATE task_launches SET state = 'reserved', reservation_id = ?, "
                        "spawn_attempts = spawn_attempts + 1, updated_at = ? WHERE task_id = ?",
                        (reservation_id, now, task_id),
                    )
                else:
                    if parent_task_id:
                        parent = conn.execute(
                            "SELECT state, child_count, max_children FROM task_launches "
                            "WHERE task_id = ?",
                            (parent_task_id,),
                        ).fetchone()
                        if parent is None or parent["state"] != "active":
                            raise TaskLaunchError(
                                f"Parent task {parent_task_id} is not active"
                            )
                        if parent["child_count"] >= parent["max_children"]:
                            raise TaskLaunchError(
                                f"Parent task {parent_task_id} reached its child limit"
                            )
                        conn.execute(
                            "UPDATE task_launches SET child_count = child_count + 1, "
                            "updated_at = ? WHERE task_id = ?",
                            (now, parent_task_id),
                        )
                    conn.execute(
                        """INSERT INTO task_launches
                        (task_id, state, reservation_id, workspace_id, session_id,
                         parent_task_id, max_children, child_count, spawn_attempts,
                         max_spawn_attempts, created_at, updated_at)
                        VALUES (?, 'reserved', ?, NULL, NULL, ?, ?, 0, 1, ?, ?, ?)""",
                        (
                            task_id,
                            reservation_id,
                            parent_task_id,
                            max_children,
                            max_spawn_attempts,
                            now,
                            now,
                        ),
                    )
                row = conn.execute(
                    "SELECT * FROM task_launches WHERE task_id = ?", (task_id,)
                ).fetchone()
                conn.execute("COMMIT")
        except TaskLaunchError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskLaunchError(f"Cannot reserve task {task_id}: {exc}") from exc
        return LaunchReservation(TaskLaunch(**dict(row)), reservation_id, True)

    def activate(
        self,
        task_id: str,
        reservation_id: str,
        *,
        workspace_id: str,
        session_id: str | None,
    ) -> TaskLaunch:
        return self._transition(
            task_id,
            reservation_id,
            "active",
            workspace_id=workspace_id,
            session_id=session_id,
            keep_reservation=True,
        )

    def transfer_reservation(
        self, task_id: str, reservation_id: str, *, prior_spawn_absent: bool = False
    ) -> LaunchReservation:
        """Explicitly rotate a crashed launcher's capability before retrying.

        The destination must first atomically prove that the old idempotency
        key never committed. A workspace listing is not such a proof.
        """
        if not prior_spawn_absent:
            raise TaskLaunchError(
                "Reservation transfer requires destination-owned proof that the "
                "prior spawn did not commit"
            )
        replacement = str(uuid.uuid4())
        blocked: TaskLaunch | None = None
        try:
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM task_launches WHERE task_id = ?", (task_id,)
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "reserved"
                    or row["reservation_id"] != reservation_id
                ):
                    raise TaskLaunchError(
                        f"Task {task_id} reservation is no longer owned"
                    )
                if row["spawn_attempts"] >= row["max_spawn_attempts"]:
                    conn.execute(
                        "UPDATE task_launches SET state = 'blocked', "
                        "reservation_id = NULL, updated_at = ? WHERE task_id = ?",
                        (time.time(), task_id),
                    )
                    blocked_row = conn.execute(
                        "SELECT * FROM task_launches WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
                    blocked = TaskLaunch(**dict(blocked_row))
                else:
                    conn.execute(
                        "UPDATE task_launches SET reservation_id = ?, "
                        "spawn_attempts = spawn_attempts + 1, updated_at = ? "
                        "WHERE task_id = ?",
                        (replacement, time.time(), task_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM task_launches WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
        except TaskLaunchError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskLaunchError(f"Cannot transfer task {task_id}: {exc}") from exc
        if blocked is not None:
            self._park_blocked(blocked)
            return LaunchReservation(blocked, None, False)
        task = TaskLaunch(**dict(row))
        return LaunchReservation(task, replacement, True)

    def reserve_failover(self, source_session_id: str) -> LaunchReservation:
        """Atomically move an active manager task into its next launch attempt."""
        replacement = str(uuid.uuid4())
        now = time.time()
        blocked: TaskLaunch | None = None
        try:
            with self.store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM task_launches WHERE session_id = ?",
                    (source_session_id,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    raise TaskLaunchError(
                        f"Session {source_session_id} has no managed task identity"
                    )
                task = TaskLaunch(**dict(row))
                if task.state == "reserved":
                    conn.execute("COMMIT")
                    # Concurrent/restarted failover callers replay one exact
                    # destination-owned key. This is the same attempt, not a
                    # newly authorized side effect.
                    return LaunchReservation(task, task.reservation_id, True)
                if task.state == "blocked" or task.spawn_attempts >= task.max_spawn_attempts:
                    conn.execute(
                        "UPDATE task_launches SET state = 'blocked', "
                        "reservation_id = NULL, updated_at = ? WHERE task_id = ?",
                        (now, task.task_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM task_launches WHERE task_id = ?", (task.task_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
                    blocked = TaskLaunch(**dict(row))
                elif task.state != "active":
                    conn.execute("COMMIT")
                    raise TaskLaunchError(f"Task {task.task_id} is not active")
                else:
                    conn.execute(
                        "UPDATE task_launches SET state = 'reserved', reservation_id = ?, "
                        "spawn_attempts = spawn_attempts + 1, updated_at = ? "
                        "WHERE task_id = ? AND state = 'active'",
                        (replacement, now, task.task_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM task_launches WHERE task_id = ?", (task.task_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
        except TaskLaunchError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskLaunchError(
                f"Cannot reserve failover for {source_session_id}: {exc}"
            ) from exc
        if blocked is not None:
            self._park_blocked(blocked)
            return LaunchReservation(blocked, None, False)
        return LaunchReservation(TaskLaunch(**dict(row)), replacement, True)

    def failed(self, task_id: str, reservation_id: str) -> TaskLaunch:
        task = self._transition(task_id, reservation_id, "retryable")
        if task.spawn_attempts >= task.max_spawn_attempts:
            try:
                with self.store._connect() as conn:
                    conn.execute(
                        "UPDATE task_launches SET state = 'blocked', updated_at = ? "
                        "WHERE task_id = ? AND state = 'retryable'",
                        (time.time(), task_id),
                    )
            except sqlite3.DatabaseError as exc:
                raise TaskLaunchError(f"Cannot block task {task_id}: {exc}") from exc
            task = self.get(task_id) or task
            self._park_blocked(task)
        return task

    def _transition(
        self, task_id: str, reservation_id: str, state: str, **values: object
    ) -> TaskLaunch:
        keep_reservation = bool(values.pop("keep_reservation", False))
        assignments = ["state = ?", "updated_at = ?"]
        if not keep_reservation:
            assignments.append("reservation_id = NULL")
        params: list[object] = [state, time.time()]
        for name in ("workspace_id", "session_id"):
            if name in values:
                assignments.append(f"{name} = ?")
                params.append(values[name])
        params.extend((task_id, reservation_id))
        try:
            with self.store._connect() as conn:
                changed = conn.execute(
                    f"UPDATE task_launches SET {', '.join(assignments)} "
                    "WHERE task_id = ? AND state = 'reserved' AND reservation_id = ?",
                    params,
                ).rowcount
        except sqlite3.DatabaseError as exc:
            raise TaskLaunchError(f"Cannot transition task {task_id}: {exc}") from exc
        if changed != 1:
            raise TaskLaunchError(f"Task {task_id} reservation is no longer owned")
        task = self.get(task_id)
        assert task is not None
        return task

    def _park_blocked(self, task: TaskLaunch) -> None:
        try:
            self.store.park(
                child_session_id=f"task:{task.task_id}",
                child_workspace_id=None,
                recorded_parent_session_id=None,
                reason="no_parent",
                message=(
                    f"BLOCKED: task {task.task_id} exceeded "
                    f"{task.max_spawn_attempts} spawn attempts"
                ),
                dedupe_key=f"task-spawn-circuit:{task.task_id}",
            )
        except EscalationStoreError as exc:
            raise TaskLaunchError(
                f"Cannot park blocked task {task.task_id}: {exc}"
            ) from exc
