from __future__ import annotations

from .common import *
from .spawn import _validate_reasoning

def cmd_profile(args: argparse.Namespace) -> int:
    store = ProfileStore()
    if args.profile_action == "list":
        _emit([profile.to_dict() for profile in store.list()], args.json)
        return 0
    if args.profile_action == "providers":
        providers = [
            provider_summary(item) for item in CdesktopClient(args.url).providers()
        ]
        _emit(providers, args.json)
        return 0
    if args.profile_action == "set":
        _validate_reasoning(args.executor, args.reasoning)
        profile = Profile(
            name=args.name,
            executor=args.executor,
            provider_id=args.provider,
            credential_kind=args.credential_kind,
            model=args.model,
            reasoning=args.reasoning,
            automatic_failover=args.automatic_failover,
        )
        validate_provider(profile, CdesktopClient(args.url).providers())
        _emit(store.set(profile).to_dict(), args.json)
        return 0
    if args.profile_action == "remove":
        _emit(store.remove(args.name).to_dict(), args.json)
        return 0
    raise ValueError(f"Unknown profile action: {args.profile_action}")


def cmd_routing(args: argparse.Namespace) -> int:
    store = execution_routing.ExecutionRoutingStore()
    action = args.routing_action

    if action == "show":
        _emit(store.load().to_dict(), args.json)
        return 0

    if action == "validate":
        settings = store.load()
        results = (
            (execution_routing.validate_chain(settings, args.route_class),)
            if args.route_class
            else execution_routing.validate_all(settings)
        )
        _emit(
            {
                "valid": all(result.valid for result in results),
                "classes": [result.to_dict() for result in results],
                "warnings": execution_routing.route_warnings(settings),
            },
            args.json,
        )
        return 0

    if action == "set-metered":
        updated = store.save(dataclasses.replace(store.load(), metered_fallback=args.value))
        _emit(updated.to_dict(), args.json)
        return 0

    if action == "set-free-fallback":
        updated = store.save(
            dataclasses.replace(
                store.load(), fallback_on_free_failure=args.value == "on"
            )
        )
        _emit(updated.to_dict(), args.json)
        return 0

    if action == "routes":
        return _cmd_routing_routes(args, store)

    if action == "explain":
        settings = store.load()
        route_class = args.route_class or settings.default_class
        result = execution_routing.select_route(
            settings, route_class=route_class, preferred_model=args.model
        )
        payload = {
            **result.to_dict(),
            "routeClass": route_class,
            "chain": [
                route.to_dict() for route in settings.routes_for(route_class)
            ],
            "workspace_id": args.workspace,
        }
        _emit(payload, args.json)
        return 0

    raise ValueError(f"Unknown routing action: {action}")


def _cmd_routing_routes(
    args: argparse.Namespace, store: execution_routing.ExecutionRoutingStore
) -> int:
    settings = store.load()
    route_class = args.route_class or settings.default_class
    routes = list(settings.routes_for(route_class))
    action = args.routes_action

    if action == "list":
        _emit([route.to_dict() for route in routes], args.json)
        return 0

    if action == "add":
        if any(existing.id == args.id for existing in routes):
            raise execution_routing.ExecutionRoutingError(
                f"Route already exists in class {route_class}: {args.id}"
            )
        route = execution_routing.Route(
            id=args.id,
            executor=args.executor,
            model=args.model,
            billing_class=args.billing_class,
            account_pool=args.account_pool,
            account=args.account,
        )
        routes.append(route)
        if args.before:
            routes = [r for r in routes if r.id != route.id]
            index = next((i for i, r in enumerate(routes) if r.id == args.before), None)
            if index is None:
                raise execution_routing.ExecutionRoutingError(
                    f"Unknown route in class {route_class}: {args.before}"
                )
            routes = [*routes[:index], route, *routes[index:]]
        return _save_chain(store, settings, route_class, routes, args, ids_only=False)

    if action == "remove":
        if not any(r.id == args.id for r in routes):
            raise execution_routing.ExecutionRoutingError(
                f"Unknown route in class {route_class}: {args.id}"
            )
        routes = [r for r in routes if r.id != args.id]
        return _save_chain(store, settings, route_class, routes, args, ids_only=False)

    if action == "order":
        current_ids = [r.id for r in routes]
        if sorted(args.ids) != sorted(current_ids):
            raise execution_routing.ExecutionRoutingError(
                f"must list every route in class {route_class} exactly once "
                f"(have: {' '.join(current_ids)})"
            )
        by_id = {r.id: r for r in routes}
        routes = [by_id[i] for i in args.ids]
        return _save_chain(store, settings, route_class, routes, args, ids_only=True)

    raise ValueError(f"Unknown routes action: {args.routes_action}")


def _save_chain(
    store: execution_routing.ExecutionRoutingStore,
    settings: execution_routing.ExecutionRoutingSettings,
    route_class: str,
    routes: list[execution_routing.Route],
    args: argparse.Namespace,
    *,
    ids_only: bool,
) -> int:
    """Replace one class chain, leaving every other class exactly as it was."""
    chain = execution_routing.RouteChain(route_class, tuple(routes))
    others = tuple(c for c in settings.chains if c.route_class != route_class)
    updated = store.save(
        dataclasses.replace(settings, chains=(*others, chain))
    )
    saved = updated.routes_for(route_class)
    _emit(
        [r.id for r in saved] if ids_only else [r.to_dict() for r in saved],
        args.json,
    )
    return 0



