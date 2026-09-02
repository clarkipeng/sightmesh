"""Route-chain builders shared by the routing, CLI, and succession suites.

Settings are class-scoped, so almost every test needs the same one-liner: put
these routes in a class chain. Keeping it here means a later change to the
chain model touches one helper rather than every construction site.
"""

from __future__ import annotations

from sightmesh.execution_routing import (
    DEFAULT_ROUTE_CLASS,
    Route,
    RouteChain,
)


def _chains(
    *routes: Route, route_class: str = DEFAULT_ROUTE_CLASS
) -> tuple[RouteChain, ...]:
    """One chain holding these routes, in order, for one class."""
    return (RouteChain(route_class, tuple(routes)),)


def chains(*chain_specs: tuple[str, tuple[Route, ...]]) -> tuple[RouteChain, ...]:
    """Several class chains at once, for scenarios that span both classes."""
    return tuple(
        RouteChain(route_class, tuple(routes)) for route_class, routes in chain_specs
    )
