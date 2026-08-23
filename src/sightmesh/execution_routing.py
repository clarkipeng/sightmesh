"""Execution routing policy: ordered routes over the authoritative pool.

A route names an executor, a model, a billing class, and either a pool of
owned accounts (subscription) or one fixed account (metered). Selection walks
routes in configured order and, within each route, accounts in pool order -
`pool_core` stays the single source of account identity, credential, cooldown,
and quota truth. Nothing here mirrors or caches pool state; every call re-reads
it, so a newly added account or a freshly cooled one participates immediately.

Settings never carry tokens, headers, credential paths, or provider response
bodies - only route shape and policy.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .pool import core as pool_core

SETTINGS_VERSION = 1

EXECUTORS = {"CLAUDE_CODE", "CODEX", "OPENCODE"}
BILLING_CLASSES = {"subscription", "metered", "free"}

# A free route bills nothing and therefore owns no account. Selection still has
# to hand the launcher *some* binding id, so it gets this fixed sentinel - which
# resolves to no credential anywhere, keeping "free never spends" true by
# construction rather than by remembering to check.
FREE_AUTH_BINDING = "free"
METERED_FALLBACK_VALUES = {"auto", "ask", "never"}
ALL_ROUTES_EXHAUSTED_VALUES = {"block"}

MAX_SAME_ROUTE_RETRIES = 3
MAX_BACKOFF_SECONDS = 3600
MAX_BACKOFF_STEPS = 10

# How a free route's terminal failure is described to a human. This is a
# report, never routing truth: nothing here is persisted, and no selection
# consults it. Only the explicit `fallbackOnFreeFailure` policy may change
# which route runs next, so a misread outcome can cost visibility at worst -
# it can never silently move work onto a billed account.
MODEL_UNAVAILABLE = "model_unavailable"
PROVIDER_REJECTED = "provider_rejected"
UNKNOWN_FAILURE = "unknown"
FREE_FAILURE_OUTCOMES = (MODEL_UNAVAILABLE, PROVIDER_REJECTED, UNKNOWN_FAILURE)

_MODEL_UNAVAILABLE_RE = re.compile(
    r"model not found|unknown model|no such model"
    r"|model .* (?:is )?(?:not available|unavailable)",
    re.IGNORECASE,
)


class ExecutionRoutingError(RuntimeError):
    pass


def default_settings_path() -> Path:
    return Path.home() / ".config" / "sightmesh" / "execution_routing.json"


# ---------------------------------------------------------------- settings model


@dataclass(frozen=True)
class Route:
    id: str
    executor: str
    model: str
    billing_class: str
    account_pool: str | None = None
    account: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ExecutionRoutingError("Route id must not be empty")
        if self.executor not in EXECUTORS:
            raise ExecutionRoutingError(f"Unsupported executor: {self.executor}")
        if not self.model.strip():
            raise ExecutionRoutingError(f"Route {self.id} must specify a model")
        if self.billing_class not in BILLING_CLASSES:
            raise ExecutionRoutingError(
                f"Unsupported billing class: {self.billing_class}"
            )
        if self.billing_class == "subscription":
            if not self.account_pool:
                raise ExecutionRoutingError(
                    f"Route {self.id} requires accountPool for a subscription route"
                )
            if self.account_pool not in pool_core.PROVIDERS:
                raise ExecutionRoutingError(
                    f"Route {self.id} has an unknown accountPool: {self.account_pool}"
                )
            if self.account:
                raise ExecutionRoutingError(
                    f"Route {self.id} must not set account for a subscription route"
                )
        elif self.billing_class == "metered":
            if not self.account:
                raise ExecutionRoutingError(
                    f"Route {self.id} requires account for a metered route"
                )
            if self.account_pool:
                raise ExecutionRoutingError(
                    f"Route {self.id} must not set accountPool for a metered route"
                )
        elif self.account or self.account_pool:
            raise ExecutionRoutingError(
                f"Route {self.id} must not name an account or accountPool for a "
                "free route"
            )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "executor": self.executor,
            "model": self.model,
            "billingClass": self.billing_class,
        }
        if self.account_pool:
            data["accountPool"] = self.account_pool
        if self.account:
            data["account"] = self.account
        return data

    @staticmethod
    def from_dict(value: Any) -> "Route":
        if not isinstance(value, dict):
            raise ExecutionRoutingError("Invalid route entry")
        try:
            return Route(
                id=value["id"],
                executor=value["executor"],
                model=value["model"],
                billing_class=value["billingClass"],
                account_pool=value.get("accountPool"),
                account=value.get("account"),
            )
        except KeyError as exc:
            raise ExecutionRoutingError(f"Route entry missing field: {exc}") from exc


@dataclass(frozen=True)
class ExecutionRoutingSettings:
    enabled: bool = True
    routes: tuple[Route, ...] = ()
    metered_fallback: str = "auto"
    same_route_retries: int = 2
    transient_backoff_seconds: tuple[int, ...] = (5, 20)
    approval_timeout_minutes: int = 0
    all_routes_exhausted: str = "block"
    notify_on_swap: bool = True
    expose_account_alias: bool = True
    # A free route that fails terminally is always reported. Moving that work
    # onto an account that bills is a separate, explicit decision, so this
    # stays off unless the operator opts in.
    fallback_on_free_failure: bool = False

    def __post_init__(self) -> None:
        if self.metered_fallback not in METERED_FALLBACK_VALUES:
            raise ExecutionRoutingError(
                f"Unsupported meteredFallback: {self.metered_fallback}"
            )
        if not 0 <= self.same_route_retries <= MAX_SAME_ROUTE_RETRIES:
            raise ExecutionRoutingError(
                f"sameRouteRetries must be between 0 and {MAX_SAME_ROUTE_RETRIES}"
            )
        if not self.transient_backoff_seconds:
            raise ExecutionRoutingError("transientBackoffSeconds must not be empty")
        if len(self.transient_backoff_seconds) > MAX_BACKOFF_STEPS:
            raise ExecutionRoutingError(
                f"transientBackoffSeconds must have at most {MAX_BACKOFF_STEPS} entries"
            )
        for seconds in self.transient_backoff_seconds:
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, int)
                or not 0 < seconds <= MAX_BACKOFF_SECONDS
            ):
                raise ExecutionRoutingError(
                    "transientBackoffSeconds values must be positive integers"
                    f" up to {MAX_BACKOFF_SECONDS}"
                )
        if self.approval_timeout_minutes < 0:
            raise ExecutionRoutingError(
                "approvalTimeoutMinutes must be 0 or a positive integer"
            )
        if self.all_routes_exhausted not in ALL_ROUTES_EXHAUSTED_VALUES:
            raise ExecutionRoutingError(
                f"Unsupported allRoutesExhausted: {self.all_routes_exhausted}"
            )
        ids = [route.id for route in self.routes]
        if len(ids) != len(set(ids)):
            raise ExecutionRoutingError("Route ids must be unique")

    def to_dict(self) -> dict[str, Any]:
        return _settings_to_dict(self)


def _settings_to_dict(settings: ExecutionRoutingSettings) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "routes": [route.to_dict() for route in settings.routes],
        "meteredFallback": settings.metered_fallback,
        "sameRouteRetries": settings.same_route_retries,
        "transientBackoffSeconds": list(settings.transient_backoff_seconds),
        "approvalTimeoutMinutes": settings.approval_timeout_minutes,
        "allRoutesExhausted": settings.all_routes_exhausted,
        "notifyOnSwap": settings.notify_on_swap,
        "exposeAccountAlias": settings.expose_account_alias,
        "fallbackOnFreeFailure": settings.fallback_on_free_failure,
    }


def _settings_from_dict(data: Any) -> ExecutionRoutingSettings:
    if not isinstance(data, dict):
        raise ExecutionRoutingError("Invalid executionRouting settings")
    defaults = ExecutionRoutingSettings()
    routes = tuple(Route.from_dict(item) for item in data.get("routes", []))
    try:
        return ExecutionRoutingSettings(
            enabled=data.get("enabled", defaults.enabled),
            routes=routes,
            metered_fallback=data.get("meteredFallback", defaults.metered_fallback),
            same_route_retries=data.get(
                "sameRouteRetries", defaults.same_route_retries
            ),
            transient_backoff_seconds=tuple(
                data.get(
                    "transientBackoffSeconds", defaults.transient_backoff_seconds
                )
            ),
            approval_timeout_minutes=data.get(
                "approvalTimeoutMinutes", defaults.approval_timeout_minutes
            ),
            all_routes_exhausted=data.get(
                "allRoutesExhausted", defaults.all_routes_exhausted
            ),
            notify_on_swap=data.get("notifyOnSwap", defaults.notify_on_swap),
            expose_account_alias=data.get(
                "exposeAccountAlias", defaults.expose_account_alias
            ),
            fallback_on_free_failure=data.get(
                "fallbackOnFreeFailure", defaults.fallback_on_free_failure
            ),
        )
    except ExecutionRoutingError:
        raise
    except (TypeError, ValueError) as exc:
        raise ExecutionRoutingError(f"Invalid routing settings: {exc}") from exc


class ExecutionRoutingStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_settings_path()

    def load(self) -> ExecutionRoutingSettings:
        return _settings_from_dict(self._read().get("executionRouting", {}))

    def save(self, settings: ExecutionRoutingSettings) -> ExecutionRoutingSettings:
        # Round-trip through the dataclass constructor so an invalid value is
        # rejected before anything reaches disk.
        validated = replace(settings)
        self._write(
            {"version": SETTINGS_VERSION, "executionRouting": _settings_to_dict(validated)}
        )
        return validated

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SETTINGS_VERSION, "executionRouting": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutionRoutingError(
                f"Cannot read routing settings {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != SETTINGS_VERSION:
            raise ExecutionRoutingError(
                f"Unsupported routing settings version: {self.path}"
            )
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=".execution_routing.", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)


def route_warnings(settings: ExecutionRoutingSettings) -> list[str]:
    """Routes that cannot currently resolve to any pool account. Advisory only."""
    pool = pool_core.load_pool()
    state = pool_core.load_state()
    warnings: list[str] = []
    for route in settings.routes:
        if route.billing_class == "free":
            continue
        if not any(
            _account_eligibility(account, state, frozenset())[0]
            for account in _route_candidates(route, pool)
        ):
            warnings.append(f"route {route.id}: no eligible account")
    return warnings


def classify_free_failure(output: str) -> str:
    """Name what a free route's terminal failure looked like, for a human.

    The caller passes whatever the executor printed. A recognisable
    "model not found" is reported as ``model_unavailable``; any other
    non-empty failure text is a ``provider_rejected`` report; nothing at all
    is ``unknown``. The result is only ever carried in an escalation message,
    so it never becomes routing state - see FREE_FAILURE_OUTCOMES.
    """
    text = str(output or "").strip()
    if not text:
        return UNKNOWN_FAILURE
    if _MODEL_UNAVAILABLE_RE.search(text):
        return MODEL_UNAVAILABLE
    return PROVIDER_REJECTED


# ---------------------------------------------------------------- selection


@dataclass(frozen=True)
class SelectedTarget:
    route_id: str
    executor: str
    model: str
    billing_class: str
    auth_binding_id: str
    account_alias: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SelectionResult:
    status: str  # "resolved" | "approval_needed" | "blocked"
    target: SelectedTarget | None
    trace: tuple[str, ...]
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target.to_dict() if self.target else None,
            "trace": list(self.trace),
            "reason": self.reason,
        }


def _account_eligibility(
    account: dict[str, Any],
    state: dict[str, Any],
    exclude_account_ids: frozenset[str],
) -> tuple[bool, str]:
    aid = account.get("id", "")
    if aid in exclude_account_ids:
        return False, "excluded: prior failed binding"
    if account.get("disabled"):
        return False, "account disabled"
    if not _has_launch_credential(account):
        return False, "no credential stored"
    until = pool_core.cooling_until(state, aid)
    if until:
        return False, f"cooling {pool_core.fmt_delta(until - time.time())}"
    usage = pool_core.quota_cached(account)
    if usage.get("known") and usage.get("remaining") == 0:
        return False, "zero quota"
    return True, "eligible"


def _has_launch_credential(account: dict[str, Any]) -> bool:
    """Check launch-material presence without reading secret-bearing material."""
    if account.get("provider") == "codex":
        return bool(account.get("codex_home"))
    return pool_core.token_path(account.get("id", "")).exists()


def _route_candidates(route: Route, pool: dict[str, Any]) -> list[dict[str, Any]]:
    if route.billing_class == "subscription":
        # A provider pool may contain API-key entries; they are never valid for
        # a subscription route even when they share its provider.
        return [
            account
            for account in pool_core.accounts_for(pool, route.account_pool)
            if account.get("kind") != "apikey"
        ]
    found = pool_core.find(pool, route.account)
    return [found] if found else []


def _trace_account(account: dict[str, Any], settings: ExecutionRoutingSettings) -> str:
    return account.get("id", "account") if settings.expose_account_alias else "account"


def _target(
    route: Route, account: dict[str, Any] | None, settings: ExecutionRoutingSettings
) -> SelectedTarget:
    account_id = account["id"] if account else None
    return SelectedTarget(
        route_id=route.id,
        executor=route.executor,
        model=route.model,
        billing_class=route.billing_class,
        auth_binding_id=account_id or FREE_AUTH_BINDING,
        account_alias=account_id if settings.expose_account_alias else None,
    )


def select_route(
    settings: ExecutionRoutingSettings,
    *,
    preferred_model: str | None = None,
    exclude_account_ids: frozenset[str] = frozenset(),
    exclude_route_ids: frozenset[str] = frozenset(),
) -> SelectionResult:
    """Walk configured routes in order and return a safe selection outcome.

    Only ever produces an opaque `auth_binding_id` (the pool account id) - never
    a resolved credential. That resolution boundary belongs to the executor
    launcher, not this selector.

    A route that just failed is excluded by id, not by binding: every free
    route shares the FREE_AUTH_BINDING sentinel, so account exclusion cannot
    name one of them without naming them all.
    """
    trace: list[str] = []

    if not settings.enabled:
        trace.append("execution routing disabled")
        return SelectionResult("blocked", None, tuple(trace), "routing_disabled")
    if not settings.routes:
        trace.append("no routes configured")
        return SelectionResult("blocked", None, tuple(trace), "routes_exhausted")

    # Loaded on the first route that actually needs an account, so a free route
    # reaching the front of the list never touches pool state at all.
    pool_state: tuple[dict[str, Any], dict[str, Any]] | None = None

    def pool_and_state() -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal pool_state
        if pool_state is None:
            pool_state = (pool_core.load_pool(), pool_core.load_state())
        return pool_state

    for route in settings.routes:
        if route.id in exclude_route_ids:
            trace.append(f"route {route.id}: skip, excluded by caller")
            continue
        if preferred_model and route.model != preferred_model:
            trace.append(
                f"route {route.id}: skip, model {route.model} != "
                f"preferred {preferred_model}"
            )
            continue

        if route.billing_class == "free":
            trace.append(f"selected route {route.id}: free, no account required")
            return SelectionResult(
                "resolved", _target(route, None, settings), tuple(trace), None
            )

        if route.billing_class == "metered" and settings.metered_fallback == "never":
            trace.append(
                f"route {route.id}: skip, metered route blocked by "
                "meteredFallback=never"
            )
            continue

        pool, state = pool_and_state()
        candidates = _route_candidates(route, pool)
        if route.billing_class == "metered" and not candidates:
            account = route.account if settings.expose_account_alias else "account"
            trace.append(f"route {route.id}: {account} not in pool")

        eligible_account: dict[str, Any] | None = None
        for account in candidates:
            ok, note = _account_eligibility(account, state, exclude_account_ids)
            if not ok:
                trace.append(
                    f"route {route.id}: skip {_trace_account(account, settings)}: {note}"
                )
                continue
            eligible_account = account
            trace.append(f"route {route.id}: {_trace_account(account, settings)} eligible")
            break

        if eligible_account is None:
            trace.append(f"route {route.id}: no eligible account")
            continue

        if route.billing_class == "metered" and settings.metered_fallback == "ask":
            target = _target(route, eligible_account, settings)
            trace.append(
                f"route {route.id}: metered route reached, meteredFallback=ask, "
                "approval required"
            )
            return SelectionResult(
                "approval_needed", target, tuple(trace), "approval_needed"
            )

        target = _target(route, eligible_account, settings)
        trace.append(f"selected route {route.id}")
        return SelectionResult("resolved", target, tuple(trace), None)

    trace.append("all configured routes exhausted")
    return SelectionResult("blocked", None, tuple(trace), "routes_exhausted")
