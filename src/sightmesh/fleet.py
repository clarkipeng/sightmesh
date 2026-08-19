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

TERMINAL = frozenset({"completed", "done", "failed", "cancelled", "stopped"})
RUNNING = frozenset({"queued", "claimed", "running", "active"})
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
        selector=_base_selector(workspace_id, execution_id),
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