def _add_class_argument(parser: argparse.ArgumentParser) -> None:
    """``--class`` names which chain a command reads or edits.

    Omitted, it means the configured default class, so every existing
    invocation keeps addressing the chain it always addressed.
    """
    parser.add_argument(
        "--class",
        dest="route_class",
        choices=execution_routing.ROUTE_CLASSES,
        help="Route class to act on (default: the configured default class)",
    )


def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    profile = sub.add_parser(
        "profile", help="Manage safe named mappings to configured cdesktop providers"
    )
    profile_sub = profile.add_subparsers(dest="profile_action", required=True)
    profile_list = profile_sub.add_parser("list", help="List SightMesh profiles")
    profile_list.set_defaults(func=cmd_profile)
    profile_providers = profile_sub.add_parser(
        "providers", help="List redacted cdesktop provider metadata"
    )
    profile_providers.set_defaults(func=cmd_profile)
    profile_set = profile_sub.add_parser("set", help="Create or update a named profile")
    profile_set.add_argument("name")
    profile_set.add_argument(
        "--executor", choices=["CLAUDE_CODE", "CODEX", "OPENCODE"], required=True
    )
    profile_set.add_argument(
        "--provider", required=True, help="Configured cdesktop provider UUID"
    )
    profile_set.add_argument(
        "--credential-kind", choices=["ambient", "api", "enterprise"], default="ambient"
    )
    profile_set.add_argument("--model")
    profile_set.add_argument(
        "--reasoning", choices=["low", "medium", "high", "xhigh", "max"]
    )
    profile_set.add_argument(
        "--automatic-failover",
        action="store_true",
        help=(
            "Let a task launched on this profile fail over automatically onto "
            "its route class chain on a typed provider outcome"
        ),
    )
    profile_set.set_defaults(func=cmd_profile)
    profile_remove = profile_sub.add_parser("remove", help="Remove a named profile")
    profile_remove.add_argument("name")
    profile_remove.set_defaults(func=cmd_profile)

    routing_group = sub.add_parser(
        "routing", help="Subscription-first execution routing policy"
    )
    routing_sub = routing_group.add_subparsers(dest="routing_action", required=True)

    routing_show = routing_sub.add_parser("show", help="Show execution routing settings")
    routing_show.set_defaults(func=cmd_routing)

    routing_validate = routing_sub.add_parser(
        "validate",
        help="Prove every route class still has a usable path before dispatch",
    )
    _add_class_argument(routing_validate)
    routing_validate.set_defaults(func=cmd_routing)

    routing_set_metered = routing_sub.add_parser(
        "set-metered", help="Set the metered fallback policy"
    )
    routing_set_metered.add_argument("value", choices=sorted(execution_routing.METERED_FALLBACK_VALUES))
    routing_set_metered.set_defaults(func=cmd_routing)

    routing_set_free_fallback = routing_sub.add_parser(
        "set-free-fallback",
        help=(
            "Allow a failed free route to fall back onto a route that bills "
            "(off by default; the failure is escalated either way)"
        ),
    )
    routing_set_free_fallback.add_argument("value", choices=["on", "off"])
    routing_set_free_fallback.set_defaults(func=cmd_routing)

    routing_routes = routing_sub.add_parser(
        "routes", help="Manage one route class's ordered chain"
    )
    routing_routes_sub = routing_routes.add_subparsers(
        dest="routes_action", required=True
    )

    routing_routes_list = routing_routes_sub.add_parser(
        "list", help="List a class chain's routes in order"
    )
    _add_class_argument(routing_routes_list)
    routing_routes_list.set_defaults(func=cmd_routing)

    routing_routes_add = routing_routes_sub.add_parser("add", help="Add a route")
    routing_routes_add.add_argument("--id", required=True)
    routing_routes_add.add_argument(
        "--executor", required=True, choices=sorted(execution_routing.EXECUTORS)
    )
    routing_routes_add.add_argument("--model", required=True)
    routing_routes_add.add_argument(
        "--billing-class", required=True, choices=sorted(execution_routing.BILLING_CLASSES)
    )
    routing_routes_add.add_argument(
        "--account-pool", choices=pool_core.PROVIDERS, help="Ordered pool for a subscription route"
    )
    routing_routes_add.add_argument(
        "--account", help="Fixed pool account id for a metered route"
    )
    routing_routes_add.add_argument("--before", help="Insert before this route id")
    _add_class_argument(routing_routes_add)
    routing_routes_add.set_defaults(func=cmd_routing)

    routing_routes_remove = routing_routes_sub.add_parser("remove", help="Remove a route")
    routing_routes_remove.add_argument("id")
    _add_class_argument(routing_routes_remove)
    routing_routes_remove.set_defaults(func=cmd_routing)

    routing_routes_order = routing_routes_sub.add_parser(
        "order", help="Reorder every route in one class chain"
    )
    routing_routes_order.add_argument("ids", nargs="+")
    _add_class_argument(routing_routes_order)
    routing_routes_order.set_defaults(func=cmd_routing)

    routing_explain = routing_sub.add_parser(
        "explain", help="Safe selection trace for the current settings and pool"
    )
    routing_explain.add_argument("--workspace", help="Workspace id, echoed for traceability")
    routing_explain.add_argument("--model", help="Preferred model override")
    _add_class_argument(routing_explain)
    routing_explain.set_defaults(func=cmd_routing)
