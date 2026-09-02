"""Pure, privacy-safe projection of native fleet facts.

This module deliberately owns no readers and writes no state.  Callers collect
facts from cdesktop, Git, Repowire, the account pool, and GitHub, then pass them
to :func:`overview`.  The resulting view is stable for a supplied ``now`` and
can therefore be rendered by either a CLI or UI without duplicating policy.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

TERMINAL = frozenset({"completed", "done", "failed", "cancelled", "stopped", "killed"})
RUNNING = frozenset({"queued", "claimed", "running", "active"})
#: Managed task states, as the kernel store writes them.
TASK_RUNNING_STATES = frozenset({"reserved", "active", "replacing"})
TASK_DONE_STATES = frozenset({"completed", "cancelled"})
#: Facts only the executor can produce, named here so an absent one is
#: reported as degraded rather than silently dropped from the queue.
DEGRADABLE_SOURCES = ("dirty_closeouts", "failing_checks")
_ATTENTION_PRIORITY = {
    "blocked_approval": 0,
    "tripped_breaker": 1,
    "blocked": 2,
    "lost": 3,
    "dirty_closeout": 4,
    "failing_check": 5,
    "unacked_delivery": 6,
}
_QUOTA_FIELDS = ("known", "remaining", "resetsAt", "resetsIn", "reason")
_EVENT_FIELDS = ("at", "kind", "status", "summary")
_TOKEN_USAGE_FIELDS = (
    "input",
    "output",
    "total",
    "cached",
    "reasoning",
    "unit",
    "provenance",
)
_COST_FIELDS = ("amount", "currency", "unit", "provenance")
_CONTEXT_FIELDS = ("used", "limit", "pressure")
_PARENT_FIELDS = ("id", "selector", "name", "status", "workspace_id")
_DELIVERY_FIELDS = ("pr", "ci", "ref", "status", "url")


@dataclass(frozen=True)
class FleetFacts:
    """Native observations, all optional and never persisted by this module."""

    workspaces: tuple[Mapping[str, Any], ...] = ()
    executions: tuple[Mapping[str, Any], ...] = ()
    approvals: tuple[Mapping[str, Any], ...] = ()
    relationships: tuple[Mapping[str, Any], ...] = ()
    accounts: tuple[Mapping[str, Any], ...] = ()
    deliveries: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class FleetItem:
    selector: str
    group: str
    urgency: str
    age_seconds: int | None
    reason: str
    next_action: str
    workspace_id: str | None
    execution_id: str
    status: str | None
    model: str | None
    provider: str | None
    account_id: str | None
    quota: Mapping[str, Any] | None
    last_event: Mapping[str, Any] | None
    token_usage: Mapping[str, Any] | None
    monetary_cost: Mapping[str, Any] | None
    context: Mapping[str, Any] | None
    parent: Mapping[str, Any] | None
    branch: str | None
    delivery: Mapping[str, Any] | None


@dataclass(frozen=True)
class FleetOverview:
    needs_attention: tuple[FleetItem, ...]
    running: tuple[FleetItem, ...]
    done_since_view: tuple[FleetItem, ...]

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Return only the fixed public schema intended for renderer input."""
        return {
            "needs_attention": [_public_item(item) for item in self.needs_attention],
            "running": [_public_item(item) for item in self.running],
            "done_since_view": [_public_item(item) for item in self.done_since_view],
        }


def overview(
    facts: FleetFacts,
    *,
    now: datetime,
    viewed_at: datetime | None = None,
) -> FleetOverview:
    """Project facts into deterministically ordered attention, running, and done groups.

    ``now`` is required so age and ordering do not depend on ambient clock time.
    A terminal execution appears in ``done_since_view`` only when its meaningful
    event is at or after ``viewed_at`` (or whenever ``viewed_at`` is omitted).
    """
    workspace_by_id = _index(facts.workspaces, "id")
    approvals = _by_execution(facts.approvals)
    relationships = _by_execution(facts.relationships)
    accounts = _index(facts.accounts, "id")
    deliveries = _by_execution(facts.deliveries)
    items = [
        _item(
            execution,
            workspace_by_id=workspace_by_id,
            approvals=approvals,
            relationships=relationships,
            accounts=accounts,
            deliveries=deliveries,
            now=now,
        )
        for execution in facts.executions
    ]
    items = _disambiguate(items)
    attention = sorted(
        (item for item in items if item.group == "needs_attention"), key=_order
    )
    running = sorted((item for item in items if item.group == "running"), key=_order)
    done = sorted(
        (
            item
            for item in items
            if item.group == "done_since_view"
            and (viewed_at is None or _event_time(item.last_event) >= viewed_at)
        ),
        key=_order,
    )
    return FleetOverview(tuple(attention), tuple(running), tuple(done))


