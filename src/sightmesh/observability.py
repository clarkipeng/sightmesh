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
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import fleet
from .escalation import EscalationStore, EscalationStoreError, escalation_db_path
from .task_store import TaskStore, TaskStoreError

#: Newest-N bounds for one operator read. Every task surface is bounded for
#: the same reason: a queue that prints an entire history is not a queue, and
#: one measured `attention` emitted 33k rows / 10.8MB. `--all` opts out.
DEFAULT_UNACKED_LIMIT = 100
DEFAULT_TASK_LIMIT = 200

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
        """Attempts have reached the budget, so no further epoch is allowed.

        Answered by the one classifier the attention queue also uses, so a
        count and a queue row can never describe the same task differently.
        """
        return fleet.breaker_tripped(
            {
                "state": self.state,
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
            }
        )

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


def read_tasks(
    store: TaskStore,
    *,
    scope: str | None = None,
    limit: int | None = DEFAULT_TASK_LIMIT,
) -> list[TaskView]:
    """Read managed tasks straight from the kernel store.

    ``scope=None`` is the operator surface (every scope); a scope string is
    the task-local surface. One indexed ``SELECT`` either way.

    ``limit`` keeps the newest N tasks and still returns them oldest first.
    A host accumulates managed tasks forever, so an unbounded read grows
    without bound too; ``limit=None`` is the deliberate "show me everything"
    read behind an explicit flag.
    """
    if limit is not None and limit < 1:
        raise ValueError("Task read limit must be positive")
    columns = ", ".join(_TASK_COLUMNS)
    query = f"SELECT {columns} FROM managed_tasks"
    params: list[object] = []
    if scope is not None:
        query += " WHERE scope = ?"
        params.append(scope)
    if limit is None:
        query += " ORDER BY created_at ASC"
    else:
        query += " ORDER BY created_at DESC, task_id DESC LIMIT ?"
        params.append(limit)
    try:
        with store.connect() as conn:
            rows = conn.execute(query, params).fetchall()
    except (sqlite3.DatabaseError, EscalationStoreError) as exc:
        raise TaskStoreError(f"Cannot read the managed task surface: {exc}") from exc
    if limit is not None:
        rows = list(reversed(rows))
    return [_view(row) for row in rows]


def task_counts(views: Iterable[TaskView]) -> dict[str, int]:
    """Count states and tripped breakers with the shared classifier."""
    counts: dict[str, int] = {}
    tripped = 0
    for view in views:
        counts[view.state] = counts.get(view.state, 0) + 1
        tripped += int(view.breaker_tripped)
    counts["breaker_tripped"] = tripped
    return counts


def unacked_deliveries(
    escalations: EscalationStore,
    *,
    limit: int = DEFAULT_UNACKED_LIMIT,
    idle_recipients: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Deliveries the kernel accepted responsibility for but nobody has closed.

    Two kernel-owned kinds qualify: an escalation parked because no live
    parent could receive it, and an order whose recipient never reported
    back. Both are durable rows, so this read needs no executor.

    The read is bounded on purpose. Unmet orders accumulate for the lifetime
    of a host - one measured inbox held 33k of them, 10.8MB of output for a
    queue a human is supposed to work through - so at most ``limit`` rows
    are returned and everything past it is summarized in a single row rather
    than printed. ``idle_recipients`` is the executor-owned fact that an
    order's recipient is live and not mid-turn; when a caller holds it, only
    those recipients' orders are anyone's business right now.
    """
    if limit < 1:
        raise ValueError("Unacknowledged delivery limit must be positive")
    rows: list[dict[str, Any]] = [
        {
            "kind": "parked_escalation",
            "id": parked.escalation_id,
            "session_id": parked.child_session_id,
            "workspace_id": parked.child_workspace_id,
            "summary": parked.reason,
            "created_at": parked.created_at,
        }
        for parked in escalations.pending(limit=limit)
    ]
    rows.extend(
        {
            "kind": "unmet_order",
            "id": order.order_id,
            "session_id": order.recipient_session_id,
            "workspace_id": None,
            "summary": order.body,
            "created_at": order.created_at,
        }
        for order in escalations.orders(
            unmet_only=True,
            recipient_session_ids=idle_recipients,
            limit=limit,
        )
    )
    rows.sort(key=lambda row: (float(row["created_at"]), str(row["id"])))
    totals = escalations.unacked_counts(recipient_session_ids=idle_recipients)
    shown = rows[-limit:]
    suppressed = sum(totals.values()) - len(shown)
    if suppressed <= 0:
        return shown
    return [_suppressed_row(suppressed, shown), *shown]


def _suppressed_row(suppressed: int, shown: list[dict[str, Any]]) -> dict[str, Any]:
    """One aggregate row standing in for everything the bound left out."""
    oldest = min((float(row["created_at"]) for row in shown), default=0.0)
    return {
        "kind": "suppressed_unacked",
        "id": "suppressed",
        "session_id": None,
        "workspace_id": None,
        "summary": f"{suppressed} older unacknowledged deliveries suppressed",
        "created_at": oldest,
    }


def attention_facts(
    store: TaskStore,
    *,
    escalations: EscalationStore | None = None,
    scope: str | None = None,
    dirty_closeouts: tuple[Mapping[str, Any], ...] | None = None,
    failing_checks: tuple[Mapping[str, Any], ...] | None = None,
    idle_recipients: Collection[str] | None = None,
    limit: int | None = DEFAULT_TASK_LIMIT,
    tasks: Iterable[Mapping[str, Any]] | None = None,
) -> fleet.AttentionFacts:
    """Collect the kernel-owned half of the attention queue with zero fan-out.

    ``dirty_closeouts``, ``failing_checks`` and ``idle_recipients`` are
    cdesktop-owned facts. They stay ``None`` unless a caller already holds
    them, and the projection then reports those rows as degraded instead of
    inventing them.

    ``tasks`` lets a caller that has already read the bounded task rows pass
    them straight in, so a surface rendering both the queue and the groups
    reads the store once and both halves describe the same rows.
    """
    rows = (
        tuple(view.to_dict() for view in read_tasks(store, scope=scope, limit=limit))
        if tasks is None
        else tuple(dict(task) for task in tasks)
    )
    inbox = escalations or escalation_store(store.path)
    return fleet.AttentionFacts(
        tasks=rows,
        unacked=tuple(unacked_deliveries(inbox, idle_recipients=idle_recipients)),
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
