from __future__ import annotations

from .common import *
from .fleet import _fleet_sessions, _resolve_session


@dataclasses.dataclass(frozen=True)
class LaunchSelection:
    executor: str
    provider_id: str | None
    model: str | None
    reasoning: str | None
    profile: str | None
    route_id: str | None = None
    auth_binding_id: str | None = None
    billing_class: str | None = None


def _routed_selection(args: argparse.Namespace) -> LaunchSelection:
    """Resolve executor and model through the routing selector.

    Only the opaque route id and pool binding id leave here; credential
    resolution stays inside the executor launcher.
    """
    settings = execution_routing.ExecutionRoutingStore().load()
    result = execution_routing.select_route(
        settings, preferred_model=getattr(args, "model", None)
    )
    if result.status == "approval_needed":
        raise ValueError(
            "Execution routing reached a metered route that requires approval; "
            "pass --executor or --profile to launch explicitly"
        )
    if result.status != "resolved" or result.target is None:
        detail = "; ".join(result.trace)
        raise ValueError(
            f"Execution routing could not resolve a route ({result.reason}); "
            f"pass --executor or --profile. Trace: {detail}"
        )
    target = result.target
    reasoning = getattr(args, "reasoning", None)
    _validate_reasoning(target.executor, reasoning)
    return LaunchSelection(
        executor=target.executor,
        provider_id=getattr(args, "provider", None),
        model=target.model,
        reasoning=reasoning,
        profile=None,
        route_id=target.route_id,
        auth_binding_id=target.auth_binding_id,
        billing_class=target.billing_class,
    )


def _report_free_route_failure(
    client: CdesktopClient,
    args: argparse.Namespace,
    selection: LaunchSelection,
    error: BaseException,
) -> None:
    """Make a refused free-route launch visible before re-raising.

    A subscription route that fails leaves a binding to cool and a session to
    reconcile. A free route owns neither, and a launch cdesktop refused leaves
    no worker behind to notice either - so this escalation is the only signal
    the failure ever produces. Deliberately not wrapped in a handler: if the
    durable record cannot be written, that surfaces alongside the spawn error
    rather than quietly replacing it.
    """
    if selection.billing_class != "free" or not selection.route_id:
        return
    succession.escalate_free_route_failure(
        client,
        execution_routing.ExecutionRoutingStore().load(),
        route_id=selection.route_id,
        # No session exists to name; this namespace cannot collide with one.
        child_session_id=f"pending-spawn:{args.name}",
        parent_session_id=os.environ.get(escalation.CDESKTOP_SESSION_ENV),
        output=str(error),
    )


def _profile_selection(
    args: argparse.Namespace, client: CdesktopClient
) -> LaunchSelection:
    profile_name = getattr(args, "profile_name", None)
    if not profile_name:
        executor = getattr(args, "executor", None)
        if not executor:
            return _routed_selection(args)
        selection = LaunchSelection(
            executor,
            getattr(args, "provider", None),
            getattr(args, "model", None),
            getattr(args, "reasoning", None),
            None,
        )
        _validate_reasoning(selection.executor, selection.reasoning)
        return selection

    profile = ProfileStore().get(profile_name)
    validate_provider(profile, client.providers())
    executor_override = getattr(args, "executor", None)
    provider_override = getattr(args, "provider", None)
    if executor_override and executor_override != profile.executor:
        raise ValueError("--executor cannot override a profile's executor")
    if provider_override and provider_override != profile.provider_id:
        raise ValueError("--provider cannot override a profile's provider")
    selection = LaunchSelection(
        profile.executor,
        profile.provider_id,
        getattr(args, "model", None) or profile.model,
        getattr(args, "reasoning", None) or profile.reasoning,
        profile.name,
    )
    _validate_reasoning(selection.executor, selection.reasoning)
    return selection


def _validate_reasoning(executor: str, reasoning: str | None) -> None:
    if reasoning is None:
        return
    allowed = {"low", "medium", "high", "xhigh", "max"}
    if reasoning not in allowed:
        raise ValueError(
            f"Reasoning {reasoning!r} is unsupported by {executor}; "
            f"choose one of {', '.join(sorted(allowed))}"
        )


