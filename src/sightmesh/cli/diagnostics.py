from __future__ import annotations

from ..sdk import SightMesh, SightMeshError
from .common import *
from .fleet import _fleet_sessions, _idle_unmet_orders, _latest_process, _normalized_snapshot_with_retry, _process_event_time, _session_processes

def _version_token(detail: object) -> str | None:
    match = re.search(r"\b\d+\.\d+\.\d+[\w.+-]*", str(detail or ""))
    return match.group(0) if match else None


def _version_skew_check(
    running_version: object, installed_cli_detail: object
) -> dict[str, Any]:
    """Compare installed CLI, running service, and staged/active versions.

    Skew is invisible until something breaks, which is how three cdesktop
    releases accumulated on one host. Reporting all three versions in one
    failing check makes it a first-class `doctor` outcome instead.
    """
    try:
        state = updates.read_state()
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "check": "cdesktop-version-skew",
            "ok": False,
            "detail": {"error": f"update state is unreadable: {exc}"},
        }
    active = _release_version(state.get("active"))
    staged = _release_version(state.get("pending"))
    running = _version_token(running_version)
    installed_cli = _version_token(installed_cli_detail)
    try:
        stale = updates.prune(dry_run=True)["removed"]
    except (OSError, RuntimeError, ValueError) as exc:
        stale = [f"unreadable: {exc}"]
    observed = {value for value in (active, running, installed_cli) if value}
    ok = len(observed) <= 1 and not stale
    return {
        "check": "cdesktop-version-skew",
        "ok": ok,
        "detail": {
            "installed_cli": installed_cli,
            "running_service": running,
            "active": active,
            "staged": staged,
            "stale_releases": stale,
        },
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    failures = 0
    running_version: object = None
    installed_cli_detail: object = None
    try:
        client = CdesktopClient(args.url)
        info = client.info()
        running_version = info.get("version")
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
        installed_cli_detail = detail
        fork_ok = result.returncode == 0 and _is_sightmesh_cdesktop_version(detail)
        checks.append(
            {
                "check": "cdesktop-sightmesh-fork",
                "ok": fork_ok,
                "detail": detail or "version unavailable",
            }
        )
        failures += int(not fork_ok)

    skew = _version_skew_check(running_version, installed_cli_detail)
    checks.append(skew)
    failures += int(not skew["ok"])

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


def _task_limit(args: argparse.Namespace) -> int | None:
    """Newest-N bound for one task read; ``--all`` is the explicit opt-out."""
    return None if args.all else int(args.limit)


def _bounded_tasks(
    store: Any, args: argparse.Namespace
) -> tuple[list[observability.TaskView], bool]:
    """Newest-N managed tasks, plus whether the bound actually hid anything.

    Reading one row past the bound is what makes the notice exact: printing
    "showing the newest N" whenever N rows come back cries wolf on a host
    that happens to hold exactly N tasks.
    """
    limit = _task_limit(args)
    if limit is None:
        return observability.read_tasks(store, limit=None), False
    views = observability.read_tasks(store, limit=limit + 1)
    if len(views) > limit:
        return views[-limit:], True
    return views, False


def _note_truncation(args: argparse.Namespace, truncated: bool) -> None:
    """Say so on stderr, so a bounded read is never mistaken for the whole."""
    if truncated:
        print(
            f"Showing the newest {_task_limit(args)} managed tasks; "
            "pass --all for every task.",
            file=sys.stderr,
        )


def cmd_list(args: argparse.Namespace) -> int:
    """List managed tasks from the kernel store, never the native fleet.

    Native workspace inventory is `sightmesh workspaces`; keeping the two
    surfaces apart is what stops a routine `list` from fanning out.
    """
    store = observability.task_store()
    views, truncated = _bounded_tasks(store, args)
    _emit([view.to_dict() for view in views], args.json)
    _note_truncation(args, truncated)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = observability.task_store()
    views, truncated = _bounded_tasks(store, args)
    _emit(
        {
            "service": service.status(args.port),
            "update": _update_summary(),
            "tasks": [view.to_dict() for view in views],
            "task_counts": observability.task_counts(views),
            "leases": [lease.to_public_dict() for lease in leases.LeaseStore().list()],
            "profiles": [profile.to_dict() for profile in ProfileStore().list()],
        },
        args.json,
    )
    _note_truncation(args, truncated)
    return 0


def _update_summary() -> dict[str, Any]:
    try:
        state = updates.read_state()
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "unreadable", "error": str(exc)}
    return {
        "status": state.get("status"),
        "active_version": _release_version(state.get("active")),
        "staged_version": _release_version(state.get("pending")),
    }


def _release_version(release: Any) -> str | None:
    return str(release.get("version")) if isinstance(release, dict) else None


def cmd_attention(args: argparse.Namespace) -> int:
    """One queue of everything a human must act on, answered by the kernel."""
    store = observability.task_store()
    views, truncated = _bounded_tasks(store, args)
    facts = observability.attention_facts(
        store, tasks=[view.to_dict() for view in views]
    )
    queue = fleet.attention(facts, now=datetime.now(UTC))
    if args.json:
        _emit(queue.to_dict(), True)
        _note_truncation(args, truncated)
        return 0
    if not queue.items:
        print("(nothing needs attention)")
    for item in queue.items:
        print(f"  {item.selector} - {item.reason} Next: {item.next_action}")
    for entry in queue.degraded:
        print(f"  degraded: {entry['source']} - {entry['reason']}")
    _note_truncation(args, truncated)
    return 0


