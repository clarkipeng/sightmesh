from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..cdesktop import CdesktopClient
from ..external_runs import ExternalRunReconciler, ExternalRunStore
from ..sdk import Command, SightMesh, WorkerSpec
from .common import _emit


def _mesh(args: argparse.Namespace) -> SightMesh:
    return SightMesh(url=args.url)


def _json_file(path: str) -> Any:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read batch file {path}: {exc}") from exc


def cmd_start(args: argparse.Namespace) -> int:
    if args.batch:
        if args.key or args.prompt or args.repo:
            raise ValueError("--batch cannot be combined with key, prompt, or --repo")
        payload = _json_file(args.batch)
        if not isinstance(payload, list):
            raise ValueError("Start batch must be a JSON array")
        try:
            specs = [WorkerSpec(**item) for item in payload]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid start batch: {exc}") from exc
        result = _mesh(args).start_all(specs)
        _emit(result.to_dict(), args.json)
        return 0 if result.ok else 1
    if not args.key or args.prompt is None or not args.repo:
        raise ValueError("start requires KEY PROMPT --repo REPO, or --batch FILE")
    worker = _mesh(args).start(
        WorkerSpec(
            key=args.key,
            prompt=args.prompt,
            repo=args.repo,
            base=args.base,
            profile=args.profile,
            executor=args.executor,
            model=args.model,
            reasoning=args.reasoning,
            permission=args.permission,
            children=args.children,
        )
    )
    _emit(worker.to_dict(), args.json)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    if args.batch:
        if args.worker or args.prompt:
            raise ValueError("--batch cannot be combined with worker or prompt")
        payload = _json_file(args.batch)
        if isinstance(payload, dict):
            commands: Any = {str(key): str(value) for key, value in payload.items()}
        elif isinstance(payload, list):
            try:
                commands = [Command(**item) for item in payload]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid command batch: {exc}") from exc
        else:
            raise ValueError("Command batch must be a JSON object or array")
        result = _mesh(args).send_all(commands)
        _emit(result.to_dict(), args.json)
        return 0 if result.ok else 1
    if not args.worker or args.prompt is None:
        raise ValueError("send requires WORKER PROMPT, or --batch FILE")
    _emit(_mesh(args).send(args.worker, args.prompt), args.json)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _emit(_mesh(args).show(args.worker).to_dict(), args.json)
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    _emit([worker.to_dict() for worker in _mesh(args).list()], args.json)
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    _emit(_mesh(args).replace(args.worker, args.prompt).to_dict(), args.json)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _emit(_mesh(args).cancel(args.worker).to_dict(), args.json)
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    _emit(_mesh(args).checkpoint(args.text, worker=args.worker).to_dict(), args.json)
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    _emit(_mesh(args).complete(args.summary, worker=args.worker).to_dict(), args.json)
    return 0


def cmd_blocked(args: argparse.Namespace) -> int:
    _emit(_mesh(args).blocked(args.reason, worker=args.worker).to_dict(), args.json)
    return 0


def cmd_run_subscribe(args: argparse.Namespace) -> int:
    result = ExternalRunStore().subscribe(
        run_id=args.run_id,
        output_root=args.output_root,
        return_session_id=args.return_session,
        return_workspace_id=args.return_workspace,
    )
    _emit(result.to_dict(), args.json)
    return 0


def cmd_run_bind(args: argparse.Namespace) -> int:
    result = ExternalRunStore().bind(
        args.subscription,
        writer_capability=args.writer_capability,
        pid=args.pid,
        process_fingerprint=args.process_fingerprint,
        expect_version=args.expect_version,
    )
    _emit(result.to_dict(), args.json)
    return 0


def cmd_run_show(args: argparse.Namespace) -> int:
    store = ExternalRunStore()
    value: object = (
        store.find(args.subscription).to_dict()
        if args.subscription
        else [run.to_dict() for run in store.pending()]
    )
    _emit(value, args.json)
    return 0


