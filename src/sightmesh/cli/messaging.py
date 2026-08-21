from __future__ import annotations

from .common import *
from .fleet import (
    _compact_text,
    _latest_process,
    _normalized_snapshot_with_retry,
    _resolve_session,
    _session_processes,
)
from .spawn import _primary_session_id

def cmd_message(args: argparse.Namespace) -> int:
    message = _read_text(args.message, args.message_file, "message")
    client = CdesktopClient(args.url)
    target = _resolve_session(client, args.session_id)
    succession.OwnershipStore().assert_deliverable(str(target["session_id"]))
    sender = args.sender_session or os.environ.get("CDESKTOP_SESSION_ID")
    order_id = f"order:{uuid.uuid4()}"
    if not getattr(args, "no_expect_ack", False):
        escalation.EscalationStore().expect_order(
            order_id=order_id,
            sender_session_id=sender,
            recipient_session_id=str(target["session_id"]),
            body=message,
        )
    result = client.send(str(target["session_id"]), message, sender, dedupe_key=order_id)
    _emit(result, args.json)
    return 0


def cmd_steer(args: argparse.Namespace) -> int:
    message = _read_text(args.message, args.message_file, "message")
    if not message.strip():
        raise ValueError("Steering message must not be empty")
    client = CdesktopClient(args.url)
    target = _resolve_session(client, args.session_id)
    caller_session = args.sender_session or os.environ.get("CDESKTOP_SESSION_ID")
    result = _steer_target(
        client,
        target,
        message,
        caller_session=caller_session,
    )
    _emit(result, args.json)
    return 0


def _steer_target(
    client: CdesktopClient,
    target: dict[str, Any],
    message: str,
    *,
    caller_session: str | None,
) -> dict[str, Any]:
    session_id = str(target["session_id"])
    succession.OwnershipStore().assert_deliverable(session_id)
    if caller_session == session_id:
        raise ValueError("An agent cannot steer itself")
    workspace_id = str(target["workspace_id"])
    workspace = client.workspace(workspace_id)
    if workspace.get("archived"):
        raise ValueError("Cannot steer a session in an archived workspace")

    processes = _session_processes(client, target)
    running = [
        process
        for process in processes
        if process.get("status") == "running"
        and process.get("run_reason") != "devserver"
    ]
    result = client.send(session_id, message, caller_session, intent="replace")
    return {
        "workspace_id": workspace_id,
        "session_id": session_id,
        "agent": f"@{target['selector']}",
        "interrupted_execution_processes": [str(process["id"]) for process in running],
        "scope": "selected session only",
        "follow_up": result,
    }


def cmd_parent(args: argparse.Namespace) -> int:
    child_session = args.session or os.environ.get("CDESKTOP_SESSION_ID")
    if not child_session:
        raise ValueError("Provide --session or run inside a cdesktop session")
    client = CdesktopClient(args.url)
    child = client.session(child_session)
    parent_session_id = child.get("parent_session_id")
    message: str | None = None
    if args.message or args.message_file:
        message = _read_text(args.message, args.message_file, "message")
        if not message.strip():
            raise ValueError("Parent message must not be empty")

    if not parent_session_id and message is None:
        raise ValueError(f"No recorded parent for session {child_session}")

    result: dict[str, Any] = {
        "parent": {
            "child_session_id": child_session,
            "child_workspace_id": child["workspace_id"],
            "parent_session_id": parent_session_id,
        }
    }
    if message is not None:
        result["delivery"] = escalation.escalate(
            client,
            child_session_id=child_session,
            child_workspace_id=str(child["workspace_id"]),
            parent_session_id=(
                str(parent_session_id) if parent_session_id else None
            ),
            message=message,
        )
        escalation.EscalationStore().satisfy_orders(child_session)
    else:
        target = _resolve_session(client, str(parent_session_id))
        result["parent"]["parent_workspace_id"] = target["workspace_id"]
    _emit(result, args.json)
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    recipient = args.session or os.environ.get("CDESKTOP_SESSION_ID")
    if not recipient:
        raise ValueError("Provide --session or run inside a cdesktop session")
    satisfied = escalation.EscalationStore().satisfy_orders(
        recipient, order_id=args.order_id
    )
    if not satisfied:
        raise ValueError(f"No outstanding order expectation: {args.order_id}")
    _emit({"order_id": args.order_id, "satisfied": satisfied}, args.json)
    return 0


