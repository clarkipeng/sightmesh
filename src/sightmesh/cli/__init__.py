from __future__ import annotations

import argparse
import sys
import types

from .. import __version__
from . import common as common_commands
from .common import *
from . import diagnostics as diagnostics_commands
from . import fleet as fleet_commands
from . import spawn as spawn_commands
from . import messaging as messaging_commands
from . import approvals_commands
from . import workspaces as workspace_commands
from . import service_updates
from . import bridge_commands
from . import routing_commands
from . import pool_commands
from . import run_commands
from .diagnostics import *
from .fleet import *
from .spawn import *
from .messaging import *
from .approvals_commands import *
from .workspaces import *
from .service_updates import *
from .bridge_commands import *
from .routing_commands import *
from .pool_commands import *
from .run_commands import *
from ..run_subscriptions import RunSubscriptionError
from ..escalation import EscalationStoreError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sightmesh")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--url", help="Exact local cdesktop backend URL")
    root.add_argument("--json", action="store_true", help="Emit JSON")
    sub = root.add_subparsers(dest="command", required=True)
    diagnostics_commands.add_initial_parser(sub)
    fleet_commands.add_parser(sub)
    approvals_commands.add_inbox_parser(sub)
    diagnostics_commands.add_status_parser(sub)
    workspace_commands.add_configure_parser(sub)
    spawn_commands.add_spawn_parser(sub)
    messaging_commands.add_primary_parser(sub)
    spawn_commands.add_failover_parser(sub)
    messaging_commands.add_teammate_parser(sub)
    approvals_commands.add_parser(sub)
    routing_commands.add_parser(sub)
    pool_commands.add_parser(sub)
    run_commands.add_parser(sub)
    workspace_commands.add_workspace_parser(sub)
    service_updates.add_parser(sub)
    bridge_commands.add_parser(sub)
    workspace_commands.add_final_parser(sub)
    return root


def __getattr__(name: str):
    for module in (
        common_commands,
        diagnostics_commands,
        fleet_commands,
        spawn_commands,
        messaging_commands,
        approvals_commands,
        workspace_commands,
        service_updates,
        bridge_commands,
        routing_commands,
        pool_commands,
        run_commands,
    ):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _CliModule(types.ModuleType):
    """Keep test and integration monkeypatches on the legacy facade working."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module in (
            common_commands,
            diagnostics_commands,
            fleet_commands,
            spawn_commands,
            messaging_commands,
            approvals_commands,
            workspace_commands,
            service_updates,
            bridge_commands,
            routing_commands,
            pool_commands,
            run_commands,
        ):
            if name in vars(module):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CliModule


def main() -> None:
    args = parser().parse_args()
    try:
        code = args.func(args)
    except (
        CdesktopError,
        PoolError,
        ProfileError,
        RepowireError,
        EscalationStoreError,
        RunSubscriptionError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)
