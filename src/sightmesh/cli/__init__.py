from __future__ import annotations

import argparse
import importlib
import sys
import types

from .. import __version__

_COMMAND_MODULES = (
    "common",
    "diagnostics",
    "fleet",
    "spawn",
    "messaging",
    "approvals_commands",
    "workspaces",
    "service_updates",
    "bridge_commands",
    "routing_commands",
    "pool_commands",
    "tasks",
)
_COMMAND_MODULE_NAMES = frozenset(_COMMAND_MODULES)


def _command_modules() -> tuple[types.ModuleType, ...]:
    """Load command handlers only when a command needs the SDK runtime."""
    return tuple(importlib.import_module(f"{__name__}.{name}") for name in _COMMAND_MODULES)


def parser() -> argparse.ArgumentParser:
    (
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
        task_commands,
    ) = _command_modules()
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
    task_commands.add_parser(sub)
    workspace_commands.add_workspace_parser(sub)
    service_updates.add_parser(sub)
    bridge_commands.add_parser(sub)
    workspace_commands.add_final_parser(sub)
    return root


def __getattr__(name: str):
    module_name = name.removesuffix("_commands")
    if name in _COMMAND_MODULES:
        return importlib.import_module(f"{__name__}.{name}")
    if module_name in _COMMAND_MODULES:
        return importlib.import_module(f"{__name__}.{module_name}")
    for module in _command_modules():
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _CliModule(types.ModuleType):
    """Keep test and integration monkeypatches on the legacy facade working."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name in _COMMAND_MODULE_NAMES:
            return
        for module in _command_modules():
            if name in vars(module):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CliModule


def main() -> None:
    if sys.argv[1:] == ["--version"]:
        print(__version__)
        raise SystemExit(0)
    from ..cdesktop import CdesktopError
    from ..pool import PoolError
    from ..profiles import ProfileError
    from ..repowire import RepowireError
    from ..sdk import SightMeshError
    from ..task_store import TaskStoreError

    args = parser().parse_args()
    try:
        code = args.func(args)
    except (
        CdesktopError,
        PoolError,
        ProfileError,
        RepowireError,
        SightMeshError,
        TaskStoreError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)