def cmd_children(args: argparse.Namespace) -> int:
    parent_session = args.session or os.environ.get("CDESKTOP_SESSION_ID")
    if not parent_session:
        raise ValueError("Provide --session or run inside a cdesktop session")
    client = CdesktopClient(args.url)
    children = []
    for workspace in client.workspaces():
        for session in client.sessions(str(workspace["id"])):
            if str(session.get("parent_session_id") or "") == parent_session:
                children.append(
                    {
                        "child_session_id": session["id"],
                        "child_workspace_id": workspace["id"],
                        "parent_session_id": parent_session,
                    }
                )
    _emit(children, args.json)
    return 0


def cmd_escalations(args: argparse.Namespace) -> int:
    store = escalation.EscalationStore()
    rows = [record.to_dict() for record in store.pending(limit=args.limit)]
    if args.session:
        rows = [row for row in rows if row["child_session_id"] == args.session]
    _emit(rows, args.json)
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    """Edit an opt-in policy for any currently live, resolved session."""
    client = CdesktopClient(args.url)
    target = _resolve_session(client, args.session_id)
    session_id = str(target["session_id"])
    store = escalation.EscalationStore()
    if args.policy_action == "set":
        policy = store.set_signal_policy(
            session_id, escalation.parse_signal_conditions(args.signal_on)
        )
        result = policy.to_dict()
    elif args.policy_action == "clear":
        result = {"session_id": session_id, "cleared": store.clear_signal_policy(session_id)}
    else:
        result = store.signal_policy(session_id).to_dict()
    result["agent"] = f"@{target['selector']}"
    _emit(result, args.json)
    return 0


def cmd_prompt_idle(args: argparse.Namespace) -> int:
    message = _read_text(args.message, args.message_file, "message")
    client = CdesktopClient(args.url)
    target = _resolve_session(client, args.session_id)
    session_id = str(target["session_id"])
    succession.OwnershipStore().assert_deliverable(session_id)
    workspace_id = str(target["workspace_id"])
    processes = _session_processes(client, target)
    if any(
        process.get("status") == "running" and process.get("run_reason") != "devserver"
        for process in processes
    ):
        raise ValueError("Target agent is running; refusing idle-only prompt")
    process_ids = {str(process["id"]) for process in processes}
    if any(
        str(approval.get("execution_process_id")) in process_ids
        for approval in client.pending_approvals()
    ):
        raise ValueError(
            "Target agent has a pending approval; refusing idle-only prompt"
        )
    sender = args.sender_session or os.environ.get("CDESKTOP_SESSION_ID")
    result = client.send(session_id, message, sender)
    _emit(
        {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "agent": f"@{target['selector']}",
            "verified_idle": True,
            "follow_up": result,
        },
        args.json,
    )
    return 0


def _caller_session(explicit: str | None) -> str:
    caller = explicit or os.environ.get("CDESKTOP_SESSION_ID")
    if not caller:
        raise ValueError("Provide --caller or run inside a cdesktop session")
    return caller


def cmd_teammate_spawn(args: argparse.Namespace) -> int:
    prompt = _with_coordination_contract(
        _read_text(args.prompt, args.prompt_file, "prompt")
    )
    client = CdesktopClient(args.url)
    selection = _profile_selection(args, client)
    caller_session = _caller_session(args.caller)
    result = client.spawn_teammate(
        caller_session=caller_session,
        name=args.name,
        prompt=prompt,
        executor=selection.executor,
        permission_policy=args.permission,
        model=selection.model,
        reasoning=selection.reasoning,
        provider_id=selection.provider_id,
        auth_binding_id=selection.auth_binding_id,
    )
    if selection.profile and isinstance(result, dict):
        result["profile"] = selection.profile
    if selection.route_id and isinstance(result, dict):
        result["routing"] = {
            "route_id": selection.route_id,
            "auth_binding_id": selection.auth_binding_id,
        }
    child_session = _primary_session_id(result)
    if child_session:
        child = client.session(child_session)
        result["parent"] = {
            "child_session_id": child_session,
            "child_workspace_id": child["workspace_id"],
            "parent_session_id": child.get("parent_session_id"),
        }
    _emit(result, args.json)
    return 0


