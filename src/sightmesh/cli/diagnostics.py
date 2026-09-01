from __future__ import annotations

from ..sdk import SightMesh, SightMeshError
from .common import *
from .fleet import _fleet_sessions, _idle_unmet_orders, _latest_process, _normalized_snapshot_with_retry, _process_event_time, _session_processes

def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    failures = 0
    try:
        client = CdesktopClient(args.url)
        info = client.info()
        config = info["config"]
        local_ok = (
            config.get("analytics_enabled") is False
            and config.get("relay_enabled") is False
        )
        runtime_fork_ok = _is_sightmesh_cdesktop_version(
            info.get("version")
        ) or _active_runtime_matches_lock(info.get("version"))
        checks.append(
            {
                "check": "cdesktop-local-only",
                "ok": local_ok,
                "detail": {
                    "url": client.base_url,
                    "version": info.get("version"),
                    "analytics_enabled": config.get("analytics_enabled"),
                    "relay_enabled": config.get("relay_enabled"),
                },
            }
        )
        failures += int(not local_ok)
        checks.append(
            {
                "check": "cdesktop-runtime-sightmesh-fork",
                "ok": runtime_fork_ok,
                "detail": info.get("version") or "version unavailable",
            }
        )
        failures += int(not runtime_fork_ok)
        # An advertised capability with no executed probe is the failure mode
        # this check exists to surface, so the probe kind is always reported.
        try:
            probe = SightMesh(client=client)._require_contract()
            checks.append(
                {
                    "check": "managed-task-launch",
                    "ok": True,
                    "detail": {"probe": probe},
                }
            )
        except (CdesktopError, SightMeshError) as exc:
            checks.append(
                {"check": "managed-task-launch", "ok": False, "detail": str(exc)}
            )
            failures += 1
    except CdesktopError as exc:
        checks.append({"check": "cdesktop", "ok": False, "detail": str(exc)})
        failures += 1

    private, insecure = service.local_storage_is_private()
    checks.append(
        {
            "check": "local-storage-permissions",
            "ok": private,
            "detail": "private" if private else {"insecure_roots": insecure},
        }
    )
    failures += int(not private)

    for command in ("repowire", "codex", "claude", "cdesktop"):
        found = shutil.which(command)
        checks.append(
            {"check": f"command:{command}", "ok": bool(found), "detail": found}
        )
        failures += int(not bool(found) and command in {"repowire", "cdesktop"})

    cdesktop_command = shutil.which("cdesktop")
    if cdesktop_command:
        result = subprocess.run(
            [cdesktop_command, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        detail = (result.stdout or result.stderr).strip()
        fork_ok = result.returncode == 0 and _is_sightmesh_cdesktop_version(detail)
        checks.append(
            {
                "check": "cdesktop-sightmesh-fork",
                "ok": fork_ok,
                "detail": detail or "version unavailable",
            }
        )
        failures += int(not fork_ok)

    if shutil.which("repowire"):
        result = subprocess.run(
            ["repowire", "status"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        detail = (result.stdout or result.stderr).strip()
        repowire_ok = _repowire_status_ok(result.returncode, detail)
        checks.append(
            {
                "check": "repowire",
                "ok": repowire_ok,
                "detail": detail,
            }
        )
        failures += int(not repowire_ok)

    if shutil.which("claude"):
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        try:
            raw_auth = json.loads(result.stdout)
            auth_detail: Any = {
                "logged_in": raw_auth.get("loggedIn"),
                "auth_method": raw_auth.get("authMethod"),
                "provider": raw_auth.get("apiProvider"),
                "subscription_type": raw_auth.get("subscriptionType"),
            }
        except json.JSONDecodeError:
            auth_detail = "status unavailable" if result.returncode else "authenticated"
        checks.append(
            {
                "check": "claude-auth",
                "ok": result.returncode == 0,
                "warning_only": True,
                "detail": auth_detail,
            }
        )

    _emit(checks, args.json)
    return 1 if failures else 0


def cmd_list(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    summaries = {
        item["workspace_id"]: item
        for archived in (False, True)
        for item in client.workspace_summaries(archived)
    }
    rows: list[dict[str, Any]] = []
    for workspace in client.workspaces():
        sessions = client.sessions(workspace["id"])
        summary = summaries.get(workspace["id"], {})
        rows.append(
            {
                "workspace_id": workspace["id"],
                "name": workspace.get("name"),
                "branch": workspace.get("branch"),
                "archived": workspace.get("archived"),
                "use_worktree": workspace.get("use_worktree"),
                "latest_process_status": summary.get("latest_process_status"),
                "latest_process_completed_at": summary.get(
                    "latest_process_completed_at"
                ),
                "has_pending_approval": bool(summary.get("has_pending_approval")),
                "has_unseen_turns": bool(summary.get("has_unseen_turns")),
                "bridge_enabled": workspace["id"] in routing.enabled_workspaces(),
                "sessions": [
                    {
                        "id": session["id"],
                        "name": session.get("name"),
                        "executor": session.get("executor"),
                        "created_at": session.get("created_at"),
                    }
                    for session in sessions
                ],
            }
        )
    _emit(rows, args.json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    services = service.status(args.port)
    workspaces: list[dict[str, Any]] = []
    summaries = {
        item["workspace_id"]: item
        for archived in (False, True)
        for item in client.workspace_summaries(archived)
    }
    for workspace in client.workspaces():
        if workspace.get("archived") and not args.include_archived:
            continue
        summary = summaries.get(workspace["id"], {})
        workspaces.append(
            {
                "workspace_id": workspace["id"],
                "name": workspace.get("name"),
                "branch": workspace.get("branch"),
                "archived": bool(workspace.get("archived")),
                "worktree": bool(workspace.get("use_worktree")),
                "latest_process_status": summary.get("latest_process_status"),
                "latest_process_completed_at": summary.get(
                    "latest_process_completed_at"
                ),
                "has_pending_approval": bool(summary.get("has_pending_approval")),
                "has_unseen_turns": bool(summary.get("has_unseen_turns")),
                "bridge_enabled": workspace["id"] in routing.enabled_workspaces(),
                "sessions": [
                    {
                        "id": session["id"],
                        "executor": session.get("executor"),
                        "name": session.get("name"),
                    }
                    for session in client.sessions(workspace["id"])
                ],
            }
        )
    profile_rows: list[dict[str, Any]] = []
    providers = client.providers()
    for profile in ProfileStore().list():
        try:
            validate_provider(profile, providers)
            valid = True
            error = None
        except ProfileError as exc:
            valid = False
            error = str(exc)
        profile_rows.append({**profile.to_dict(), "valid": valid, "error": error})
    _emit(
        {
            "service": services,
            "workspaces": workspaces,
            "workspace_counts": {
                "active": sum(not item["archived"] for item in workspaces),
                "running": sum(
                    item["latest_process_status"] == "running" for item in workspaces
                ),
                "awaiting_approval": sum(
                    item["has_pending_approval"] for item in workspaces
                ),
            },
            "leases": [lease.to_public_dict() for lease in leases.LeaseStore().list()],
            "profiles": profile_rows,
            "providers": [provider_summary(provider) for provider in providers],
        },
        args.json,
    )
    return 0


def _overview_execution_facts(
    client: CdesktopClient,
    process: dict[str, Any],
    provider_kinds: dict[str, str],
) -> dict[str, Any]:
    action = process.get("executor_action")
    action = action if isinstance(action, dict) else {}
    action_type = action.get("typ")
    action_type = action_type if isinstance(action_type, dict) else {}
    config = action_type.get("executor_config", {})
    config = config if isinstance(config, dict) else {}
    model = action.get("selected_model_id") or config.get("model_id")
    provider_id = action.get("selected_provider_id")
    token_usage = None
    context = None
    try:
        snapshot = _normalized_snapshot_with_retry(client, str(process["id"]))
    except CdesktopError:
        snapshot = {}
    entries = snapshot.get("entries") if isinstance(snapshot, dict) else []
    for wrapped in entries if isinstance(entries, list) else []:
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        entry_type = content.get("entry_type") if isinstance(content, dict) else None
        if (
            not isinstance(entry_type, dict)
            or entry_type.get("type") != "token_usage_info"
        ):
            continue
        total = entry_type.get("total_tokens")
        limit = entry_type.get("model_context_window")
        if isinstance(total, (int, float)):
            token_usage = {
                "total": total,
                "unit": "tokens",
                "provenance": "cdesktop normalized snapshot",
            }
        if (
            isinstance(total, (int, float))
            and isinstance(limit, (int, float))
            and limit
        ):
            context = {
                "used": total,
                "limit": limit,
                "pressure": round(float(total) / float(limit), 3),
            }
    return {
        "model": str(model) if model else None,
        "provider": provider_kinds.get(str(provider_id)) if provider_id else None,
        "account_id": None,
        "token_usage": token_usage,
        "context": context,
    }


def _fleet_overview(
    client: CdesktopClient,
    viewed_at: datetime | None,
    *,
    now: datetime | None = None,
) -> fleet.FleetOverview:
    current_time = now or datetime.now(UTC)
    cutoff = viewed_at or current_time - timedelta(hours=DEFAULT_OVERVIEW_HOURS)
    workspaces = [
        {
            "id": str(workspace["id"]),
            "branch": workspace.get("branch"),
        }
        for workspace in client.workspaces()
        if not workspace.get("archived")
    ]
    executions: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    try:
        provider_kinds = {
            str(provider["id"]): str(provider.get("kind") or provider.get("name"))
            for provider in client.providers()
            if provider.get("id") and (provider.get("kind") or provider.get("name"))
        }
    except (AttributeError, CdesktopError):
        provider_kinds = {}
    for row in _fleet_sessions(client):
        process = _latest_process(_session_processes(client, row))
        if process is None:
            continue
        event_at = _process_event_time(process)
        status = str(process.get("status") or "")
        if status not in fleet.RUNNING and (event_at is None or event_at < cutoff):
            continue
        native_facts = _overview_execution_facts(client, process, provider_kinds)
        executions.append(
            {
                "id": str(process["id"]),
                "session_id": row["session_id"],
                "workspace_id": row["workspace_id"],
                "status": status,
                **native_facts,
                "branch": row.get("branch"),
                "last_event": {
                    "at": event_at,
                    "kind": "execution",
                    "status": status,
                },
            }
        )
        parent = row.get("parent_session_id")
        if parent:
            relationships.append(
                {"execution_id": str(process["id"]), "id": str(parent)}
            )
    try:
        pending = client.pending_approvals()
    except (AttributeError, CdesktopError):
        pending = []
    approvals_rows = [
        {
            "execution_id": str(item["execution_process_id"]),
            "status": "pending",
        }
        for item in pending
        if item.get("execution_process_id")
    ]
    facts = fleet.FleetFacts(
        workspaces=tuple(workspaces),
        executions=tuple(executions),
        approvals=tuple(approvals_rows),
        relationships=tuple(relationships),
    )
    return fleet.overview(facts, now=current_time, viewed_at=cutoff)


def cmd_overview(args: argparse.Namespace) -> int:
    viewed_at = datetime.fromisoformat(args.since) if args.since else None
    if viewed_at and viewed_at.tzinfo is None:
        viewed_at = viewed_at.replace(tzinfo=UTC)
    client = CdesktopClient(args.url)
    projection = _fleet_overview(client, viewed_at)
    orders = _idle_unmet_orders(client, _fleet_sessions(client))
    if args.json:
        _emit({**projection.to_dict(), "unmet_order_expectations": orders}, True)
        return 0
    groups = (
        ("Needs attention", projection.needs_attention),
        ("Running", projection.running),
        ("Done since view", projection.done_since_view),
    )
    for label, items in groups:
        print(label)
        if not items:
            print("  (none)")
        for item in items:
            print(f"  {item.selector} — {item.reason} Next: {item.next_action}")
    print("Unmet order expectations")
    if not orders:
        print("  (none)")
    for order in orders:
        print(f"  {order['agent'] or order['recipient_session_id']} — {order['body']}")
    return 0



def add_initial_parser(sub: argparse._SubParsersAction[Any]) -> None:
    doctor = sub.add_parser("doctor", help="Verify local orchestration dependencies")
    doctor.set_defaults(func=cmd_doctor)

    listing = sub.add_parser("list", help="List cdesktop workspaces and sessions")
    listing.set_defaults(func=cmd_list)




def add_status_parser(sub: argparse._SubParsersAction[Any]) -> None:
    fleet_status = sub.add_parser(
        "status", help="Show joined local fleet and reliability status"
    )
    fleet_status.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    fleet_status.add_argument("--include-archived", action="store_true")
    fleet_status.set_defaults(func=cmd_status)

    overview = sub.add_parser(
        "overview", help="Group privacy-safe fleet activity by required attention"
    )
    overview.add_argument(
        "--since",
        help="ISO-8601 lower bound for inactive items; defaults to the last 24 hours",
    )
    overview.set_defaults(func=cmd_overview)