def _workspace_id(result: dict[str, Any]) -> str:
    workspace = result.get("workspace") if isinstance(result, dict) else None
    if isinstance(workspace, dict) and workspace.get("id"):
        return str(workspace["id"])
    if isinstance(result, dict) and result.get("workspace_id"):
        return str(result["workspace_id"])
    raise ValueError("cdesktop did not return a workspace id")


def _primary_session_id(result: dict[str, Any]) -> str | None:
    if isinstance(result, dict) and result.get("session_id"):
        return str(result["session_id"])
    sessions = result.get("sessions") if isinstance(result, dict) else None
    if isinstance(sessions, list) and sessions and isinstance(sessions[0], dict):
        return str(sessions[0].get("id")) if sessions[0].get("id") else None
    session = result.get("session") if isinstance(result, dict) else None
    if isinstance(session, dict) and session.get("id"):
        return str(session["id"])
    execution = result.get("execution_process") if isinstance(result, dict) else None
    if isinstance(execution, dict) and execution.get("session_id"):
        return str(execution["session_id"])
    return None


def _workspace_container(
    result: dict[str, Any], client: CdesktopClient, workspace_id: str
) -> Path:
    workspace = result.get("workspace") if isinstance(result, dict) else None
    container = workspace.get("container_ref") if isinstance(workspace, dict) else None
    if not container:
        container = client.workspace(workspace_id).get("container_ref")
    if not container:
        raise ValueError("cdesktop did not return a worktree container path")
    return Path(str(container)).expanduser().resolve()


def _validate_base_branch(
    repo_path: Path, base: str, local_only: bool = False
) -> str:
    """Resolve a named base, preferring origin after a best-effort fetch."""
    if not local_only:
        # Refresh the tracking ref when the network allows; a stale local
        # checkout must not silently define a lane's base (issue #37).
        subprocess.run(
            ["git", "fetch", "origin", base, "--quiet"],
            cwd=repo_path,
            capture_output=True,
            timeout=20,
            check=False,
        )
    candidates = [f"refs/heads/{base}"] if local_only else [
        f"refs/remotes/origin/{base}",
        f"refs/heads/{base}",
    ]
    for candidate in candidates:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", candidate],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate.removeprefix("refs/remotes/").removeprefix("refs/heads/")
    preference = "local" if local_only else "origin or local"
    raise ValueError(
        f"--base must name an existing {preference} branch, not a raw commit: {base}"
    )


