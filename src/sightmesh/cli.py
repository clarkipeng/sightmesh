from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import os
from pathlib import Path
from typing import Any

from . import leases
from .cdesktop import CdesktopClient, CdesktopError
from . import service
from .bridge import run_bridge
from .delivery import DeliveryStore, DeliveryStoreError, to_dict
from . import routing
from .repowire import RepowireError, reply as repowire_reply


def _read_text(value: str | None, path: str | None, label: str) -> str:
    if bool(value) == bool(path):
        raise ValueError(f"Provide exactly one of --{label} or --{label}-file")
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    return value or ""


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, indent=2))


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    failures = 0
    try:
        client = CdesktopClient(args.url)
        info = client.info()
        config = info["config"]
        local_ok = config.get("analytics_enabled") is False and config.get("relay_enabled") is False
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
    except CdesktopError as exc:
        checks.append({"check": "cdesktop", "ok": False, "detail": str(exc)})
        failures += 1

    for command in ("repowire", "codex", "claude", "cdesktop"):
        found = shutil.which(command)
        checks.append({"check": f"command:{command}", "ok": bool(found), "detail": found})
        failures += int(not bool(found) and command in {"repowire", "cdesktop"})

    if shutil.which("repowire"):
        result = subprocess.run(
            ["repowire", "status"], capture_output=True, text=True, timeout=20, check=False
        )
        checks.append(
            {
                "check": "repowire",
                "ok": result.returncode == 0,
                "detail": (result.stdout or result.stderr).strip(),
            }
        )
        failures += int(result.returncode != 0)

    if shutil.which("claude"):
        result = subprocess.run(
            ["claude", "auth", "status"], capture_output=True, text=True, timeout=20, check=False
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
    rows: list[dict[str, Any]] = []
    for workspace in client.workspaces():
        sessions = client.sessions(workspace["id"])
        rows.append(
            {
                "workspace_id": workspace["id"],
                "name": workspace.get("name"),
                "branch": workspace.get("branch"),
                "archived": workspace.get("archived"),
                "use_worktree": workspace.get("use_worktree"),
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


def cmd_configure(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    config = client.configure_local(Path(args.workspace_root))
    _emit(
        {
            "url": client.base_url,
            "analytics_enabled": config.get("analytics_enabled"),
            "relay_enabled": config.get("relay_enabled"),
            "workspace_dir": config.get("workspace_dir"),
        },
        args.json,
    )
    return 0


def _workspace_id(result: dict[str, Any]) -> str:
    workspace = result.get("workspace") if isinstance(result, dict) else None
    if isinstance(workspace, dict) and workspace.get("id"):
        return str(workspace["id"])
    if isinstance(result, dict) and result.get("workspace_id"):
        return str(result["workspace_id"])
    raise ValueError("cdesktop did not return a workspace id")


def _primary_session_id(result: dict[str, Any]) -> str | None:
    sessions = result.get("sessions") if isinstance(result, dict) else None
    if isinstance(sessions, list) and sessions and isinstance(sessions[0], dict):
        return str(sessions[0].get("id")) if sessions[0].get("id") else None
    session = result.get("session") if isinstance(result, dict) else None
    if isinstance(session, dict) and session.get("id"):
        return str(session["id"])
    return None


def _workspace_container(result: dict[str, Any], client: CdesktopClient, workspace_id: str) -> Path:
    workspace = result.get("workspace") if isinstance(result, dict) else None
    container = workspace.get("container_ref") if isinstance(workspace, dict) else None
    if not container:
        container = client.workspace(workspace_id).get("container_ref")
    if not container:
        raise ValueError("cdesktop did not return a worktree container path")
    return Path(str(container)).expanduser().resolve()


def _validate_base_branch(repo_path: Path, base: str) -> None:
    candidates = [f"refs/heads/{base}", f"refs/remotes/{base}"]
    for candidate in candidates:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", candidate],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
    raise ValueError(
        f"--base must name an existing local or remote branch, not a raw commit: {base}"
    )


def cmd_spawn(args: argparse.Namespace) -> int:
    prompt = _read_text(args.prompt, args.prompt_file, "prompt")
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    repo_path = Path(args.repo).expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"Repository path does not exist: {repo_path}")
    _validate_base_branch(repo_path, args.base)
    if args.unattended and not args.worktree:
        raise ValueError("--unattended requires --worktree")
    if args.unattended:
        if args.permission not in {None, "BYPASS_PERMISSIONS"}:
            raise ValueError("--unattended cannot be combined with a supervised permission policy")
        permission_policy = "BYPASS_PERMISSIONS"
    else:
        permission_policy = args.permission or "SUPERVISED"
        if permission_policy == "BYPASS_PERMISSIONS":
            raise ValueError("BYPASS_PERMISSIONS requires explicit --unattended")
    client = CdesktopClient(args.url)
    leases.sync_active_workspaces(client)
    lease_store = leases.LeaseStore()
    lease_owner = f"cdesktop-spawn:{args.name}"
    pending_lease: leases.Lease | None = None
    if args.worktree:
        lease_store.assert_spawn_allowed(repo_path, use_worktree=True)
    else:
        pending_lease = lease_store.acquire(
            lease_owner,
            repo_path,
            ttl_seconds=args.lease_ttl_seconds,
        )
    try:
        result = client.spawn_workspace(
            name=args.name,
            repo_path=repo_path,
            target_branch=args.base,
            executor=args.executor,
            prompt=prompt,
            use_worktree=args.worktree,
            permission_policy=permission_policy,
            model=args.model,
            reasoning=args.reasoning,
            provider_id=args.provider,
        )
    except Exception:
        if pending_lease:
            lease_store.release(pending_lease.token)
        raise
    workspace_id = _workspace_id(result)
    session_id = _primary_session_id(result)
    try:
        if args.worktree:
            container = _workspace_container(result, client, workspace_id)
            lease = lease_store.acquire(
                lease_owner,
                repo_path,
                container / repo_path.name,
                ttl_seconds=args.lease_ttl_seconds,
                workspace_id=workspace_id,
                session_id=session_id,
            )
        elif pending_lease:
            lease = lease_store.attach_workspace(pending_lease.token, workspace_id, session_id)
        else:
            lease = None
    except Exception:
        if args.worktree:
            client.stop_workspace(workspace_id)
        elif pending_lease:
            lease_store.release(pending_lease.token)
        raise
    if not args.no_bridge:
        routing.enable(workspace_id)
    if lease:
        result["lease"] = lease.to_dict()
    _emit(result, args.json)
    return 0


def cmd_message(args: argparse.Namespace) -> int:
    message = _read_text(args.message, args.message_file, "message")
    result = CdesktopClient(args.url).send(args.session_id, message, args.sender_session)
    _emit(result, args.json)
    return 0


def _caller_session(explicit: str | None) -> str:
    caller = explicit or os.environ.get("CDESKTOP_SESSION_ID")
    if not caller:
        raise ValueError("Provide --caller or run inside a cdesktop session")
    return caller


def cmd_teammate_spawn(args: argparse.Namespace) -> int:
    prompt = _read_text(args.prompt, args.prompt_file, "prompt")
    result = CdesktopClient(args.url).spawn_teammate(
        caller_session=_caller_session(args.caller),
        name=args.name,
        prompt=prompt,
        executor=args.executor,
        permission_policy=args.permission,
        model=args.model,
        reasoning=args.reasoning,
        provider_id=args.provider,
    )
    _emit(result, args.json)
    return 0


def cmd_teammate_list(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    caller = _caller_session(args.caller)
    session = client.request("GET", f"/sessions/{caller}")
    result = client.sessions(session["workspace_id"])
    _emit(result, args.json)
    return 0


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


def cmd_close(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    workspace = client.workspace(args.workspace_id)
    if args.archive:
        if not args.confirm_reconciled:
            raise ValueError("--archive requires --confirm-reconciled")
        dirty = client.dirty_repositories(args.workspace_id)
        if dirty and not args.preserve_dirty:
            raise ValueError(
                "Refusing to archive dirty repositories. Reconcile them or pass "
                f"--preserve-dirty explicitly. Dirty state: {json.dumps(dirty)}"
            )
        client.stop_workspace(args.workspace_id)
        archived = client.archive_workspace(args.workspace_id)
        routing.disable(args.workspace_id)
        released = leases.LeaseStore().release_workspace_if_present(args.workspace_id)
        released_lease = released.to_dict() if released else None
        _emit(
            {
                "workspace": archived,
                "action": "stopped-and-archived",
                "preserved_dirty": dirty,
                "released_lease": released_lease,
            },
            args.json,
        )
        return 0

    message = _read_text(args.message, args.message_file, "message")
    sessions = sorted(client.sessions(args.workspace_id), key=lambda item: item["created_at"])
    if not sessions:
        raise ValueError("Workspace has no session to receive a closeout request")
    result = client.send(sessions[0]["id"], message, args.sender_session)
    _emit(
        {
            "workspace_id": workspace["id"],
            "session_id": sessions[0]["id"],
            "follow_up": result,
            "action": "closeout-requested",
        },
        args.json,
    )
    return 0


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
    _emit(result or {"ok": True}, args.json)
    return 0


def cmd_delivery(args: argparse.Namespace) -> int:
    store = DeliveryStore()
    if args.delivery_action == "status":
        _emit(store.status(), args.json)
        return 0
    if args.delivery_action == "list":
        rows = [
            to_dict(record)
            for record in store.list(
                status=args.status,
                session_id=args.session_id,
                limit=args.limit,
            )
        ]
        _emit(rows, args.json)
        return 0
    if args.delivery_action == "retry":
        rows = [to_dict(store.retry(key)) for key in args.idempotency_key]
        _emit(rows, args.json)
        return 0
    if args.delivery_action == "purge":
        deleted = store.purge(args.idempotency_key)
        _emit({"deleted": deleted, "requested": args.idempotency_key}, args.json)
        return 0
    raise ValueError(f"Unknown delivery action: {args.delivery_action}")


def _lease_store(args: argparse.Namespace) -> leases.LeaseStore:
    return leases.LeaseStore(Path(args.lease_dir).expanduser() if args.lease_dir else None)


def cmd_lease(args: argparse.Namespace) -> int:
    store = _lease_store(args)
    if args.lease_action == "acquire":
        lease = store.acquire(
            args.owner,
            args.repo,
            args.worktree,
            args.ttl_seconds,
            workspace_id=args.workspace_id,
            session_id=args.session_id,
        )
        _emit(lease.to_dict(), args.json)
    elif args.lease_action == "list":
        _emit([lease.to_dict() for lease in store.list(include_stale=args.include_stale)], args.json)
    elif args.lease_action == "release":
        _emit(store.release(args.token, owner=args.owner, workspace_id=args.workspace_id).to_dict(), args.json)
    elif args.lease_action == "renew":
        _emit(
            store.renew(
                args.token,
                ttl_seconds=args.ttl_seconds,
                owner=args.owner,
                workspace_id=args.workspace_id,
            ).to_dict(),
            args.json,
        )
    elif args.lease_action == "recover-stale":
        _emit([lease.to_dict() for lease in store.recover_stale()], args.json)
    else:
        raise ValueError(f"Unknown lease action: {args.lease_action}")
    return 0


def cmd_migration_dry_run(args: argparse.Namespace) -> int:
    command = [sys.executable, "-m", "sightmesh.migration"]
    for root in args.conductor_root:
        command.extend(["--conductor-root", root])
    if args.json:
        command.append("--json")
    result = subprocess.run(command, check=False)
    return result.returncode


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sightmesh")
    root.add_argument("--url", help="Exact local cdesktop backend URL")
    root.add_argument("--json", action="store_true", help="Emit JSON")
    sub = root.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Verify local orchestration dependencies")
    doctor.set_defaults(func=cmd_doctor)

    listing = sub.add_parser("list", help="List cdesktop workspaces and sessions")
    listing.set_defaults(func=cmd_list)

    configure = sub.add_parser("configure", help="Enforce local-only cdesktop settings")
    configure.add_argument(
        "--workspace-root",
        default=str(Path.home() / ".local" / "share" / "sightmesh"),
    )
    configure.set_defaults(func=cmd_configure)

    spawn = sub.add_parser("spawn", help="Launch a full visible cdesktop workspace")
    spawn.add_argument("--name", required=True)
    spawn.add_argument("--repo", required=True)
    spawn.add_argument("--base", required=True, help="Existing local or remote Git branch")
    spawn.add_argument("--executor", choices=["CLAUDE_CODE", "CODEX"], required=True)
    prompt_group = spawn.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    topology = spawn.add_mutually_exclusive_group(required=True)
    topology.add_argument("--worktree", action="store_true")
    topology.add_argument("--direct", action="store_false", dest="worktree")
    spawn.add_argument(
        "--permission",
        choices=["SUPERVISED", "PLAN", "ACCEPT_EDITS", "BYPASS_PERMISSIONS"],
        default=None,
    )
    spawn.add_argument(
        "--unattended",
        action="store_true",
        help="Run a worktree-isolated worker without approval prompts",
    )
    spawn.add_argument("--model")
    spawn.add_argument("--reasoning", choices=["low", "medium", "high", "max"])
    spawn.add_argument("--provider", help="Configured cdesktop provider UUID")
    spawn.add_argument("--lease-ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS)
    spawn.add_argument("--no-bridge", action="store_true")
    spawn.set_defaults(func=cmd_spawn)

    message = sub.add_parser("message", help="Send a visible cdesktop follow-up")
    message.add_argument("session_id")
    message_group = message.add_mutually_exclusive_group(required=True)
    message_group.add_argument("--message")
    message_group.add_argument("--message-file")
    message.add_argument("--sender-session")
    message.set_defaults(func=cmd_message)

    teammate_spawn = sub.add_parser("teammate-spawn", help="Launch a visible same-workspace teammate")
    teammate_spawn.add_argument("--caller")
    teammate_spawn.add_argument("--name", required=True)
    teammate_prompt = teammate_spawn.add_mutually_exclusive_group(required=True)
    teammate_prompt.add_argument("--prompt")
    teammate_prompt.add_argument("--prompt-file")
    teammate_spawn.add_argument("--executor", choices=["CLAUDE_CODE", "CODEX"])
    teammate_spawn.add_argument(
        "--permission",
        choices=["SUPERVISED", "PLAN", "ACCEPT_EDITS"],
    )
    teammate_spawn.add_argument("--model")
    teammate_spawn.add_argument("--reasoning", choices=["low", "medium", "high", "max"])
    teammate_spawn.add_argument("--provider")
    teammate_spawn.set_defaults(func=cmd_teammate_spawn)

    teammate_list = sub.add_parser("teammate-list", help="List the caller's cdesktop team")
    teammate_list.add_argument("--caller")
    teammate_list.set_defaults(func=cmd_teammate_list)

    close = sub.add_parser("close", help="Request closeout or archive reconciled work")
    close.add_argument("workspace_id")
    close_group = close.add_mutually_exclusive_group()
    close_group.add_argument("--message")
    close_group.add_argument("--message-file")
    close.add_argument("--sender-session")
    close.add_argument("--archive", action="store_true")
    close.add_argument("--confirm-reconciled", action="store_true")
    close.add_argument(
        "--preserve-dirty",
        action="store_true",
        help="Archive while preserving explicitly reconciled dirty state",
    )
    close.set_defaults(func=cmd_close)

    managed = sub.add_parser("service", help="Manage the local cdesktop service")
    managed.add_argument(
        "action",
        choices=["install", "start", "stop", "status", "open", "cutover", "uninstall"],
    )
    managed.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    managed.add_argument("--no-start", action="store_true")
    managed.set_defaults(func=cmd_service)

    bridge = sub.add_parser("bridge", help="Bridge enabled cdesktop sessions into Repowire")
    bridge.add_argument("--repowire-url", default="ws://127.0.0.1:8377/ws")
    bridge.add_argument("--verbose", action="store_true")
    bridge.set_defaults(func=cmd_bridge)

    bridge_route = sub.add_parser("bridge-route", help="Enable or disable Repowire routing")
    bridge_route.add_argument("workspace_id")
    bridge_route.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    bridge_route.set_defaults(func=cmd_bridge_route)

    bridge_reply = sub.add_parser("bridge-reply", help="Reply to a bridged Repowire ask")
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

    delivery = sub.add_parser("delivery", help="Inspect and operate bridge delivery records")
    delivery_sub = delivery.add_subparsers(dest="delivery_action", required=True)

    delivery_status = delivery_sub.add_parser("status", help="Show delivery store status")
    delivery_status.set_defaults(func=cmd_delivery)

    delivery_list = delivery_sub.add_parser("list", help="List delivery records")
    delivery_list.add_argument("--status", choices=["pending", "inflight", "injected", "dead"])
    delivery_list.add_argument("--session-id")
    delivery_list.add_argument("--limit", type=int, default=50)
    delivery_list.set_defaults(func=cmd_delivery)

    delivery_retry = delivery_sub.add_parser("retry", help="Retry exact delivery keys")
    delivery_retry.add_argument("idempotency_key", nargs="+")
    delivery_retry.set_defaults(func=cmd_delivery)

    delivery_purge = delivery_sub.add_parser("purge", help="Purge exact delivery keys")
    delivery_purge.add_argument("idempotency_key", nargs="+")
    delivery_purge.set_defaults(func=cmd_delivery)

    lease = sub.add_parser("lease", help="Inspect and manage local workspace ownership leases")
    lease.add_argument("--lease-dir", help="Override lease state directory")
    lease_sub = lease.add_subparsers(dest="lease_action", required=True)
    lease_acquire = lease_sub.add_parser("acquire", help="Acquire an expiring ownership lease")
    lease_acquire.add_argument("--owner", required=True)
    lease_acquire.add_argument("--repo", required=True)
    lease_acquire.add_argument("--worktree")
    lease_acquire.add_argument("--ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS)
    lease_acquire.add_argument("--workspace-id")
    lease_acquire.add_argument("--session-id")
    lease_acquire.set_defaults(func=cmd_lease)
    lease_list = lease_sub.add_parser("list", help="List local ownership leases")
    lease_list.add_argument("--include-stale", action=argparse.BooleanOptionalAction, default=True)
    lease_list.set_defaults(func=cmd_lease)
    lease_release = lease_sub.add_parser("release", help="Release an ownership lease by token")
    lease_release.add_argument("token")
    lease_release.add_argument("--owner")
    lease_release.add_argument("--workspace-id")
    lease_release.set_defaults(func=cmd_lease)
    lease_renew = lease_sub.add_parser("renew", help="Renew an ownership lease by token")
    lease_renew.add_argument("token")
    lease_renew.add_argument("--ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS)
    lease_renew.add_argument("--owner")
    lease_renew.add_argument("--workspace-id")
    lease_renew.set_defaults(func=cmd_lease)
    lease_recover = lease_sub.add_parser("recover-stale", help="Remove expired or dead-owner leases")
    lease_recover.set_defaults(func=cmd_lease)

    migration = sub.add_parser(
        "migration-dry-run", help="Read-only Conductor to cdesktop migration inventory"
    )
    migration.add_argument("--conductor-root", action="append", default=[])
    migration.set_defaults(func=cmd_migration_dry_run)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        code = args.func(args)
    except (
        CdesktopError,
        DeliveryStoreError,
        leases.LeaseError,
        RepowireError,
        OSError,
        ValueError,
    ) as exc:
        print(f"sightmesh: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
