from __future__ import annotations

from .common import *
from ..run_subscriptions import RunReconciler, RunSubscriptionStore


def cmd_run_subscribe(args: argparse.Namespace) -> int:
    result = RunSubscriptionStore().subscribe(
        run_id=args.run_id,
        output_root=args.output_root,
        return_session_id=args.return_session,
        return_workspace_id=args.return_workspace,
    )
    _emit(result.to_dict(), args.json)
    return 0


def cmd_run_bind(args: argparse.Namespace) -> int:
    record = RunSubscriptionStore().bind(
        args.subscription,
        writer_capability=args.writer_capability,
        pid=args.pid,
        process_start=args.process_start,
    )
    _emit(record.to_public_dict(), args.json)
    return 0


def cmd_run_show(args: argparse.Namespace) -> int:
    store = RunSubscriptionStore()
    if args.subscription:
        result = store.find(args.subscription).to_public_dict()
    else:
        result = [record.to_public_dict() for record in store.all()]
    _emit(result, args.json)
    return 0


def cmd_run_reconcile(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    reconciler = RunReconciler(client)
    if args.subscription:
        result: object = reconciler.reconcile_one(args.subscription)
    else:
        result = reconciler.reconcile()
    _emit(result, args.json)
    return 0


def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    run = sub.add_parser(
        "run",
        help="Manage durable wake subscriptions for external runs",
    )
    run_sub = run.add_subparsers(dest="run_action", required=True)

    subscribe = run_sub.add_parser(
        "subscribe",
        help="Reserve an output root and create a durable wake subscription",
    )
    subscribe.add_argument("--run-id", required=True)
    subscribe.add_argument("--output-root", required=True)
    subscribe.add_argument("--return-session", required=True)
    subscribe.add_argument("--return-workspace")
    subscribe.set_defaults(func=cmd_run_subscribe)

    bind = run_sub.add_parser(
        "bind",
        help="Bind a launched process fingerprint to a run subscription",
    )
    bind.add_argument("subscription")
    bind.add_argument("--writer-capability", required=True)
    bind.add_argument("--pid", required=True, type=int)
    bind.add_argument("--process-start", required=True)
    bind.set_defaults(func=cmd_run_bind)

    show = run_sub.add_parser(
        "show",
        help="Show redacted durable run subscriptions",
    )
    show.add_argument("subscription", nargs="?")
    show.set_defaults(func=cmd_run_show)

    reconcile = run_sub.add_parser(
        "reconcile",
        help="Run one external-run wake reconciliation pass",
    )
    reconcile.add_argument("subscription", nargs="?")
    reconcile.set_defaults(func=cmd_run_reconcile)