@dataclass(frozen=True)
class AttentionFacts:
    """Kernel-owned facts, plus the two the executor alone can supply.

    ``dirty_closeouts`` and ``failing_checks`` are ``None`` when the caller
    does not already hold them; the queue then reports those sources as
    degraded instead of pretending they are empty.
    """

    tasks: tuple[Mapping[str, Any], ...] = ()
    unacked: tuple[Mapping[str, Any], ...] = ()
    dirty_closeouts: tuple[Mapping[str, Any], ...] | None = None
    failing_checks: tuple[Mapping[str, Any], ...] | None = None


@dataclass(frozen=True)
class AttentionItem:
    selector: str
    kind: str
    reason: str
    next_action: str
    scope: str | None
    task_key: str | None
    state: str | None
    workspace_id: str | None
    session_id: str | None
    age_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "kind": self.kind,
            "reason": self.reason,
            "next_action": self.next_action,
            "scope": self.scope,
            "task_key": self.task_key,
            "state": self.state,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True)
class AttentionQueue:
    items: tuple[AttentionItem, ...]
    degraded: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "degraded": [dict(entry) for entry in self.degraded],
        }


@dataclass(frozen=True)
class TaskGroups:
    needs_attention: tuple[Mapping[str, Any], ...]
    running: tuple[Mapping[str, Any], ...]
    done_since_view: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "needs_attention": [dict(row) for row in self.needs_attention],
            "running": [dict(row) for row in self.running],
            "done_since_view": [dict(row) for row in self.done_since_view],
        }


def attention(facts: AttentionFacts, *, now: datetime) -> AttentionQueue:
    """Project one ordered queue of everything a human must act on.

    The contract (docs/kernel-contract.md, "Observability") names five rows:
    unacknowledged deliveries, dirty closeouts, tripped breakers, blocked
    approvals, and failing checks. Three of them are kernel facts and are
    always answerable; the other two are executor facts and are reported as
    degraded when the caller holds none.
    """
    items = [_task_attention_item(task, now) for task in facts.tasks]
    items.extend(_delivery_attention_item(row, now) for row in facts.unacked)
    items.extend(
        _native_attention_item("dirty_closeout", row, now)
        for row in facts.dirty_closeouts or ()
    )
    items.extend(
        _native_attention_item("failing_check", row, now)
        for row in facts.failing_checks or ()
    )
    present = [item for item in items if item is not None]
    degraded = tuple(
        {
            "source": source,
            "status": "reported-degraded",
            "owner": "cdesktop",
            "reason": f"{source} is an executor-owned fact and was not supplied",
        }
        for source in DEGRADABLE_SOURCES
        if getattr(facts, source) is None
    )
    return AttentionQueue(tuple(sorted(present, key=_attention_order)), degraded)


def task_groups(
    tasks: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    viewed_at: datetime | None = None,
) -> TaskGroups:
    """Group managed tasks the way ``overview`` groups native executions."""
    rows = list(tasks)
    needs_attention = [row for row in rows if _task_attention_kind(row) is not None]
    running = [row for row in rows if str(row.get("state")) in TASK_RUNNING_STATES]
    done = [
        row
        for row in rows
        if str(row.get("state")) in TASK_DONE_STATES
        and (viewed_at is None or _task_time(row) >= viewed_at)
    ]
    def order(row: Mapping[str, Any]) -> tuple[int, str]:
        return -(_task_age(row, now) or 0), str(row.get("key") or "")

    return TaskGroups(
        tuple(sorted(needs_attention, key=order)),
        tuple(sorted(running, key=order)),
        tuple(sorted(done, key=order)),
    )


def breaker_tripped(task: Mapping[str, Any]) -> bool:
    """The one definition of an exhausted attempt budget, for every surface.

    ``status`` counts breakers, the attention queue classifies them and
    ``to_dict`` reports them. While each answered the question its own way
    the surfaces disagreed: ``status`` counted a tripped breaker that the
    queue described as merely "blocked" and advised replacing - which
    ``TaskStore.prepare_replacement`` rejects outright. A finished task never
    counts, however many attempts it spent.
    """
    if str(task.get("state") or "") in TASK_DONE_STATES:
        return False
    try:
        return int(task["attempts"]) >= int(task["max_attempts"])
    except (KeyError, TypeError, ValueError):
        return False


def _task_attention_kind(task: Mapping[str, Any]) -> str | None:
    # Budget first: an exhausted task cannot be replaced, so classifying it
    # by its state would hand the operator advice the store refuses.
    if breaker_tripped(task):
        return "tripped_breaker"
    state = str(task.get("state") or "")
    if state == "blocked":
        reason = str(task.get("result") or "").casefold()
        return "blocked_approval" if "approval" in reason else "blocked"
    if state == "lost":
        return "lost"
    return None


