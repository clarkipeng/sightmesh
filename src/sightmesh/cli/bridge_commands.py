from __future__ import annotations

from .common import *

def cmd_bridge(args: argparse.Namespace) -> int:
    logging_level = "DEBUG" if args.verbose else "INFO"
    import logging

    logging.basicConfig(level=getattr(logging, logging_level))
    try:
        asyncio.run(run_bridge(args.url, args.repowire_url))
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_bridge_route(args: argparse.Namespace) -> int:
    if args.enabled:
        routing.enable(args.workspace_id)
    else:
        routing.disable(args.workspace_id)
    _emit(
        {
            "workspace_id": args.workspace_id,
            "bridge_enabled": args.workspace_id in routing.enabled_workspaces(),
        },
        args.json,
    )
    return 0


def cmd_bridge_reply(args: argparse.Namespace) -> int:
    message = _read_text(args.message, args.message_file, "message")
    result = repowire_reply(
        args.correlation_id,
        message,
        from_peer=args.from_peer,
        question=args.question,
        base_url=args.repowire_http_url,
    )
    reporter = os.environ.get("CDESKTOP_SESSION_ID")
    if reporter:
        escalation.EscalationStore().satisfy_orders(reporter)
    _emit(result or {"ok": True}, args.json)
    return 0



def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    bridge = sub.add_parser(
        "bridge", help="Bridge enabled cdesktop sessions into Repowire"
    )
    bridge.add_argument("--repowire-url", default="ws://127.0.0.1:8377/ws")
    bridge.add_argument("--verbose", action="store_true")
    bridge.set_defaults(func=cmd_bridge)

    bridge_route = sub.add_parser(
        "bridge-route", help="Enable or disable Repowire routing"
    )
    bridge_route.add_argument("workspace_id")
    bridge_route.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, default=True
    )
    bridge_route.set_defaults(func=cmd_bridge_route)

    bridge_reply = sub.add_parser(
        "bridge-reply", help="Reply to a bridged Repowire ask"
    )
    bridge_reply.add_argument("correlation_id")
    bridge_reply.add_argument("--from-peer", required=True)
    reply_group = bridge_reply.add_mutually_exclusive_group(required=True)
    reply_group.add_argument("--message")
    reply_group.add_argument("--message-file")
    bridge_reply.add_argument("--question", action="store_true")
    bridge_reply.add_argument(
        "--repowire-http-url",
        default="http://127.0.0.1:8377",
    )
    bridge_reply.set_defaults(func=cmd_bridge_reply)
