"""Execution routing policy: per-class route chains over the authoritative pool.

A route names an executor, a model, a billing class, and either a pool of
owned accounts (subscription) or one fixed account (metered). Routes are
grouped into *classes* - the unit a manager selects - and each class holds one
ordered fallback chain. Selection walks that chain in order and, within each
route, accounts in pool order - `pool_core` stays the single source of account
identity, credential, cooldown, and quota truth. Nothing here mirrors or caches
pool state; every call re-reads it, so a newly added account or a freshly
cooled one participates immediately.

Class membership is the only closed set here. Which models a class contains is
operator data in ``Route.model``, so adding or renaming a model is a settings
edit, never a code change.

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

SETTINGS_VERSION = 2
#: Settings shapes this module can still read. A v1 file carries one flat
#: ``routes`` list, which migrates forward into the ``standard`` chain.
READABLE_SETTINGS_VERSIONS = (1, SETTINGS_VERSION)

EXECUTORS = {"CLAUDE_CODE", "CODEX", "OPENCODE"}
BILLING_CLASSES = {"subscription", "metered", "free"}

#: The two route classes the contract names (docs/kernel-contract.md,
#: "Routing"). This is the only closed set the class model introduces: the
#: models a class chains through live in ``Route.model`` as operator data.
ROUTE_CLASSES = ("standard", "deep")
DEFAULT_ROUTE_CLASS = "standard"

#: A top-level supervised task that fans out at least this many children is a
#: manager: its judgement propagates to every child, which is the risk the
#: deep chain exists to cover.
DEEP_CLASS_MIN_CHILDREN = 1

# A free route bills nothing and therefore owns no account. Selection still has
# to hand the launcher *some* binding id, so it gets this fixed sentinel - which
# resolves to no credential anywhere, keeping "free never spends" true by
# construction rather than by remembering to check.
FREE_AUTH_BINDING = "free"
METERED_FALLBACK_VALUES = {"auto", "ask", "never"}
ALL_ROUTES_EXHAUSTED_VALUES = {"block"}

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
class RouteChain:
    """One route class and its ordered fallback chain.

    The chain is what the contract calls terra->luna->sol: each hop is one
    ``Route``, and selection advances a hop only when every account of the
    current one is ineligible.
    """

    route_class: str
    routes: tuple[Route, ...] = ()

    def __post_init__(self) -> None:
        if self.route_class not in ROUTE_CLASSES:
            raise ExecutionRoutingError(f"Unsupported route class: {self.route_class}")
        ids = [route.id for route in self.routes]
        if len(ids) != len(set(ids)):
            raise ExecutionRoutingError(
                f"Route ids must be unique within class {self.route_class}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "routeClass": self.route_class,
            "routes": [route.to_dict() for route in self.routes],
        }

    @staticmethod
    def from_dict(value: Any) -> "RouteChain":
        if not isinstance(value, dict):
            raise ExecutionRoutingError("Invalid route chain entry")
        try:
            return RouteChain(
                route_class=value["routeClass"],
                routes=tuple(Route.from_dict(item) for item in value.get("routes", [])),
            )
        except KeyError as exc:
            raise ExecutionRoutingError(
                f"Route chain entry missing field: {exc}"
            ) from exc


@dataclass(frozen=True)
class ExecutionRoutingSettings:
    enabled: bool = True
    chains: tuple[RouteChain, ...] = ()
    default_class: str = DEFAULT_ROUTE_CLASS
    metered_fallback: str = "auto"
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
        if self.default_class not in ROUTE_CLASSES:
            raise ExecutionRoutingError(
                f"Unsupported defaultClass: {self.default_class}"
            )
        if self.approval_timeout_minutes < 0:
            raise ExecutionRoutingError(
                "approvalTimeoutMinutes must be 0 or a positive integer"
            )
        if self.all_routes_exhausted not in ALL_ROUTES_EXHAUSTED_VALUES:
            raise ExecutionRoutingError(
                f"Unsupported allRoutesExhausted: {self.all_routes_exhausted}"
            )
        classes = [chain.route_class for chain in self.chains]
        if len(classes) != len(set(classes)):
            raise ExecutionRoutingError("Each route class may hold only one chain")

    def chain(self, route_class: str | None = None) -> RouteChain | None:
        """The chain for a class, or ``None`` when the class is unconfigured."""
        wanted = route_class or self.default_class
        return next(
            (chain for chain in self.chains if chain.route_class == wanted), None
        )

    def routes_for(self, route_class: str | None = None) -> tuple[Route, ...]:
        chain = self.chain(route_class)
        return chain.routes if chain else ()

    def route(self, route_class: str | None, route_id: str) -> Route | None:
        return next(
            (route for route in self.routes_for(route_class) if route.id == route_id),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return _settings_to_dict(self)


def _settings_to_dict(settings: ExecutionRoutingSettings) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "chains": [chain.to_dict() for chain in settings.chains],
        "defaultClass": settings.default_class,
        "meteredFallback": settings.metered_fallback,
        "approvalTimeoutMinutes": settings.approval_timeout_minutes,
        "allRoutesExhausted": settings.all_routes_exhausted,
        "notifyOnSwap": settings.notify_on_swap,
        "exposeAccountAlias": settings.expose_account_alias,
        "fallbackOnFreeFailure": settings.fallback_on_free_failure,
    }


def _chains_from_dict(data: dict[str, Any]) -> tuple[RouteChain, ...]:
    """Read v2 ``chains``, or migrate a v1 flat ``routes`` list forward.

    v1 had no class concept, so its single ordered list is exactly the
    ``standard`` chain. The migration is pure and total: it needs no default
    and can never invent a hop the operator did not configure.
    """
    if "chains" in data:
        return tuple(RouteChain.from_dict(item) for item in data.get("chains") or [])
    legacy = tuple(Route.from_dict(item) for item in data.get("routes") or [])
    return (RouteChain(DEFAULT_ROUTE_CLASS, legacy),) if legacy else ()


def _settings_from_dict(data: Any) -> ExecutionRoutingSettings:
    if not isinstance(data, dict):
        raise ExecutionRoutingError("Invalid executionRouting settings")
    defaults = ExecutionRoutingSettings()
    try:
        return ExecutionRoutingSettings(
            enabled=data.get("enabled", defaults.enabled),
            chains=_chains_from_dict(data),
            default_class=data.get("defaultClass", defaults.default_class),
            metered_fallback=data.get("meteredFallback", defaults.metered_fallback),
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
        if (
            not isinstance(payload, dict)
            or payload.get("version") not in READABLE_SETTINGS_VERSIONS
        ):
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
    """Routes that cannot currently resolve to any pool account. Advisory only.

    Advisory means exactly that: a warning never blocks a dispatch. The gate
    that does is :func:`validate_chain`, which asks the stronger question -
    does this *class* still have one usable hop.
    """
    pool = pool_core.load_pool()
    state = pool_core.load_state()
    warnings: list[str] = []
    for chain in settings.chains:
        for route in chain.routes:
            if route.billing_class == "free":
                continue
            if not any(
                _account_eligibility(account, state, frozenset())[0]
                for account in _route_candidates(route, pool)
            ):
                warnings.append(
                    f"class {chain.route_class}: route {route.id}: no eligible account"
                )
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
    route_class: str
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
    route: Route,
    account: dict[str, Any] | None,
    settings: ExecutionRoutingSettings,
    route_class: str,
) -> SelectedTarget:
    account_id = account["id"] if account else None
    return SelectedTarget(
        route_class=route_class,
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
    route_class: str | None = None,
    preferred_model: str | None = None,
    exclude_account_ids: frozenset[str] = frozenset(),
    exclude_route_ids: frozenset[str] = frozenset(),
) -> SelectionResult:
    """Walk one class chain in order and return a safe selection outcome.

    Selection never leaves the class it was asked for. That is what makes a
    failover stay on the chain the manager was dispatched onto: the class is
    frozen at dispatch, and every later hop walks the same chain.

    Only ever produces an opaque `auth_binding_id` (the pool account id) - never
    a resolved credential. That resolution boundary belongs to the executor
    launcher, not this selector.

    A route that just failed is excluded by id, not by binding: every free
    route shares the FREE_AUTH_BINDING sentinel, so account exclusion cannot
    name one of them without naming them all.
    """
    trace: list[str] = []
    wanted = route_class or settings.default_class

    if not settings.enabled:
        trace.append("execution routing disabled")
        return SelectionResult("blocked", None, tuple(trace), "routing_disabled")
    if wanted not in ROUTE_CLASSES:
        trace.append(f"unknown route class {wanted}")
        return SelectionResult("blocked", None, tuple(trace), "unknown_route_class")
    routes = settings.routes_for(wanted)
    if not routes:
        trace.append(f"class {wanted}: no routes configured")
        return SelectionResult("blocked", None, tuple(trace), "routes_exhausted")

    # Loaded on the first route that actually needs an account, so a free route
    # reaching the front of the list never touches pool state at all.
    pool_state: tuple[dict[str, Any], dict[str, Any]] | None = None

    def pool_and_state() -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal pool_state
        if pool_state is None:
            pool_state = (pool_core.load_pool(), pool_core.load_state())
        return pool_state

    for route in routes:
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
                "resolved",
                _target(route, None, settings, wanted),
                tuple(trace),
                None,
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
            target = _target(route, eligible_account, settings, wanted)
            trace.append(
                f"route {route.id}: metered route reached, meteredFallback=ask, "
                "approval required"
            )
            return SelectionResult(
                "approval_needed", target, tuple(trace), "approval_needed"
            )

        target = _target(route, eligible_account, settings, wanted)
        trace.append(f"selected route {route.id}")
        return SelectionResult("resolved", target, tuple(trace), None)

    trace.append(f"class {wanted}: all configured routes exhausted")
    return SelectionResult("blocked", None, tuple(trace), "routes_exhausted")


# ---------------------------------------------------------------- class policy


@dataclass(frozen=True)
class ScopeRisk:
    """The dispatch-time facts a route class is decided from.

    Derived from the worker spec alone, so the decision is reproducible from
    the persisted task and never depends on live pool or provider state.
    """

    route_class: str | None = None
    permission: str = "SUPERVISED"
    top_level: bool = True
    children: int = 0


def class_for(scope_risk: ScopeRisk, settings: ExecutionRoutingSettings) -> str:
    """Standard for ordinary work; deep when scope and risk demand it.

    Deliberately thin. An explicit operator choice always wins; otherwise the
    deep chain is reserved for a top-level supervised manager that fans work
    out, because that is the one shape where a weak judgement is multiplied
    across children rather than confined to one worker.
    """
    if scope_risk.route_class:
        if scope_risk.route_class not in ROUTE_CLASSES:
            raise ExecutionRoutingError(
                f"Unsupported route class: {scope_risk.route_class}"
            )
        return scope_risk.route_class
    if (
        scope_risk.permission == "SUPERVISED"
        and scope_risk.top_level
        and scope_risk.children >= DEEP_CLASS_MIN_CHILDREN
    ):
        return "deep"
    return settings.default_class


# ---------------------------------------------------------------- validation


@dataclass(frozen=True)
class ValidationResult:
    route_class: str
    valid: bool
    reason: str | None
    trace: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "routeClass": self.route_class,
            "valid": self.valid,
            "reason": self.reason,
            "trace": list(self.trace),
        }


def validate_chain(
    settings: ExecutionRoutingSettings, route_class: str | None = None
) -> ValidationResult:
    """Prove one class chain can currently run something, before dispatch.

    This is the fail-closed gate: an unconfigured class, an empty chain, or a
    chain whose every hop is ineligible is invalid, and a caller that honours
    it never opens an epoch it cannot fill. A metered hop awaiting approval
    still counts as a usable path - the work has somewhere to go, it just
    needs a human first.
    """
    wanted = route_class or settings.default_class
    if wanted not in ROUTE_CLASSES:
        return ValidationResult(wanted, False, "unknown_route_class")
    if not settings.enabled:
        return ValidationResult(wanted, False, "routing_disabled")
    if not settings.routes_for(wanted):
        return ValidationResult(wanted, False, "routes_exhausted")
    result = select_route(settings, route_class=wanted)
    valid = result.status in {"resolved", "approval_needed"}
    return ValidationResult(
        wanted, valid, None if valid else result.reason, result.trace
    )


def validate_all(settings: ExecutionRoutingSettings) -> tuple[ValidationResult, ...]:
    """Validate every class that has a chain, plus the default class always.

    The default class is checked even when unconfigured: a settings file with
    no chain for the class dispatch will actually use is the exact state
    ``routing validate`` exists to catch.
    """
    classes = [chain.route_class for chain in settings.chains]
    if settings.default_class not in classes:
        classes.append(settings.default_class)
    return tuple(
        validate_chain(settings, route_class)
        for route_class in sorted(classes, key=ROUTE_CLASSES.index)
    )
