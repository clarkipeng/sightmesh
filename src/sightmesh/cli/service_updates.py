from __future__ import annotations

from .common import *

def cmd_service(args: argparse.Namespace) -> int:
    if args.action == "install":
        path = service.install(args.port, start_now=not args.no_start)
        result = {"installed": str(path), **service.status(args.port)}
    elif args.action == "start":
        service.start(args.port)
        result = service.status(args.port)
    elif args.action == "stop":
        service.stop()
        result = service.status(args.port)
    elif args.action == "status":
        result = service.status(args.port)
    elif args.action == "open":
        service.open_ui(args.port)
        result = service.status(args.port)
    elif args.action == "cutover":
        result = service.cutover(args.port)
    elif args.action == "uninstall":
        service.uninstall()
        result = service.status(args.port)
    else:
        raise ValueError(f"Unknown service action: {args.action}")
    _emit(result, args.json)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    if args.update_action == "stage":
        pinned = RUNTIME_LOCK.cdesktop
        package = args.package or pinned.package.url
        version = args.version or pinned.version
        sha256 = args.sha256
        if args.package is None:
            sha256 = sha256 or pinned.package.sha256
        elif sha256 is None and not args.local_development:
            raise ValueError(
                "Package overrides require --sha256 or explicit --local-development"
            )
        result = updates.stage(
            package,
            version,
            expected_sha256=sha256,
        )
    elif args.update_action == "activate":
        result = updates.activate_if_idle(
            CdesktopClient(args.url),
            port=args.port,
        )
    elif args.update_action == "status":
        result = updates.read_state()
        if result.get("pending") and service.is_healthy(args.port):
            result["activity"] = updates.activity(CdesktopClient(args.url))
    elif args.update_action == "cancel":
        result = updates.cancel()
    elif args.update_action == "prune":
        result = updates.prune(keep=args.keep, dry_run=args.dry_run)
    else:
        raise ValueError(f"Unknown update action: {args.update_action}")
    if not getattr(args, "quiet", False):
        _emit(result, args.json)
    return 0



def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    managed = sub.add_parser("service", help="Manage the local cdesktop service")
    managed.add_argument(
        "action",
        choices=["install", "start", "stop", "status", "open", "cutover", "uninstall"],
    )
    managed.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    managed.add_argument("--no-start", action="store_true")
    managed.set_defaults(func=cmd_service)

    update = sub.add_parser(
        "update", help="Stage and safely activate versioned cdesktop releases"
    )
    update_sub = update.add_subparsers(dest="update_action", required=True)
    update_stage = update_sub.add_parser(
        "stage",
        help="Verify and install a release without interrupting active workers",
    )
    update_stage.add_argument(
        "--package", help="Local tgz or HTTPS URL; defaults to the runtime lock"
    )
    update_stage.add_argument("--version", help="Defaults to the runtime lock")
    update_stage.add_argument(
        "--sha256", help="Required SHA-256 digest for remote packages"
    )
    update_stage.add_argument(
        "--local-development",
        action="store_true",
        help="Explicitly allow an unverified local package override",
    )
    update_stage.set_defaults(func=cmd_update)
    update_activate = update_sub.add_parser(
        "activate",
        help="Activate now, or return safely while workers are busy",
    )
    update_activate.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    update_activate.set_defaults(func=cmd_update)
    update_status = update_sub.add_parser("status", help="Show staged update state")
    update_status.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    update_status.set_defaults(func=cmd_update)
    update_cancel = update_sub.add_parser(
        "cancel", help="Cancel a staged update without removing its verified package"
    )
    update_cancel.set_defaults(func=cmd_update)
    update_prune = update_sub.add_parser(
        "prune", help="Remove superseded packages while retaining active staged paths"
    )
    update_prune.add_argument("--keep", type=int, default=1)
    update_prune.add_argument("--dry-run", action="store_true")
    update_prune.set_defaults(func=cmd_update)