def cmd_teammate_list(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    caller = _caller_session(args.caller)
    session = client.request("GET", f"/sessions/{caller}")
    result = client.sessions(session["workspace_id"])
    _emit(result, args.json)
    return 0



def add_primary_parser(sub: argparse._SubParsersAction[Any]) -> None:
    parent = sub.add_parser(
        "parent", help="Show or immediately contact the session that spawned this agent"
    )
    parent.add_argument(
        "--session", help="Child session; defaults to CDESKTOP_SESSION_ID"
    )
    parent_message = parent.add_mutually_exclusive_group()
    parent_message.add_argument("--message")
    parent_message.add_argument("--message-file")
    parent.set_defaults(func=cmd_parent)

    children = sub.add_parser(
        "children", help="List visible workspaces launched directly by this session"
    )
    children.add_argument(
        "--session", help="Parent session; defaults to CDESKTOP_SESSION_ID"
    )
    children.set_defaults(func=cmd_children)

    escalations = sub.add_parser(
        "escalations",
        help="List parent escalations durably parked because no live parent could receive them",
    )
    escalations.add_argument("--session", help="Filter to one child session")
    escalations.add_argument("--limit", type=int, default=100)
    escalations.set_defaults(func=cmd_escalations)

    policy = sub.add_parser("policy", help="Show or edit a session's opt-in signal policy")
    policy_sub = policy.add_subparsers(dest="policy_action", required=True)
    policy_show = policy_sub.add_parser("show", help="Show one session's signal policy")
    policy_show.add_argument("session_id", help="Agent name from `sightmesh peers` or UUID")
    policy_show.set_defaults(func=cmd_policy)
    policy_set = policy_sub.add_parser("set", help="Replace one session's signal policy")
    policy_set.add_argument("session_id", help="Agent name from `sightmesh peers` or UUID")
    policy_set.add_argument("--signal-on", required=True)
    policy_set.set_defaults(func=cmd_policy)
    policy_clear = policy_sub.add_parser("clear", help="Clear one session's signal policy")
    policy_clear.add_argument("session_id", help="Agent name from `sightmesh peers` or UUID")
    policy_clear.set_defaults(func=cmd_policy)

    message = sub.add_parser(
        "message", help="Send a visible follow-up by agent name or session UUID"
    )
    message.add_argument("session_id", help="Agent name from `sightmesh peers` or UUID")
    message_group = message.add_mutually_exclusive_group(required=True)
    message_group.add_argument("--message")
    message_group.add_argument("--message-file")
    message.add_argument("--sender-session")
    message.add_argument(
        "--no-expect-ack", action="store_true", help="Do not require an outbound acknowledgment"
    )
    message.set_defaults(func=cmd_message)

    ack = sub.add_parser("ack", help="Explicitly acknowledge one ordered follow-up")
    ack.add_argument("order_id")
    ack.add_argument("--session", help="Recipient session; defaults to CDESKTOP_SESSION_ID")
    ack.set_defaults(func=cmd_ack)

    steer = sub.add_parser(
        "steer",
        help="Interrupt one selected agent and immediately resume it with a follow-up",
    )
    steer.add_argument("session_id", help="Agent name from `sightmesh peers` or UUID")
    steer_group = steer.add_mutually_exclusive_group(required=True)
    steer_group.add_argument("--message")
    steer_group.add_argument("--message-file")
    steer.add_argument("--sender-session")
    steer.set_defaults(func=cmd_steer)

    prompt_idle = sub.add_parser(
        "prompt-idle", help="Prompt a session only when cdesktop reports it idle"
    )
    prompt_idle.add_argument(
        "session_id", help="Agent name from `sightmesh peers` or UUID"
    )
    idle_group = prompt_idle.add_mutually_exclusive_group(required=True)
    idle_group.add_argument("--message")
    idle_group.add_argument("--message-file")
    prompt_idle.add_argument("--sender-session")
    prompt_idle.set_defaults(func=cmd_prompt_idle)




def add_teammate_parser(sub: argparse._SubParsersAction[Any]) -> None:
    teammate_spawn = sub.add_parser(
        "teammate-spawn", help="Launch a visible same-workspace teammate"
    )
    teammate_spawn.add_argument("--caller")
    teammate_spawn.add_argument("--name", required=True)
    teammate_prompt = teammate_spawn.add_mutually_exclusive_group(required=True)
    teammate_prompt.add_argument("--prompt")
    teammate_prompt.add_argument("--prompt-file")
    teammate_spawn.add_argument("--executor", choices=["CLAUDE_CODE", "CODEX"])
    teammate_spawn.add_argument("--profile", dest="profile_name")
    teammate_spawn.add_argument(
        "--permission",
        choices=["SUPERVISED", "PLAN", "ACCEPT_EDITS"],
    )
    teammate_spawn.add_argument("--model")
    teammate_spawn.add_argument(
        "--reasoning", choices=["low", "medium", "high", "xhigh", "max"]
    )
    teammate_spawn.add_argument("--provider")
    teammate_spawn.set_defaults(func=cmd_teammate_spawn)

    teammate_list = sub.add_parser(
        "teammate-list", help="List the caller's cdesktop team"
    )
    teammate_list.add_argument("--caller")
    teammate_list.set_defaults(func=cmd_teammate_list)
