"""Task-local read model over the kernel stores.

Every function here answers from rows the kernel already owns in SQLite. The
module deliberately imports no executor client: a task surface that cannot
reach cdesktop cannot accidentally fan out, which is what makes
``docs/kernel-contract.md`` ("Observability") true by construction rather
than by a fast path someone can regress.

Native workspace inventory lives behind its own explicitly named command; it
never shares a code path with these readers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import fleet
from .escalation import EscalationStore, EscalationStoreError, escalation_db_path
from .task_store import TaskStore, TaskStoreError

_TASK_COLUMNS = (
    "task_id",
    "scope",
    "task_key",
    "parent_task_id",
    "state",
    "epoch",
    "attempts",
    "max_attempts",
    "child_limit",
    "workspace_id",
    "holder_session_id",
    "checkpoint",
    "result",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class TaskView:
    """One managed task as an operator reads it: no join, no native call."""

    task_id: str
    scope: str
    key: str
    parent_task_id: str | None
    state: str
    epoch: int
    attempts: int
    max_attempts: int
    child_limit: int
    workspace_id: str | None
    session_id: str | None
    checkpoint: str | None
    result: str | None
    created_at: float
    updated_at: float

    @property
    def breaker_tripped(self) -> bool:
        """Attempts have reached the budget, so no further epoch is allowed."""
        return self.attempts >= self.max_attempts

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "scope": self.scope,
            "key": self.key,
            "parent_task_id": self.parent_task_id,
            "state": self.state,
            "epoch": self.epoch,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "breaker_tripped": self.breaker_tripped,
            "child_limit": self.child_limit,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "checkpoint": self.checkpoint,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def task_store(path: Path | None = None) -> TaskStore:
    """Open the kernel task store; a single seam tests can replace."""
    return TaskStore(path)


def escalation_store(path: Path | None = None) -> EscalationStore:
    return EscalationStore(path or escalation_db_path())


def read_tasks(store: TaskStore, *, scope: str | None = None) -> list[TaskView]:
    """Read managed tasks straight from the kernel store.

    ``scope=None`` is the operator surface (every scope); a scope string is
    the task-local surface. One indexed ``SELECT`` either way.
    """
    columns = ", ".join(_TASK_COLUMNS)
    query = f"SELECT {columns} FROM managed_tasks"
    params: tuple[object, ...] = ()
    if scope is not None:
        query += " WHERE scope = ?"
        params = (scope,)
    query += " ORDER BY created_at"
    try:
        with store.connect() as conn:
            rows = conn.execute(query, params).fetchall()
    except (sqlite3.DatabaseError, EscalationStoreError) as exc:
        raise TaskStoreError(f"Cannot read the managed task surface: {exc}") from exc
    return [_view(row) for row in rows]


def task_counts(views: Iterable[TaskView]) -> dict[str, int]:
    counts: dict[str, int] = {}
    tripped = 0
    for view in views:
        counts[view.state] = counts.get(view.state, 0) + 1
        tripped += int(view.breaker_tripped and view.state not in fleet.TASK_DONE_STATES)
    counts["breaker_tripped"] = tripped
    return counts


def unacked_deliveries(
    escalations: EscalationStore, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Deliveries the kernel accepted responsibility for but nobody has closed.

    Two kernel-owned kinds qualify: an escalation parked because no live
    parent could receive it, and an order whose recipient never reported
    back. Both are durable rows, so this read needs no executor.
    """
    rows: list[dict[str, Any]] = []
    for parked in escalations.pending(limit=limit):
        rows.append(
            {
                "kind": "parked_escalation",
                "id": parked.escalation_id,
                "session_id": parked.child_session_id,
                "workspace_id": parked.child_workspace_id,
                "summary": parked.reason,
                "created_at": parked.created_at,
            }
        )
    for order in escalations.orders(unmet_only=True):
        rows.append(
            {
                "kind": "unmet_order",
                "id": order.order_id,
                "session_id": order.recipient_session_id,
                "workspace_id": None,
                "summary": order.body,
                "created_at": order.created_at,
            }
        )
    return sorted(rows, key=lambda row: (float(row["created_at"]), str(row["id"])))


def attention_facts(
    store: TaskStore,
    *,
    escalations: EscalationStore | None = None,
    scope: str | None = None,
    dirty_closeouts: tuple[Mapping[str, Any], ...] | None = None,
    failing_checks: tuple[Mapping[str, Any], ...] | None = None,
) -> fleet.AttentionFacts:
    """Collect the kernel-owned half of the attention queue with zero fan-out.

    ``dirty_closeouts`` and ``failing_checks`` are cdesktop-owned facts. They
    stay ``None`` unless a caller already holds them, and the projection then
    reports those rows as degraded instead of inventing them.
    """
    tasks = read_tasks(store, scope=scope)
    inbox = escalations or escalation_store(store.path)
    return fleet.AttentionFacts(
        tasks=tuple(view.to_dict() for view in tasks),
        unacked=tuple(unacked_deliveries(inbox)),
        dirty_closeouts=dirty_closeouts,
        failing_checks=failing_checks,
    )


def _view(row: Mapping[str, Any]) -> TaskView:
    return TaskView(
        task_id=str(row["task_id"]),
        scope=str(row["scope"]),
        key=str(row["task_key"]),
        parent_task_id=row["parent_task_id"],
        state=str(row["state"]),
        epoch=int(row["epoch"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        child_limit=int(row["child_limit"]),
        workspace_id=row["workspace_id"],
        session_id=row["holder_session_id"],
        checkpoint=row["checkpoint"],
        result=row["result"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