def cmd_workspaces(args: argparse.Namespace) -> int:
    """Native workspace inventory. This surface fans out on purpose."""
    client = CdesktopClient(args.url)
    if args.overview:
        return _emit_native_overview(args, client)
    summaries = {
        item["workspace_id"]: item
        for archived in (False, True)
        for item in client.workspace_summaries(archived)
    }
    rows: list[dict[str, Any]] = []
    for workspace in client.workspaces():
        if workspace.get("archived") and not args.include_archived:
            continue
        summary = summaries.get(workspace["id"], {})
        rows.append(
            {
                "workspace_id": workspace["id"],
                "name": workspace.get("name"),
                "branch": workspace.get("branch"),
                "archived": bool(workspace.get("archived")),
                "use_worktree": bool(workspace.get("use_worktree")),
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
                    for session in client.sessions(workspace["id"])
                ],
            }
        )
    providers = client.providers()
    profile_rows: list[dict[str, Any]] = []
    for profile in ProfileStore().list():
        try:
            validate_provider(profile, providers)
            valid, error = True, None
        except ProfileError as exc:
            valid, error = False, str(exc)
        profile_rows.append({**profile.to_dict(), "valid": valid, "error": error})
    _emit(
        {
            "workspaces": rows,
            "workspace_counts": {
                "active": sum(not row["archived"] for row in rows),
                "running": sum(
                    row["latest_process_status"] == "running" for row in rows
                ),
                "awaiting_approval": sum(row["has_pending_approval"] for row in rows),
            },
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


def _viewed_at(args: argparse.Namespace) -> datetime | None:
    viewed_at = datetime.fromisoformat(args.since) if args.since else None
    if viewed_at and viewed_at.tzinfo is None:
        viewed_at = viewed_at.replace(tzinfo=UTC)
    return viewed_at


def _emit_native_overview(args: argparse.Namespace, client: CdesktopClient) -> int:
    projection = _fleet_overview(client, _viewed_at(args))
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
            print(f"  {item.selector} - {item.reason} Next: {item.next_action}")
    print("Unmet order expectations")
    if not orders:
        print("  (none)")
    for order in orders:
        print(f"  {order['agent'] or order['recipient_session_id']} - {order['body']}")
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    """Group managed tasks by required attention, reading only the kernel."""
    now = datetime.now(UTC)
    viewed_at = _viewed_at(args) or now - timedelta(hours=DEFAULT_OVERVIEW_HOURS)
    store = observability.task_store()
    views, truncated = _bounded_tasks(store, args)
    facts = observability.attention_facts(
        store, tasks=[view.to_dict() for view in views]
    )
    queue = fleet.attention(facts, now=now)
    groups = fleet.task_groups(facts.tasks, now=now, viewed_at=viewed_at)
    if args.json:
        _emit({**groups.to_dict(), "attention": queue.to_dict()}, True)
        _note_truncation(args, truncated)
        return 0
    print("Needs attention")
    if not queue.items:
        print("  (none)")
    for item in queue.items:
        print(f"  {item.selector} - {item.reason} Next: {item.next_action}")
    for label, rows in (
        ("Running", groups.running),
        ("Done since view", groups.done_since_view),
    ):
        print(label)
        if not rows:
            print("  (none)")
        for row in rows:
            print(f"  task/{row.get('scope')}/{row.get('key')} - {row.get('state')}")
    for entry in queue.degraded:
        print(f"  degraded: {entry['source']} - {entry['reason']}")
    _note_truncation(args, truncated)
    return 0



def _add_task_bounds(command: argparse.ArgumentParser) -> None:
    """Bound every task read the same way, with one explicit opt-out."""
    bound = command.add_mutually_exclusive_group()
    bound.add_argument(
        "--limit",
        type=int,
        default=observability.DEFAULT_TASK_LIMIT,
        help="Show only the newest N managed tasks",
    )
    bound.add_argument(
        "--all",
        action="store_true",
        help="Show every managed task, however many there are",
    )


def add_initial_parser(sub: argparse._SubParsersAction[Any]) -> None:
    doctor = sub.add_parser("doctor", help="Verify local orchestration dependencies")
    doctor.set_defaults(func=cmd_doctor)

    listing = sub.add_parser("list", help="List managed tasks from the kernel store")
    _add_task_bounds(listing)
    listing.set_defaults(func=cmd_list)




def add_status_parser(sub: argparse._SubParsersAction[Any]) -> None:
    fleet_status = sub.add_parser(
        "status", help="Show managed task, service, and lease state"
    )
    fleet_status.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    _add_task_bounds(fleet_status)
    fleet_status.set_defaults(func=cmd_status)

    attention = sub.add_parser(
        "attention", help="List everything that needs a human decision"
    )
    _add_task_bounds(attention)
    attention.set_defaults(func=cmd_attention)

    overview = sub.add_parser(
        "overview", help="Group managed tasks by required attention"
    )
    overview.add_argument(
        "--since",
        help="ISO-8601 lower bound for inactive items; defaults to the last 24 hours",
    )
    _add_task_bounds(overview)
    overview.set_defaults(func=cmd_overview)

    workspaces = sub.add_parser(
        "workspaces",
        help="Native cdesktop workspace inventory (fans out to the executor)",
    )
    workspaces.add_argument("--include-archived", action="store_true")
    workspaces.add_argument(
        "--overview",
        action="store_true",
        help="Group native execution processes by required attention",
    )
    workspaces.add_argument(
        "--since",
        help="ISO-8601 lower bound for inactive items in --overview",
    )
    workspaces.set_defaults(func=cmd_workspaces)