def _repository_setup_script(repo_path: Path, base: str) -> str | None:
    settings = ".conductor/settings.toml"
    result = subprocess.run(
        ["git", "show", f"{base}:{settings}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    data = tomllib.loads(result.stdout)
    scripts = data.get("scripts")
    setup = scripts.get("setup") if isinstance(scripts, dict) else None
    if setup is None:
        return None
    if not isinstance(setup, str):
        raise ValueError(f"{settings}: scripts.setup must be a string")  # noqa: TRY004
    return setup.strip() or None


EPHEMERAL_BASE_MARKERS = ("conductor/workspaces/", ".cdesktop-workspaces/")


def _reject_ephemeral_base(repo_path: Path, *, allow: bool) -> None:
    """A spawn worktree is tethered to the repo it branches from, so an
    ephemeral checkout (a Conductor workspace, another spawn's worktree) must
    not become that home: deleting the temp copy later breaks every derived
    worktree. Canonical checkouts live outside these directories."""
    if allow:
        return
    posix = repo_path.as_posix()
    for marker in EPHEMERAL_BASE_MARKERS:
        if f"/{marker}" in posix:
            raise ValueError(
                f"Refusing to spawn from an ephemeral checkout: {repo_path} "
                f'lives under "{marker}". Deleting that temporary copy would '
                "break every worktree derived from it. Spawn from the "
                "canonical checkout instead, or pass --ephemeral-base to "
                "accept the risk."
            )


def _spawn_workspace(args: argparse.Namespace) -> dict[str, Any]:
    prompt = _with_coordination_contract(
        _read_text(args.prompt, args.prompt_file, "prompt")
    )
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    repo_path = Path(args.repo).expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"Repository path does not exist: {repo_path}")
    _reject_ephemeral_base(repo_path, allow=getattr(args, "ephemeral_base", False))
    base_ref = _validate_base_branch(
        repo_path, args.base, getattr(args, "local_base", False)
    ) or args.base
    setup_script = (
        _repository_setup_script(repo_path, base_ref) if args.worktree else None
    )
    if args.unattended and not args.worktree:
        raise ValueError("--unattended requires --worktree")
    if args.unattended:
        if args.permission not in {None, "BYPASS_PERMISSIONS"}:
            raise ValueError(
                "--unattended cannot be combined with a supervised permission policy"
            )
        permission_policy = "BYPASS_PERMISSIONS"
    else:
        permission_policy = args.permission or "SUPERVISED"
        if permission_policy == "BYPASS_PERMISSIONS":
            raise ValueError("BYPASS_PERMISSIONS requires explicit --unattended")
    task_id = getattr(args, "task_id", None)
    task_store = tasks.TaskLaunchStore() if task_id else None
    supplied_reservation = getattr(args, "task_reservation", None)
    parent_task_id = getattr(args, "parent_task_id", None)
    caller_session_id = os.environ.get(escalation.CDESKTOP_SESSION_ENV)
    caller_task = (
        (task_store or tasks.TaskLaunchStore()).get_by_session(caller_session_id)
        if caller_session_id
        else None
    )
    if caller_task:
        if not task_id:
            raise ValueError("A managed manager must assign a stable child --task-id")
        if parent_task_id and parent_task_id != caller_task.task_id:
            raise ValueError("A managed manager cannot assign another task as parent")
        parent_task_id = caller_task.task_id
    if task_store and supplied_reservation is None:
        existing_task = task_store.get(task_id)
        if existing_task and existing_task.state == "active":
            if existing_task.session_id:
                escalation.EscalationStore().record_launcher(
                    session_id=existing_task.session_id,
                    workspace_id=existing_task.workspace_id,
                    identity=escalation.detect_launcher(),
                )
            return {"action": "task-launch-existing", "task": existing_task.to_dict()}
        if existing_task and existing_task.state == "reserved":
            supplied_reservation = tasks.LaunchReservation(
                existing_task, existing_task.reservation_id, True
            )
        elif existing_task:
            existing = task_store.reserve(
                task_id,
                parent_task_id=parent_task_id,
                max_children=getattr(args, "max_children", 0),
                max_spawn_attempts=getattr(args, "max_spawn_attempts", 3),
            )
            if not existing.should_spawn:
                return {
                    "action": "task-launch-existing",
                    "task": existing.task.to_dict(),
                }
            supplied_reservation = existing
    lease_store = leases.LeaseStore()
    if not task_store:
        lease_store.assert_spawn_allowed(repo_path, use_worktree=args.worktree)
    task_reservation = supplied_reservation or (
        task_store.reserve(
            task_id,
            parent_task_id=parent_task_id,
            max_children=getattr(args, "max_children", 0),
            max_spawn_attempts=getattr(args, "max_spawn_attempts", 3),
        )
        if task_store
        else None
    )
    if task_reservation and not task_reservation.should_spawn:
        return {"action": "task-launch-existing", "task": task_reservation.task.to_dict()}
    client = CdesktopClient(args.url)
    if task_store and task_reservation:
        try:
            client.require_task_launch_contract()
        except CdesktopRejectedError as exc:
            task_store.park_issue(
                task_reservation.task,
                message=f"BLOCKED: task {task_id} requires task-launch-v1: {exc}",
                dedupe_key=f"task-launch-capability:{task_id}",
            )
            raise
    leases.sync_active_workspaces(client)
    selection = _profile_selection(args, client)
    if task_store:
        try:
            lease_store.assert_spawn_allowed(repo_path, use_worktree=args.worktree)
        except Exception:
            if task_reservation and task_reservation.reservation_id:
                task_store.failed(task_id, task_reservation.reservation_id)
            raise
    lease_owner = f"cdesktop-spawn:{args.name}"
    pending_lease: leases.Lease | None = None
    if not args.worktree:
        try:
            pending_lease = lease_store.acquire(
                lease_owner,
                repo_path,
                ttl_seconds=args.lease_ttl_seconds,
            )
        except Exception:
            if task_store and task_reservation and task_reservation.reservation_id:
                task_store.failed(task_id, task_reservation.reservation_id)
            raise
    try:
        if task_store and task_reservation:
            assert task_reservation.reservation_id is not None
            launch_key = task_reservation.task.idempotency_key
            assert launch_key is not None
            existing_launch = client.lookup_task_launch(launch_key)
            launch_spec = client.workspace_launch_spec(
                name=args.name,
                repo_path=repo_path,
                target_branch=base_ref,
                executor=selection.executor,
                prompt=prompt,
                use_worktree=args.worktree,
                permission_policy=permission_policy,
                model=selection.model,
                reasoning=selection.reasoning,
                provider_id=selection.provider_id,
                setup_script=setup_script,
                auth_binding_id=selection.auth_binding_id,
            )
            if existing_launch is None:
                authorized = task_store.authorize_creation(
                    task_id, task_reservation.reservation_id
                )
                if not authorized.should_spawn:
                    return {
                        "action": "task-launch-blocked",
                        "task": authorized.task.to_dict(),
                    }
            native_launch = client.create_or_return_task_launch(
                task_id=task_id,
                incarnation_generation=task_reservation.task.incarnation_generation,
                attempt_id=task_reservation.reservation_id,
                idempotency_key=launch_key,
                launch=launch_spec,
            )
            if native_launch.phase != "active":
                failed_task = task_store.failed(
                    task_id, task_reservation.reservation_id
                )
                if failed_task.state != "blocked":
                    task_store.park_issue(
                        failed_task,
                        message=(
                            f"BLOCKED: task {task_id} native launch is "
                            f"{native_launch.phase}"
                        ),
                        dedupe_key=f"task-launch-outcome:{task_id}:{launch_key}",
                    )
                raise CdesktopRejectedError(
                    f"Task {task_id} native launch is {native_launch.phase}"
                )
            result = {
                "workspace_id": native_launch.workspace_id,
                "session_id": native_launch.session_id,
                "task_launch": dataclasses.asdict(native_launch),
            }
        else:
            result = client.spawn_workspace(
                name=args.name,
                repo_path=repo_path,
                target_branch=base_ref,
                executor=selection.executor,
                prompt=prompt,
                use_worktree=args.worktree,
                permission_policy=permission_policy,
                model=selection.model,
                reasoning=selection.reasoning,
                provider_id=selection.provider_id,
                setup_script=setup_script,
                auth_binding_id=selection.auth_binding_id,
            )
    except (CdesktopInterruptedError, CdesktopPendingError) as spawn_error:
        if pending_lease:
            lease_store.release(pending_lease.token)
        if task_store and task_reservation:
            task_store.park_issue(
                task_reservation.task,
                message=f"BLOCKED: task {task_id} launch is ambiguous: {spawn_error}",
                dedupe_key=(
                    f"task-launch-ambiguous:{task_id}:"
                    f"{task_reservation.reservation_id}"
                ),
            )
        raise
    except CdesktopRejectedError:
        if pending_lease:
            lease_store.release(pending_lease.token)
        raise
    except Exception as spawn_error:
        if pending_lease:
            lease_store.release(pending_lease.token)
        # A keyed create may have committed remotely before its response was
        # lost. Keep the capability reserved so recovery must replay that key.
        if task_store and task_reservation:
            task_store.park_issue(
                task_reservation.task,
                message=f"BLOCKED: task {task_id} launch is ambiguous: {spawn_error}",
                dedupe_key=(
                    f"task-launch-ambiguous:{task_id}:"
                    f"{task_reservation.reservation_id}"
                ),
            )
        _report_free_route_failure(client, args, selection, spawn_error)
        raise
    # Parsing and activation are the first local operations after the external
    # create.  If either crashes, replaying the same destination-owned key must
    # return this exact workspace; SightMesh never guesses from a listing.
    workspace_id = _workspace_id(result)
    session_id = _primary_session_id(result)
    if task_store and task_reservation and task_reservation.reservation_id:
        task = task_store.activate(
            task_id,
            task_reservation.reservation_id,
            workspace_id=workspace_id,
            session_id=session_id,
        )
        result["task"] = task.to_dict()
    if session_id:
        escalation.EscalationStore().record_launcher(
            session_id=session_id,
            workspace_id=workspace_id,
            identity=escalation.detect_launcher(),
        )
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
            lease = lease_store.attach_workspace(
                pending_lease.token, workspace_id, session_id
            )
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
        result["lease"] = lease.to_public_dict()
    if selection.profile:
        result["profile"] = selection.profile
    if selection.route_id:
        result["routing"] = {
            "route_id": selection.route_id,
            "auth_binding_id": selection.auth_binding_id,
        }
    result["base_ref"] = base_ref
    parent_selector = getattr(args, "parent_session", None) or os.environ.get(
        "CDESKTOP_SESSION_ID"
    )
    if parent_selector and session_id:
        parent = _resolve_session(client, parent_selector)
        child = client.set_parent(session_id, str(parent["session_id"]))
        result["parent"] = {
            "child_session_id": session_id,
            "child_workspace_id": workspace_id,
            "parent_session_id": child["parent_session_id"],
            "parent_workspace_id": str(parent["workspace_id"]),
        }
    return result


def cmd_spawn(args: argparse.Namespace) -> int:
    result = _spawn_workspace(args)
    _emit(result, args.json)
    return 0


def cmd_failover(args: argparse.Namespace) -> int:
    checkpoint = _read_text(args.checkpoint, args.checkpoint_file, "checkpoint")
    if not checkpoint.strip():
        raise ValueError("Checkpoint must not be empty")
    if args.archive_source and not args.confirm_reconciled:
        raise ValueError("--archive-source requires --confirm-reconciled")
    if args.archive_source and not args.new_worktree:
        raise ValueError("--archive-source requires --new-worktree")
    client = CdesktopClient(args.url)
    source = client.workspace(args.workspace_id)
    if source.get("archived"):
        raise ValueError("Cannot fail over an archived workspace")
    profile = ProfileStore().get(args.profile_name)
    if not profile.automatic_failover:
        raise ValueError(
            f"Profile {profile.name} is not approved for automatic failover"
        )
    validate_provider(profile, client.providers())
    sessions = sorted(
        client.sessions(args.workspace_id), key=lambda item: item["created_at"]
    )
    if not sessions:
        raise ValueError("Source workspace has no session to hand off")
    lead_session_id = sessions[0]["id"]
    source_session_id = str(sessions[-1]["id"])
    ownership = succession.OwnershipStore()
    launch_store = tasks.TaskLaunchStore(ownership._store)
    source_task = launch_store.get_by_session(source_session_id)
    failover_reservation = (
        launch_store.reserve_failover(source_session_id) if source_task else None
    )
    if failover_reservation and not failover_reservation.should_spawn:
        raise ValueError(
            f"Failover for task {failover_reservation.task.task_id} is already in progress "
            "or has exhausted its attempt budget"
        )
    if failover_reservation:
        try:
            client.require_task_launch_contract()
        except CdesktopRejectedError as exc:
            launch_store.park_issue(
                failover_reservation.task,
                message=(
                    f"BLOCKED: task {failover_reservation.task.task_id} "
                    f"requires task-launch-v1: {exc}"
                ),
                dedupe_key=(
                    f"task-launch-capability:{failover_reservation.task.task_id}"
                ),
            )
            raise
    if not args.new_worktree:
        prompt = (
            "Take over this visible workspace after a checkpointed capacity or provider "
            "handoff. The prior session remains in the cdesktop transcript. First inspect "
            "the branch, HEAD, working tree, and remaining scope before writing.\n\n"
            f"Source cdesktop session: {source_session_id}\n"
            f"Destination profile: {profile.name}\n\n"
            "Checkpoint:\n"
            f"{checkpoint.rstrip()}\n"
        )
        spawned: dict[str, Any] = {}

        def spawn_successor() -> str:
            if failover_reservation is None:
                replacement = client.spawn_teammate(
                    caller_session=lead_session_id,
                    name=args.name or f"successor-{profile.name}",
                    prompt=prompt,
                    executor=profile.executor,
                    permission_policy=(
                        "BYPASS_PERMISSIONS" if args.unattended else "SUPERVISED"
                    ),
                    model=profile.model,
                    reasoning=profile.reasoning,
                    provider_id=profile.provider_id,
                )
                spawned["replacement"] = replacement
                successor_id = _primary_session_id(replacement)
                if not successor_id:
                    raise ValueError("cdesktop did not return a successor session id")
                return successor_id
            assert failover_reservation.reservation_id is not None
            launch_key = failover_reservation.task.idempotency_key
            assert launch_key is not None
            existing = client.lookup_task_launch(launch_key)
            launch_spec = client.teammate_launch_spec(
                caller_session=lead_session_id,
                name=args.name or f"successor-{profile.name}",
                prompt=prompt,
                executor=profile.executor,
                permission_policy=(
                    "BYPASS_PERMISSIONS" if args.unattended else "SUPERVISED"
                ),
                model=profile.model,
                reasoning=profile.reasoning,
                provider_id=profile.provider_id,
            )
            if existing is None:
                authorized = launch_store.authorize_creation(
                    failover_reservation.task.task_id,
                    failover_reservation.reservation_id,
                )
                if not authorized.should_spawn:
                    raise ValueError(
                        f"Task {failover_reservation.task.task_id} exhausted its attempt budget"
                    )
            native = client.create_or_return_task_launch(
                task_id=failover_reservation.task.task_id,
                incarnation_generation=(
                    failover_reservation.task.incarnation_generation
                ),
                attempt_id=failover_reservation.reservation_id,
                idempotency_key=launch_key,
                launch=launch_spec,
            )
            if native.phase != "active":
                failed_task = launch_store.failed(
                    failover_reservation.task.task_id,
                    failover_reservation.reservation_id,
                )
                if failed_task.state != "blocked":
                    launch_store.park_issue(
                        failed_task,
                        message=(
                            f"BLOCKED: task {failed_task.task_id} native launch is "
                            f"{native.phase}"
                        ),
                        dedupe_key=(
                            f"task-launch-outcome:{failed_task.task_id}:{launch_key}"
                        ),
                    )
                raise CdesktopRejectedError(
                    f"Task {failover_reservation.task.task_id} native launch is "
                    f"{native.phase}"
                )
            replacement = {
                "workspace_id": native.workspace_id,
                "session_id": native.session_id,
                "task_launch": dataclasses.asdict(native),
            }
            spawned["replacement"] = replacement
            successor_id = native.session_id
            if not successor_id:
                raise ValueError("cdesktop did not return a successor session id")
            return successor_id

        # The source session shares this worktree with its successor, so it is
        # quarantined before the successor starts: terminal ownership recorded,
        # pending commands cancelled, later delivery rejected.
        handoff = succession.transfer_ownership(
            client,
            ownership,
            source_session_id=source_session_id,
            spawn=spawn_successor,
            reason=f"failover:{profile.name}",
            launch_reservation=failover_reservation,
        )
        _emit(
            {
                "action": "visible-successor-started",
                "workspace_id": args.workspace_id,
                "source_session_id": source_session_id,
                "source_preserved": True,
                "profile": profile.name,
                "replacement": spawned.get("replacement"),
                "handoff": handoff.to_dict(),
            },
            args.json,
        )
        return 0

    repos = client.workspace_repos(args.workspace_id)
    if len(repos) != 1:
        raise ValueError(
            "New-worktree failover currently requires exactly one repository"
        )
    dirty = client.dirty_repositories(args.workspace_id)
    if dirty:
        raise ValueError(
            "New-worktree failover requires a clean checkpointed source workspace. "
            f"Dirty state: {json.dumps(dirty)}"
        )
    repo = repos[0]
    source_branch = source.get("branch") or repo.get("target_branch")
    if not source_branch:
        raise ValueError("Source workspace has no branch for failover")
    prompt = (
        "Resume a checkpointed visible-agent handoff. First verify the branch, HEAD, "
        "working tree, and remaining scope before writing.\n\n"
        f"Source cdesktop workspace: {args.workspace_id}\n"
        f"Source branch: {source_branch}\n"
        f"Destination profile: {profile.name}\n\n"
        "Checkpoint:\n"
        f"{checkpoint.rstrip()}\n"
    )
    spawn_args = argparse.Namespace(
        prompt=prompt,
        prompt_file=None,
        repo=repo["path"],
        url=args.url,
        name=args.name or f"{source.get('name') or 'worker'}-{profile.name}",
        base=source_branch,
        executor=None,
        profile_name=profile.name,
        worktree=True,
        permission=None,
        unattended=args.unattended,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=args.lease_ttl_seconds,
        no_bridge=args.no_bridge,
        json=args.json,
        task_id=failover_reservation.task.task_id if failover_reservation else None,
        parent_task_id=(
            failover_reservation.task.parent_task_id if failover_reservation else None
        ),
        max_children=(
            failover_reservation.task.max_children if failover_reservation else 0
        ),
        max_spawn_attempts=(
            failover_reservation.task.max_spawn_attempts if failover_reservation else 3
        ),
        task_reservation=failover_reservation,
    )
    spawned_workspace: dict[str, Any] = {}

    def spawn_replacement() -> str:
        replacement = _spawn_workspace(spawn_args)
        spawned_workspace["replacement"] = replacement
        successor_id = _primary_session_id(replacement)
        if not successor_id:
            raise ValueError("cdesktop did not return a successor session id")
        return successor_id

    handoff = succession.transfer_ownership(
        client,
        ownership,
        source_session_id=source_session_id,
        spawn=spawn_replacement,
        reason=f"failover:{profile.name}",
        launch_reservation=failover_reservation,
    )
    result: dict[str, Any] = {
        "action": "replacement-started",
        "source_workspace_id": args.workspace_id,
        "source_archived": False,
        "profile": profile.name,
        "replacement": spawned_workspace.get("replacement"),
        "handoff": handoff.to_dict(),
    }
    if args.archive_source:
        client.stop_workspace(args.workspace_id)
        archived = client.archive_workspace(args.workspace_id)
        routing.disable(args.workspace_id)
        released = leases.LeaseStore().release_workspace_if_present(args.workspace_id)
        result.update(
            {
                "action": "replacement-started-source-archived",
                "source_archived": True,
                "source_workspace": archived,
                "released_source_lease": released.to_public_dict()
                if released
                else None,
            }
        )
    _emit(result, args.json)
    return 0



def add_spawn_parser(sub: argparse._SubParsersAction[Any]) -> None:
    spawn = sub.add_parser("spawn", help="Launch a full visible cdesktop workspace")
    spawn.add_argument("--name", required=True)
    spawn.add_argument(
        "--task-id",
        help="Stable logical task id; repeated launches return the existing record",
    )
    spawn.add_argument(
        "--parent-task-id",
        help="Active manager task whose fixed child budget owns this task",
    )
    spawn.add_argument("--max-children", type=int, default=0)
    spawn.add_argument("--max-spawn-attempts", type=int, default=3)
    spawn.add_argument("--repo", required=True)
    spawn.add_argument(
        "--base", required=True, help="Existing Git branch; origin/<base> is preferred"
    )
    spawn.add_argument(
        "--local-base",
        action="store_true",
        help="Use the local branch ref even when origin/<base> exists",
    )
    spawn.add_argument(
        "--ephemeral-base",
        action="store_true",
        help="Allow spawning from a temporary checkout (Conductor workspace "
        "or another spawn's worktree) despite the lifetime coupling",
    )
    spawn.add_argument("--executor", choices=["CLAUDE_CODE", "CODEX", "OPENCODE"])
    spawn.add_argument(
        "--profile", dest="profile_name", help="Named SightMesh provider profile"
    )
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
    spawn.add_argument("--reasoning", choices=["low", "medium", "high", "xhigh", "max"])
    spawn.add_argument("--provider", help="Configured cdesktop provider UUID")
    spawn.add_argument(
        "--parent-session",
        help="Launching cdesktop session; defaults to CDESKTOP_SESSION_ID",
    )
    spawn.add_argument(
        "--lease-ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS
    )
    spawn.add_argument("--no-bridge", action="store_true")
    spawn.set_defaults(func=cmd_spawn)




def add_failover_parser(sub: argparse._SubParsersAction[Any]) -> None:
    failover = sub.add_parser(
        "failover",
        help="Start a visible checkpointed replacement on an approved profile",
    )
    failover.add_argument("workspace_id")
    failover.add_argument("--profile", dest="profile_name", required=True)
    checkpoint_group = failover.add_mutually_exclusive_group(required=True)
    checkpoint_group.add_argument("--checkpoint")
    checkpoint_group.add_argument("--checkpoint-file")
    failover.add_argument("--name")
    failover.add_argument("--unattended", action="store_true")
    failover.add_argument(
        "--new-worktree",
        action="store_true",
        help="Start the successor in a new isolated workspace instead of this workspace",
    )
    failover.add_argument("--archive-source", action="store_true")
    failover.add_argument("--confirm-reconciled", action="store_true")
    failover.add_argument("--no-bridge", action="store_true")
    failover.add_argument(
        "--lease-ttl-seconds", type=int, default=leases.DEFAULT_TTL_SECONDS
    )
    failover.set_defaults(func=cmd_failover)