_ATTENTION_COPY = {
    "blocked_approval": (
        "Task is blocked on an approval.",
        "Answer the approval, then replace or complete the task.",
    ),
    "blocked": (
        "Task is blocked.",
        "Read the recorded block reason and replace or cancel the task.",
    ),
    "lost": (
        "Task lost its holder session.",
        "Replace the task to start a fresh epoch.",
    ),
    "tripped_breaker": (
        "Task has spent its whole attempt budget.",
        "Raise the budget deliberately or cancel the task; it cannot be replaced.",
    ),
}


def _task_attention_item(
    task: Mapping[str, Any], now: datetime
) -> AttentionItem | None:
    kind = _task_attention_kind(task)
    if kind is None:
        return None
    reason, next_action = _ATTENTION_COPY[kind]
    scope = _text(task.get("scope"))
    key = _text(task.get("key"))
    return AttentionItem(
        selector=f"task/{quote(scope or 'unknown', safe='')}/{quote(key or '', safe='')}",
        kind=kind,
        reason=reason,
        next_action=next_action,
        scope=scope,
        task_key=key,
        state=_text(task.get("state")),
        workspace_id=_text(task.get("workspace_id")),
        session_id=_text(task.get("session_id")),
        age_seconds=_task_age(task, now),
    )


def _delivery_attention_item(row: Mapping[str, Any], now: datetime) -> AttentionItem:
    kind = _text(row.get("kind")) or "delivery"
    identifier = _text(row.get("id")) or "unknown"
    return AttentionItem(
        selector=f"delivery/{quote(kind, safe='')}/{quote(identifier, safe='')}",
        kind="unacked_delivery",
        reason=f"Delivery is unacknowledged ({kind}).",
        next_action="Resolve the escalation or collect the outstanding report.",
        scope=None,
        task_key=None,
        state=kind,
        workspace_id=_text(row.get("workspace_id")),
        session_id=_text(row.get("session_id")),
        age_seconds=_age({"at": row.get("created_at")}, now),
    )


def _native_attention_item(
    kind: str, row: Mapping[str, Any], now: datetime
) -> AttentionItem:
    identifier = _text(row.get("id")) or _text(row.get("workspace_id")) or "unknown"
    reason = (
        "Closeout is dirty."
        if kind == "dirty_closeout"
        else "A delivery check is failing."
    )
    return AttentionItem(
        selector=f"{kind}/{quote(identifier, safe='')}",
        kind=kind,
        reason=reason,
        next_action="Reconcile the reported worktree or delivery reference.",
        scope=None,
        task_key=None,
        state=_text(row.get("status")),
        workspace_id=_text(row.get("workspace_id")),
        session_id=_text(row.get("session_id")),
        age_seconds=_age({"at": row.get("at")}, now),
    )


def _attention_order(item: AttentionItem) -> tuple[int, int, str]:
    return (
        _ATTENTION_PRIORITY[item.kind],
        -(item.age_seconds or 0),
        item.selector,
    )


def _task_time(task: Mapping[str, Any]) -> datetime:
    return _event_time({"at": task.get("updated_at")})


def _task_age(task: Mapping[str, Any], now: datetime) -> int | None:
    return _age({"at": task.get("updated_at")}, now)