def cmd_run_reconcile(args: argparse.Namespace) -> int:
    reconciler = ExternalRunReconciler(CdesktopClient(args.url))
    count = (
        reconciler.reconcile_one(args.subscription)
        if args.subscription
        else reconciler.reconcile()
    )
    _emit({"reconciled": count}, args.json)
    return 0


def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    start = sub.add_parser("start", help="Idempotently start a managed worker")
    start.add_argument("key", nargs="?")
    start.add_argument("prompt", nargs="?")
    start.add_argument("--repo", help="Registered cdesktop repository name or path")
    start.add_argument("--base", default="main")
    start.add_argument("--profile")
    start.add_argument("--executor", choices=["CLAUDE_CODE", "CODEX", "OPENCODE"])
    start.add_argument("--model")
    start.add_argument("--reasoning", choices=["low", "medium", "high", "xhigh", "max"])
    start.add_argument(
        "--permission",
        choices=["BYPASS_PERMISSIONS", "ACCEPT_EDITS", "PLAN", "SUPERVISED"],
        default="BYPASS_PERMISSIONS",
    )
    start.add_argument("--children", type=int, default=0)
    start.add_argument("--batch", help="JSON array of worker specifications")
    start.set_defaults(func=cmd_start)

    send = sub.add_parser("send", help="Queue one or a batch of worker commands")
    send.add_argument("worker", nargs="?")
    send.add_argument("prompt", nargs="?")
    send.add_argument("--batch", help="JSON object or array of worker commands")
    send.set_defaults(func=cmd_send)

    show = sub.add_parser("show", help="Show a managed worker or the current worker")
    show.add_argument("worker", nargs="?")
    show.set_defaults(func=cmd_show)

    tasks = sub.add_parser("tasks", help="List managed workers in the current scope")
    tasks.set_defaults(func=cmd_tasks)

    replace = sub.add_parser(
        "replace", help="Replace a worker in its existing worktree"
    )
    replace.add_argument("worker")
    replace.add_argument("prompt", nargs="?")
    replace.set_defaults(func=cmd_replace)

    cancel = sub.add_parser("cancel", help="Stop and cancel a managed worker")
    cancel.add_argument("worker")
    cancel.set_defaults(func=cmd_cancel)

    checkpoint = sub.add_parser("checkpoint", help="Save a recovery checkpoint")
    checkpoint.add_argument("text")
    checkpoint.add_argument("--worker")
    checkpoint.set_defaults(func=cmd_checkpoint)

    complete = sub.add_parser("complete", help="Complete the current managed task")
    complete.add_argument("--summary")
    complete.add_argument("--worker")
    complete.set_defaults(func=cmd_complete)

    blocked = sub.add_parser("blocked", help="Block the current managed task")
    blocked.add_argument("reason")
    blocked.add_argument("--worker")
    blocked.set_defaults(func=cmd_blocked)

    run = sub.add_parser(
        "run", help="Manage durable wake subscriptions for external runs"
    )
    actions = run.add_subparsers(dest="run_action", required=True)
    subscribe = actions.add_parser(
        "subscribe", help="Lease an output root before provider activity"
    )
    subscribe.add_argument("--run-id", required=True)
    subscribe.add_argument("--output-root", required=True)
    subscribe.add_argument("--return-session", required=True)
    subscribe.add_argument("--return-workspace")
    subscribe.set_defaults(func=cmd_run_subscribe)
    bind = actions.add_parser("bind", help="Bind a runner-owned process fingerprint")
    bind.add_argument("subscription")
    bind.add_argument("--writer-capability", required=True)
    bind.add_argument("--pid", required=True, type=int)
    bind.add_argument("--process-fingerprint", required=True)
    bind.add_argument("--expect-version", required=True, type=int)
    bind.set_defaults(func=cmd_run_bind)
    show_run = actions.add_parser(
        "show", help="Show durable external-run subscriptions"
    )
    show_run.add_argument("subscription", nargs="?")
    show_run.set_defaults(func=cmd_run_show)
    reconcile = actions.add_parser(
        "reconcile", help="Run one external-run reconciliation pass"
    )
    reconcile.add_argument("subscription", nargs="?")
    reconcile.set_defaults(func=cmd_run_reconcile)
