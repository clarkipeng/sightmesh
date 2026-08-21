from __future__ import annotations

from .common import *

def _fleet_sessions(
    client: CdesktopClient, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    archive_states = (False, True) if include_archived else (False,)
    summaries = {
        item["workspace_id"]: item
        for archived in archive_states
        for item in client.workspace_summaries(archived)
    }
    rows: list[dict[str, Any]] = []
    for workspace in client.workspaces():
        if workspace.get("archived") and not include_archived:
            continue
        sessions = sorted(
            client.sessions(workspace["id"]),
            key=lambda item: (str(item.get("created_at") or ""), str(item["id"])),
        )
        summary = summaries.get(workspace["id"], {})
        for index, session in enumerate(sessions):
            workspace_name = str(workspace.get("name") or workspace["id"])
            session_name = str(session.get("name") or "")
            rows.append(
                {
                    "workspace_id": str(workspace["id"]),
                    "workspace": workspace_name,
                    "session_id": str(session["id"]),
                    "session": session_name
                    or ("lead" if index == 0 else f"peer-{index}"),
                    "session_name": session_name or None,
                    "parent_session_id": session.get("parent_session_id"),
                    "is_lead": index == 0,
                    "executor": session.get("executor"),
                    "branch": workspace.get("branch"),
                    "archived": bool(workspace.get("archived")),
                    "workspace_status": summary.get("latest_process_status"),
                    "pending_approval": bool(summary.get("has_pending_approval")),
                    "unseen": bool(summary.get("has_unseen_turns")),
                }
            )

    base_aliases = [
        str(row["session_name"] or row["workspace"])
        if row["is_lead"] or row["session_name"]
        else f"{row['workspace']}/{row['session']}"
        for row in rows
    ]
    for row, base_alias in zip(rows, base_aliases, strict=True):
        duplicate = (
            sum(
                candidate.casefold() == base_alias.casefold()
                for candidate in base_aliases
            )
            > 1
        )
        row["selector"] = (
            f"{row['workspace']}/{row['session']}" if duplicate else base_alias
        )
    return rows


def _resolve_session(
    client: CdesktopClient, selector: str, *, include_archived: bool = False
) -> dict[str, Any]:
    needle = selector.removeprefix("@").casefold()
    rows = _fleet_sessions(client, include_archived=include_archived)
    matches = []
    for row in rows:
        names = {
            str(row["session_id"]).casefold(),
            str(row["selector"]).casefold(),
            f"{row['workspace']}/{row['session']}".casefold(),
        }
        if row["session_name"]:
            names.add(str(row["session_name"]).casefold())
        if row["is_lead"]:
            names.update(
                {
                    str(row["workspace"]).casefold(),
                    str(row["workspace_id"]).casefold(),
                }
            )
        if needle in names:
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        available = ", ".join(f"@{row['selector']}" for row in rows)
        raise ValueError(f"Unknown agent {selector!r}. Available: {available}")
    candidates = ", ".join(f"@{row['selector']}" for row in matches)
    raise ValueError(f"Ambiguous agent {selector!r}. Use one of: {candidates}")


def _session_processes(
    client: CdesktopClient, row: dict[str, Any]
) -> list[dict[str, Any]]:
    return client.execution_processes(str(row["session_id"]))


def _process_event_time(process: dict[str, Any]) -> datetime | None:
    raw = (
        process.get("completed_at")
        or process.get("updated_at")
        or process.get("started_at")
        or process.get("created_at")
    )
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _latest_process(processes: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [
        process
        for process in processes
        if not process.get("dropped") and process.get("run_reason") != "devserver"
    ]
    return (
        max(
            eligible,
            key=lambda process: (
                _process_event_time(process) or datetime.min.replace(tzinfo=UTC),
                str(process.get("id") or ""),
            ),
        )
        if eligible
        else None
    )


def _idle_unmet_orders(
    client: CdesktopClient, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Project durable, unmet orders only when their recipient is idle."""
    active_ids = set()
    for row in rows:
        try:
            processes = _session_processes(client, row)
        except CdesktopError:
            continue
        if any(
            process.get("status") == "running"
            and process.get("run_reason") != "devserver"
            for process in processes
        ):
            active_ids.add(str(row["session_id"]))
    selectors = {str(row["session_id"]): f"@{row['selector']}" for row in rows}
    return [
        {
            "type": "unmet_order_expectation",
            "order_id": order.order_id,
            "sender_session_id": order.sender_session_id,
            "recipient_session_id": order.recipient_session_id,
            "agent": selectors.get(order.recipient_session_id),
            "body": order.body,
            "body_digest": order.body_digest,
            "created_at": order.created_at,
            "next_action": "Review the order, then use prompt-idle or contact the agent.",
        }
        for order in escalation.EscalationStore().orders(unmet_only=True)
        if order.recipient_session_id in selectors
        and order.recipient_session_id not in active_ids
    ]


def cmd_peers(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    rows = _fleet_sessions(client, include_archived=args.include_archived)
    for row in rows:
        try:
            latest = _latest_process(_session_processes(client, row))
        except CdesktopError:
            latest = None
        row["status"] = (
            latest.get("status") if latest else row.pop("workspace_status", None)
        )
        row["execution_process_id"] = latest.get("id") if latest else None
    counts: dict[str, int] = {}
    for order in _idle_unmet_orders(client, rows):
        session_id = str(order["recipient_session_id"])
        counts[session_id] = counts.get(session_id, 0) + 1
    for row in rows:
        row["unmet_order_expectations"] = counts.get(str(row["session_id"]), 0)
    if args.json:
        _emit(rows, True)
    else:
        print("AGENT\tSTATUS\tEXECUTOR\tBRANCH")
        for row in rows:
            print(
                f"@{row['selector']}\t{row.get('status') or 'unknown'}\t"
                f"{row.get('executor') or '-'}\t{row.get('branch') or '-'}"
                f"{' [ORDER ACK]' if row['unmet_order_expectations'] else ''}"
            )
    return 0


def _compact_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


def _normalized_snapshot_with_retry(
    client: CdesktopClient, execution_process_id: str, attempts: int = 3
) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for _ in range(attempts):
        candidate = client.normalized_snapshot(execution_process_id)
        if candidate.get("complete"):
            return candidate
        if int(candidate.get("patch_count") or 0) >= int(best.get("patch_count") or 0):
            best = candidate
    return best


def _workspace_repository_paths(
    client: CdesktopClient, workspace_id: str
) -> list[dict[str, str]]:
    workspace = client.workspace(workspace_id)
    container = workspace.get("container_ref")
    use_worktree = bool(workspace.get("use_worktree"))
    paths = []
    for repo in client.workspace_repos(workspace_id):
        source = str(repo.get("path") or "")
        name = str(repo.get("name") or "")
        checkout = (
            str(Path(str(container)) / name) if use_worktree and container else source
        )
        paths.append({"name": name, "source": source, "checkout": checkout})
    return paths


def cmd_peek(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    row = _resolve_session(client, args.agent, include_archived=args.include_archived)
    latest = _latest_process(_session_processes(client, row))
    if latest is None:
        raise ValueError(f"Agent @{row['selector']} has no execution history")
    snapshot = _normalized_snapshot_with_retry(client, str(latest["id"]))
    entries = snapshot.get("entries") if isinstance(snapshot, dict) else []
    assistants: list[str] = []
    tools: list[dict[str, Any]] = []
    token_usage: dict[str, Any] | None = None
    for wrapped in entries if isinstance(entries, list) else []:
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        if not isinstance(content, dict):
            continue
        entry_type = content.get("entry_type")
        if not isinstance(entry_type, dict):
            continue
        kind = entry_type.get("type")
        if kind == "assistant_message":
            assistants.append(_compact_text(content.get("content"), args.max_chars))
        elif kind == "tool_use":
            tools.append(
                {
                    "tool": entry_type.get("tool_name"),
                    "status": entry_type.get("status"),
                    "summary": _compact_text(content.get("content"), 240),
                }
            )
        elif kind == "token_usage_info":
            token_usage = {
                "used": entry_type.get("total_tokens"),
                "window": entry_type.get("model_context_window"),
            }
    result = {
        "agent": f"@{row['selector']}",
        "session_id": row["session_id"],
        "workspace": row["workspace"],
        "branch": row["branch"],
        "repositories": _workspace_repository_paths(client, str(row["workspace_id"])),
        "status": latest.get("status"),
        "execution_process_id": latest.get("id"),
        "last_assistant": assistants[-1] if assistants else None,
        "recent_tools": tools[-args.tools :],
        "token_usage": token_usage,
        "context_pressure": (
            round(float(token_usage["used"]) / float(token_usage["window"]), 3)
            if token_usage
            and isinstance(token_usage.get("used"), (int, float))
            and isinstance(token_usage.get("window"), (int, float))
            and token_usage["window"]
            else None
        ),
        "coalesced_patch_count": snapshot.get("patch_count"),
        "complete": snapshot.get("complete"),
    }
    _emit(result, args.json)
    return 0



def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    peers = sub.add_parser(
        "peers", help="List every visible agent with compact steerable names"
    )
    peers.add_argument("--include-archived", action="store_true")
    peers.set_defaults(func=cmd_peers)

    peek = sub.add_parser(
        "peek", help="Show a coalesced activity snapshot for one visible agent"
    )
    peek.add_argument("agent", help="Agent name from `sightmesh peers` or session UUID")
    peek.add_argument("--include-archived", action="store_true")
    peek.add_argument("--tools", type=int, default=3)
    peek.add_argument("--max-chars", type=int, default=600)
    peek.set_defaults(func=cmd_peek)