def _item(
    execution: Mapping[str, Any],
    *,
    workspace_by_id: Mapping[str, Mapping[str, Any]],
    approvals: Mapping[str, Mapping[str, Any]],
    relationships: Mapping[str, Mapping[str, Any]],
    accounts: Mapping[str, Mapping[str, Any]],
    deliveries: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> FleetItem:
    execution_id = str(execution["id"])
    workspace_id = _text(execution.get("workspace_id"))
    workspace = workspace_by_id.get(workspace_id or "", {})
    approval = approvals.get(execution_id)
    account_id = _text(execution.get("account_id"))
    account = accounts.get(account_id or "", {})
    last_event = _mapping(execution.get("last_event"))
    age = _age(last_event, now)
    status = _text(execution.get("status"))
    group, urgency, reason, next_action = _decision(status, approval, account, age)
    model = _text(execution.get("model"))
    provider = _text(execution.get("provider")) or _text(account.get("provider"))
    return FleetItem(
        selector=_base_selector(
            workspace_id, _text(execution.get("session_id")) or execution_id
        ),
        group=group,
        urgency=urgency,
        age_seconds=age,
        reason=reason,
        next_action=next_action,
        workspace_id=workspace_id,
        execution_id=execution_id,
        status=status,
        model=model,
        provider=provider,
        account_id=account_id,
        quota=_mapping(account.get("quota")) or _mapping(execution.get("quota")),
        last_event=last_event,
        token_usage=_usage(execution.get("token_usage")),
        monetary_cost=_usage(execution.get("monetary_cost")),
        context=_mapping(execution.get("context")),
        parent=relationships.get(execution_id),
        branch=_text(execution.get("branch")) or _text(workspace.get("branch")),
        delivery=deliveries.get(execution_id),
    )


def _decision(
    status: str | None,
    approval: Mapping[str, Any] | None,
    account: Mapping[str, Any],
    age: int | None,
) -> tuple[str, str, str, str]:
    if approval and _text(approval.get("status")) in {"pending", "required"}:
        return (
            "needs_attention",
            "approval",
            "Approval is required.",
            "Review the approval.",
        )
    if status in {"failed", "blocked", "stalled"}:
        return (
            "needs_attention",
            "blocked",
            f"Execution is {status}.",
            "Inspect the last meaningful event.",
        )
    quota = _mapping(account.get("quota"))
    if quota and quota.get("known") and quota.get("remaining") == 0:
        return (
            "needs_attention",
            "quota",
            "Assigned account has no reported quota.",
            "Wait for the reported reset window.",
        )
    if status in RUNNING:
        return (
            "running",
            "normal",
            "Execution is active.",
            "Monitor the next meaningful event.",
        )
    if status in TERMINAL:
        return (
            "done_since_view",
            "normal",
            f"Execution is {status}.",
            "Inspect the delivery reference.",
        )
    return (
        "needs_attention",
        "unknown",
        "Execution state is not recognized.",
        "Inspect native execution state.",
    )


def _index(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    return {str(row[key]): row for row in rows if row.get(key) is not None}


def _by_execution(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return _index(rows, "execution_id")


def _base_selector(workspace_id: str | None, execution_id: str) -> str:
    return f"fleet/{quote(workspace_id or 'unassigned', safe='')}/{quote(execution_id, safe='')}"


def _disambiguate(items: list[FleetItem]) -> list[FleetItem]:
    counts: dict[str, int] = defaultdict(int)
    result = []
    for item in items:
        counts[item.selector] += 1
        suffix = "" if counts[item.selector] == 1 else f"~{counts[item.selector]}"
        result.append(FleetItem(**{**asdict(item), "selector": item.selector + suffix}))
    return result


def _order(item: FleetItem) -> tuple[int, int, str]:
    priority = {"approval": 0, "blocked": 1, "quota": 2, "unknown": 3, "normal": 4}
    return priority[item.urgency], -(item.age_seconds or 0), item.selector


def _usage(value: Any) -> Mapping[str, Any] | None:
    row = _mapping(value)
    if (
        not row
        or not isinstance(row.get("provenance"), str)
        or not row["provenance"].strip()
    ):
        return None
    return row


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _event_time(event: Mapping[str, Any] | None) -> datetime:
    if not event:
        return datetime.min.replace(tzinfo=UTC)
    value = event.get("at")
    if isinstance(value, datetime):
        return _utc(value)
    # Kernel store rows carry POSIX seconds, native rows carry ISO strings.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value))
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=UTC)


def _age(event: Mapping[str, Any] | None, now: datetime) -> int | None:
    if not event or _event_time(event) == datetime.min.replace(tzinfo=UTC):
        return None
    return max(0, int((_utc(now) - _event_time(event)).total_seconds()))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _public_item(item: FleetItem) -> dict[str, Any]:
    return {
        "selector": item.selector,
        "group": item.group,
        "urgency": item.urgency,
        "age_seconds": item.age_seconds,
        "reason": item.reason,
        "next_action": item.next_action,
        "workspace_id": item.workspace_id,
        "execution_id": item.execution_id,
        "status": item.status,
        "model": item.model,
        "provider": item.provider,
        "account_id": item.account_id,
        "quota": _project(item.quota, _QUOTA_FIELDS),
        "last_event": _project(item.last_event, _EVENT_FIELDS),
        "token_usage": _project(item.token_usage, _TOKEN_USAGE_FIELDS),
        "monetary_cost": _project(item.monetary_cost, _COST_FIELDS),
        "context": _project(item.context, _CONTEXT_FIELDS),
        "parent": _project(item.parent, _PARENT_FIELDS),
        "branch": item.branch,
        "delivery": _project(item.delivery, _DELIVERY_FIELDS),
    }


def _project(
    value: Mapping[str, Any] | None, fields: tuple[str, ...]
) -> dict[str, Any] | None:
    if value is None:
        return None
    projected = {key: _public_scalar(value[key]) for key in fields if key in value}
    return {key: item for key, item in projected.items() if item is not None}


def _public_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, datetime):
        return _utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None
