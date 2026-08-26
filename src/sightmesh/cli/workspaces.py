from __future__ import annotations

from .common import *

def cmd_configure(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    config = client.configure_local(Path(args.workspace_root))
    secured = service.harden_local_storage()
    _emit(
        {
            "url": client.base_url,
            "analytics_enabled": config.get("analytics_enabled"),
            "relay_enabled": config.get("relay_enabled"),
            "workspace_dir": config.get("workspace_dir"),
            "secured_storage_roots": secured,
        },
        args.json,
    )
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    workspace = client.workspace(args.workspace_id)
    if args.archive:
        return _archive_workspace(args, client)

    message = _read_text(args.message, args.message_file, "message")
    sessions = sorted(
        client.sessions(args.workspace_id), key=lambda item: item["created_at"]
    )
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


def _archive_workspace(args: argparse.Namespace, client: CdesktopClient) -> int:
    if not args.confirm_reconciled:
        raise ValueError("Archiving requires --confirm-reconciled")
    workspace = client.workspace(args.workspace_id)
    if workspace.get("archived"):
        raise ValueError("Workspace is already archived")
    dirty = client.dirty_repositories(args.workspace_id)
    if dirty and workspace.get("use_worktree"):
        raise ValueError(
            "Refusing to archive a dirty managed worktree. cdesktop may remove archived "
            "worktrees after one hour, so commit, hand off, or otherwise reconcile these "
            f"files first. Dirty state: {json.dumps(dirty)}"
        )
    if dirty and not args.preserve_dirty:
        raise ValueError(
            "Refusing to archive a dirty direct workspace. Reconcile it or pass "
            f"--preserve-dirty explicitly. Dirty state: {json.dumps(dirty)}"
        )
    sessions = client.sessions(args.workspace_id)
    ownership = succession.OwnershipStore()
    retired_sessions = [
        ownership.retire(
            str(session["id"]), state=succession.SUPERSEDED, reason="archive"
        ).session_id
        for session in sessions
        if session.get("id")
    ]
    client.stop_workspace(args.workspace_id)
    archived = client.archive_workspace(args.workspace_id)
    routing.disable(args.workspace_id)
    released = leases.LeaseStore().release_workspace_if_present(args.workspace_id)
    _emit(
        {
            "workspace": archived,
            "action": "stopped-and-archived",
            "preserved_dirty": dirty,
            "retired_sessions": retired_sessions,
            "released_lease": released.to_public_dict() if released else None,
        },
        args.json,
    )
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    workspace = client.workspace(args.workspace_id)
    if args.workspace_action == "rename":
        name = args.name.strip()
        if not name:
            raise ValueError("Workspace name must not be empty")
        renamed = client.rename_workspace(args.workspace_id, name)
        _emit({"workspace": renamed, "action": "renamed"}, args.json)
        return 0
    if args.workspace_action == "archive":
        return _archive_workspace(args, client)
    if args.workspace_action == "restore":
        if not workspace.get("archived"):
            raise ValueError("Workspace is already active")
        lease_store = leases.LeaseStore()
        repos = client.workspace_repos(args.workspace_id)
        if not repos:
            raise ValueError("Archived workspace has no repository")
        for repo in repos:
            lease_store.assert_spawn_allowed(
                Path(str(repo["path"])),
                use_worktree=bool(workspace.get("use_worktree")),
            )
        restored = client.restore_workspace(args.workspace_id)
        try:
            synced = leases.sync_active_workspaces(
                client, ttl_seconds=args.lease_ttl_seconds
            )
        except Exception:
            client.archive_workspace(args.workspace_id)
            raise
        routing.enable(args.workspace_id)
        _emit(
            {
                "workspace": restored,
                "action": "restored",
                "leases": [
                    lease.to_public_dict()
                    for lease in synced
                    if lease.workspace_id == args.workspace_id
                ],
            },
            args.json,
        )
        return 0
    if args.workspace_action == "delete":
        if not args.confirm_delete:
            raise ValueError("Deleting an archive requires --confirm-delete")
        if not workspace.get("archived"):
            raise ValueError("Refusing to delete an active workspace; archive it first")
        dirty = client.dirty_repositories(args.workspace_id)
        missing = client.missing_repositories(args.workspace_id)
        if dirty and workspace.get("use_worktree"):
            raise ValueError(
                "Refusing to delete an archived managed worktree with dirty files. "
                f"Dirty state: {json.dumps(dirty)}"
            )
        if dirty and not args.preserve_dirty:
            raise ValueError(
                "Refusing to delete cdesktop history for a dirty direct workspace "
                "without --preserve-dirty. The repository itself will remain untouched. "
                f"Dirty state: {json.dumps(dirty)}"
            )
        result = client.delete_workspace(args.workspace_id)
        routing.disable(args.workspace_id)
        released = leases.LeaseStore().release_workspace_if_present(args.workspace_id)
        _emit(
            {
                "workspace_id": args.workspace_id,
                "action": "deleted",
                "branch_preserved": True,
                "missing_repositories": missing,
                "preserved_dirty": dirty,
                "cdesktop_result": result,
                "released_lease": released.to_public_dict() if released else None,
            },
            args.json,
        )
        return 0
    raise ValueError(f"Unknown workspace action: {args.workspace_action}")


def _lease_store(args: argparse.Namespace) -> leases.LeaseStore:
    return leases.LeaseStore(
        Path(args.lease_dir).expanduser() if args.lease_dir else None
    )


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
        _emit(
            [
                lease.to_public_dict()
                for lease in store.list(include_stale=args.include_stale)
            ],
            args.json,
        )
    elif args.lease_action == "release":
        _emit(
            store.release(
                args.token, owner=args.owner, workspace_id=args.workspace_id
            ).to_dict(),
            args.json,
        )
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
        _emit([lease.to_public_dict() for lease in store.recover_stale()], args.json)
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


def cmd_migrate(args: argparse.Namespace) -> int:
    if args.migrate_action == "plan":
        plan = conductor_migrate.build_plan(
            conductor_roots=args.conductor_root or None,
            database=args.database,
        )
        path = conductor_migrate.write_plan(plan, args.output)
        counts = {
            "total": len(plan["workspaces"]),
            "active": sum(
                item["state"] not in {"archived", "orphaned-checkout"}
                and item["kind"] != "orphaned-archive"
                for item in plan["workspaces"]
            ),
            "archived": sum(
                item["state"] in {"archived", "orphaned-checkout"}
                or item["kind"] == "orphaned-archive"
                for item in plan["workspaces"]
            ),
            "blocked": sum(bool(item["blockers"]) for item in plan["workspaces"]),
            "dirty": sum(
                bool(item.get("git", {}).get("dirty_paths"))
                for item in plan["workspaces"]
            ),
        }
        _emit({"plan_path": str(path), "run_id": plan["run_id"], **counts}, args.json)
    elif args.migrate_action == "apply":
        result = conductor_migrate.apply_plan(
            args.plan,
            names=args.workspace or (),
            include_archived=args.include_archived,
            materialize_archived=args.materialize_archived,
            include_dirty=args.include_dirty,
            confirm_conductor_paused=args.confirm_conductor_paused,
            confirm_checkpointed=args.confirm_checkpointed,
            semantic_limit=args.semantic_messages,
            client=CdesktopClient(args.url),
        )
        _emit(result, args.json)
    elif args.migrate_action == "status":
        _emit(conductor_migrate.migration_status(args.run), args.json)
    elif args.migrate_action == "rollback":
        result = conductor_migrate.rollback_run(
            args.run,
            confirm=args.confirm,
            client=CdesktopClient(args.url),
        )
        _emit(result, args.json)
    else:
        raise ValueError(f"Unknown migration action: {args.migrate_action}")
    return 0



def add_configure_parser(sub: argparse._SubParsersAction[Any]) -> None:
    configure = sub.add_parser("configure", help="Enforce local-only cdesktop settings")
    configure.add_argument(
        "--workspace-root",
        default=str(Path.home() / ".local" / "share" / "sightmesh"),
    )
    configure.set_defaults(func=cmd_configure)




def add_workspace_parser(sub: argparse._SubParsersAction[Any]) -> None:
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

    workspace = sub.add_parser(
        "workspace", help="Rename, archive, restore, or delete a cdesktop workspace"
    )
    workspace_sub = workspace.add_subparsers(dest="workspace_action", required=True)
    workspace_rename = workspace_sub.add_parser(
        "rename", help="Rename a cdesktop workspace without changing its branch"
    )
    workspace_rename.add_argument("workspace_id")
    workspace_rename.add_argument("name")
    workspace_rename.set_defaults(func=cmd_workspace)
    workspace_archive = workspace_sub.add_parser(
        "archive", help="Stop and archive a reconciled workspace"
    )
    workspace_archive.add_argument("workspace_id")
    workspace_archive.add_argument("--confirm-reconciled", action="store_true")
    workspace_archive.add_argument(
        "--preserve-dirty",
        action="store_true",
        help="Preserve reconciled dirty state only for a direct workspace",
    )
    workspace_archive.set_defaults(func=cmd_workspace)
    workspace_restore = workspace_sub.add_parser(
        "restore", help="Restore an archived workspace and its ownership lease"
    )
    workspace_restore.add_argument("workspace_id")
    workspace_restore.add_argument(
        "--lease-ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS
    )
    workspace_restore.set_defaults(func=cmd_workspace)
    workspace_delete = workspace_sub.add_parser(
        "delete",
        help="Delete an archive and owned worktree while preserving its branch",
    )
    workspace_delete.add_argument("workspace_id")
    workspace_delete.add_argument("--confirm-delete", action="store_true")
    workspace_delete.add_argument(
        "--preserve-dirty",
        action="store_true",
        help="Delete cdesktop history while leaving a dirty direct repository untouched",
    )
    workspace_delete.set_defaults(func=cmd_workspace)




def add_final_parser(sub: argparse._SubParsersAction[Any]) -> None:
    lease = sub.add_parser(
        "lease", help="Inspect and manage local workspace ownership leases"
    )
    lease.add_argument("--lease-dir", help="Override lease state directory")
    lease_sub = lease.add_subparsers(dest="lease_action", required=True)
    lease_acquire = lease_sub.add_parser(
        "acquire", help="Acquire a lease and return its capability token"
    )
    lease_acquire.add_argument("--owner", required=True)
    lease_acquire.add_argument("--repo", required=True)
    lease_acquire.add_argument("--worktree")
    lease_acquire.add_argument(
        "--ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS
    )
    lease_acquire.add_argument("--workspace-id")
    lease_acquire.add_argument("--session-id")
    lease_acquire.set_defaults(func=cmd_lease)
    lease_list = lease_sub.add_parser(
        "list", help="List leases without capability tokens"
    )
    lease_list.add_argument(
        "--include-stale", action=argparse.BooleanOptionalAction, default=True
    )
    lease_list.set_defaults(func=cmd_lease)
    lease_release = lease_sub.add_parser(
        "release", help="Release by capability token and return that capability"
    )
    lease_release.add_argument("token")
    lease_release.add_argument("--owner")
    lease_release.add_argument("--workspace-id")
    lease_release.set_defaults(func=cmd_lease)
    lease_renew = lease_sub.add_parser(
        "renew", help="Renew by capability token and return that capability"
    )
    lease_renew.add_argument("token")
    lease_renew.add_argument(
        "--ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS
    )
    lease_renew.add_argument("--owner")
    lease_renew.add_argument("--workspace-id")
    lease_renew.set_defaults(func=cmd_lease)
    lease_recover = lease_sub.add_parser(
        "recover-stale", help="Remove stale leases without returning capability tokens"
    )
    lease_recover.set_defaults(func=cmd_lease)

    migration = sub.add_parser(
        "migration-dry-run", help="Read-only Conductor to cdesktop migration inventory"
    )
    migration.add_argument("--conductor-root", action="append", default=[])
    migration.set_defaults(func=cmd_migration_dry_run)

    migrate = sub.add_parser(
        "migrate", help="Plan, apply, inspect, or roll back Conductor imports"
    )
    migrate_sub = migrate.add_subparsers(dest="migrate_action", required=True)
    migrate_plan = migrate_sub.add_parser(
        "plan", help="Write a private, resumable Conductor migration plan"
    )
    migrate_plan.add_argument("--conductor-root", action="append", default=[])
    migrate_plan.add_argument("--database", help="Read-only Conductor SQLite path")
    migrate_plan.add_argument("--output", help="Exact output plan.json path")
    migrate_plan.set_defaults(func=cmd_migrate)

    migrate_apply = migrate_sub.add_parser(
        "apply",
        help="Adopt selected Conductor sources into cdesktop without starting agents",
    )
    migrate_apply.add_argument("plan")
    selection = migrate_apply.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--workspace", action="append")
    migrate_apply.add_argument("--include-archived", action="store_true")
    migrate_apply.add_argument(
        "--materialize-archived",
        action="store_true",
        help="Create archived cdesktop rows instead of keeping archive handoffs catalog-only",
    )
    migrate_apply.add_argument("--include-dirty", action="store_true")
    migrate_apply.add_argument("--confirm-conductor-paused", action="store_true")
    migrate_apply.add_argument("--confirm-checkpointed", action="store_true")
    migrate_apply.add_argument("--semantic-messages", type=int, default=20)
    migrate_apply.set_defaults(func=cmd_migrate)

    migrate_status = migrate_sub.add_parser(
        "status", help="Inspect a resumable migration run"
    )
    migrate_status.add_argument("run", help="run.json or its sibling plan.json")
    migrate_status.set_defaults(func=cmd_migrate)

    migrate_rollback = migrate_sub.add_parser(
        "rollback", help="Archive empty workspaces created by a migration run"
    )
    migrate_rollback.add_argument("run", help="run.json or its sibling plan.json")
    migrate_rollback.add_argument("--confirm", action="store_true")
    migrate_rollback.set_defaults(func=cmd_migrate)
