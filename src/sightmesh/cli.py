from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dataclasses
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    __version__,
    approvals,
    conductor_migrate,
    escalation,
    execution_routing,
    fleet,
    leases,
    routing,
    service,
    succession,
    updates,
)
from .bridge import run_bridge
from .cdesktop import CdesktopClient, CdesktopError
from .pool import core as pool_core
from .pool.core import PoolError
from .profiles import (
    Profile,
    ProfileError,
    ProfileStore,
    provider_summary,
    validate_provider,
)
from .repowire import RepowireError
from .repowire import reply as repowire_reply
from .runtime_lock import RUNTIME_LOCK

CDESKTOP_FORK_MARKER = "sightmesh"
DEFAULT_OVERVIEW_HOURS = 24
COORDINATION_MARKER = "## Local agent coordination"
COORDINATION_CONTRACT = """## Local agent coordination

- Use `sightmesh peers` and `sightmesh peek @agent` for compact fleet awareness.
- Use `sightmesh steer @agent --message "..."` for immediate peer contact. It interrupts only that agent's active turn.
- Leads use `sightmesh inbox` and one `sightmesh respond --responses '...'` call for pending requests across the fleet.
- Contact your launcher with `sightmesh parent --message "STATUS: concise details"` when blocked, when a decision is needed, and when complete.
- Batch independent read-only tool calls and all currently known independent questions. Keep dependent or destructive actions sequential.
- Do not use hidden or native subagents.
"""


def _read_text(value: str | None, path: str | None, label: str) -> str:
    if bool(value) == bool(path):
        raise ValueError(f"Provide exactly one of --{label} or --{label}-file")
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    return value or ""


def _with_coordination_contract(prompt: str) -> str:
    if COORDINATION_MARKER in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{COORDINATION_CONTRACT.rstrip()}\n"


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, indent=2))


def _repowire_status_ok(returncode: int, detail: str) -> bool:
    return (
        returncode == 0
        and "Daemon responding at" in detail
        and "Daemon error" not in detail
    )


def _is_sightmesh_cdesktop_version(detail: object) -> bool:
    normalized = str(detail or "").casefold()
    if CDESKTOP_FORK_MARKER not in normalized and not normalized.startswith(
        "cdesktop/"
    ):
        return False
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", normalized)
    if not match:
        return False
    version = tuple(int(part) for part in match.groups())
    return version >= RUNTIME_LOCK.cdesktop.compatibility.minimum_tuple


def _active_runtime_matches_lock(reported_version: object) -> bool:
    """Checksum-verified provenance for servers whose /info version is bare.

    The server never announces fork identity in its version string; the
    updater already proves provenance by verifying the runtime lock's
    SHA-256 at stage time. Trust that verified activation when the running
    server reports the exact locked version.
    """
    try:
        active = updates.read_state().get("active") or {}
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError):
        return False
    if active.get("sha256") != RUNTIME_LOCK.cdesktop.package.sha256:
        return False
    return str(reported_version or "") == RUNTIME_LOCK.cdesktop.version


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


@dataclasses.dataclass(frozen=True)
class LaunchSelection:
    executor: str
    provider_id: str | None
    model: str | None
    reasoning: str | None
    profile: str | None
    route_id: str | None = None
    auth_binding_id: str | None = None


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


def _spawn_workspace(args: argparse.Namespace) -> dict[str, Any]:
    prompt = _with_coordination_contract(
        _read_text(args.prompt, args.prompt_file, "prompt")
    )
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    repo_path = Path(args.repo).expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"Repository path does not exist: {repo_path}")
    _validate_base_branch(repo_path, args.base)
    setup_script = (
        _repository_setup_script(repo_path, args.base) if args.worktree else None
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
    lease_store = leases.LeaseStore()
    lease_store.assert_spawn_allowed(repo_path, use_worktree=args.worktree)
    client = CdesktopClient(args.url)
    leases.sync_active_workspaces(client)
    selection = _profile_selection(args, client)
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
    except Exception:
        if pending_lease:
            lease_store.release(pending_lease.token)
        raise
    workspace_id = _workspace_id(result)
    session_id = _primary_session_id(result)
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

        # The source session shares this worktree with its successor, so it is
        # quarantined before the successor starts: terminal ownership recorded,
        # pending commands cancelled, later delivery rejected.
        handoff = succession.transfer_ownership(
            client,
            ownership,
            source_session_id=source_session_id,
            spawn=spawn_successor,
            reason=f"failover:{profile.name}",
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


def _approval_details(
    client: CdesktopClient, approval: dict[str, Any]
) -> dict[str, Any]:
    details = dict(approval)
    process = client.execution_process(str(approval["execution_process_id"]))
    session_id = str(process["session_id"])
    session = client.session(session_id)
    workspace_id = str(session["workspace_id"])
    workspace = client.workspace(workspace_id)
    tool_name = str(approval.get("tool_name") or "")
    request = None
    try:
        snapshot = _normalized_snapshot_with_retry(
            client, str(approval["execution_process_id"])
        )
        request = _pending_request_from_snapshot(snapshot, str(approval["approval_id"]))
    except CdesktopError:
        pass
    details.update(
        {
            "session_id": session_id,
            "session_name": session.get("name"),
            "executor": session.get("executor"),
            "workspace_id": workspace_id,
            "workspace_name": workspace.get("name"),
            "workspace_archived": bool(workspace.get("archived")),
            "request": request,
            "request_kind": (
                "question"
                if approval.get("is_question")
                else "plan"
                if tool_name == "ExitPlanMode"
                else "tool"
            ),
        }
    )
    return details


def _pending_request_from_snapshot(
    snapshot: dict[str, Any], approval_id: str
) -> dict[str, Any] | None:
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        return None
    for wrapped in reversed(entries):
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        entry_type = content.get("entry_type") if isinstance(content, dict) else None
        if not isinstance(entry_type, dict) or entry_type.get("type") != "tool_use":
            continue
        status = entry_type.get("status")
        if (
            not isinstance(status, dict)
            or status.get("status") != "pending_approval"
            or str(status.get("approval_id")) != approval_id
        ):
            continue
        action = entry_type.get("action_type")
        return {
            "summary": _compact_text(content.get("content"), 600),
            "action": action if isinstance(action, dict) else None,
        }
    return None


def _approval_response_template(approval: dict[str, Any]) -> dict[str, Any]:
    template: dict[str, Any] = {"approval_id": approval["approval_id"]}
    if approval.get("is_question"):
        action = (approval.get("request") or {}).get("action")
        questions = action.get("questions") if isinstance(action, dict) else None
        template["answers"] = (
            [""] * len(questions) if isinstance(questions, list) else []
        )
    else:
        template["decision"] = "approve|deny"
        if approval.get("request_kind") != "plan":
            template["allow_non_plan"] = False
    return template


def _approval_details_batch(
    client: CdesktopClient, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not items:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
        return list(pool.map(lambda item: _approval_details(client, item), items))


def cmd_inbox(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    fleet_rows = _fleet_sessions(client)
    selectors = {str(row["session_id"]): f"@{row['selector']}" for row in fleet_rows}
    rows = []
    for details in _approval_details_batch(client, client.pending_approvals()):
        details["agent"] = selectors.get(
            str(details["session_id"]), str(details["session_id"])
        )
        details["response_template"] = _approval_response_template(details)
        rows.append(details)
    rows.extend(_idle_unmet_orders(client, fleet_rows))
    _emit(rows, args.json)
    return 0


def _response_items(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Responses are not valid JSON: {exc}") from exc
    if isinstance(parsed, dict):
        parsed = parsed.get("responses")
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Responses must be a non-empty JSON array")
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError("Every batch response must be a JSON object")
    return [dict(item) for item in parsed]


def _question_items(approval: dict[str, Any]) -> list[dict[str, Any]]:
    action = (approval.get("request") or {}).get("action")
    questions = action.get("questions") if isinstance(action, dict) else None
    if not isinstance(questions, list) or not all(
        isinstance(question, dict) for question in questions
    ):
        raise ValueError(
            f"Question details are unavailable for approval {approval['approval_id']}"
        )
    return [dict(question) for question in questions]


def _structured_question_answers(
    approval: dict[str, Any], supplied: object
) -> list[dict[str, Any]]:
    questions = _question_items(approval)
    if not isinstance(supplied, list) or len(supplied) != len(questions):
        raise ValueError(
            f"Approval {approval['approval_id']} requires exactly "
            f"{len(questions)} ordered answers"
        )
    normalized = []
    for index, (question, answer_value) in enumerate(
        zip(questions, supplied, strict=True)
    ):
        expected_text = str(question.get("question") or "")
        if isinstance(answer_value, dict):
            supplied_question = answer_value.get("question")
            if (
                supplied_question is not None
                and str(supplied_question) != expected_text
            ):
                raise ValueError(
                    f"Approval {approval['approval_id']} answer {index + 1} "
                    "does not match its question"
                )
            answer_value = answer_value.get("answer")
        values = [answer_value] if isinstance(answer_value, str) else answer_value
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value.strip() for value in values)
        ):
            raise ValueError(
                f"Approval {approval['approval_id']} answer {index + 1} must "
                "contain one or more non-empty strings"
            )
        if not question.get("multiSelect") and len(values) != 1:
            raise ValueError(
                f"Approval {approval['approval_id']} answer {index + 1} is single-select"
            )
        normalized.append(
            {"question": expected_text, "answer": [value.strip() for value in values]}
        )
    return normalized


def _prepare_batch_responses(
    client: CdesktopClient,
    items: list[dict[str, Any]],
    reviewer_session: str | None,
) -> list[dict[str, Any]]:
    pending_items = client.pending_approvals()
    pending = {
        str(item["approval_id"]): item
        for item in _approval_details_batch(client, pending_items)
    }
    seen: set[str] = set()
    prepared = []
    for item in items:
        approval_id = str(item.get("approval_id") or "")
        if not approval_id or approval_id in seen:
            raise ValueError("Every batch item needs a unique approval_id")
        seen.add(approval_id)
        approval = pending.get(approval_id)
        if approval is None:
            raise ValueError(f"Approval is not currently pending: {approval_id}")
        reviewer_kind, reviewer_id = _approval_reviewer(
            client, reviewer_session, str(approval["session_id"])
        )
        if approval.get("is_question"):
            if "decision" in item:
                raise ValueError(
                    f"Question {approval_id} expects answers, not a decision"
                )
            prepared.append(
                {
                    "approval": approval,
                    "kind": "question",
                    "answers": _structured_question_answers(
                        approval, item.get("answers")
                    ),
                    "reviewer_kind": reviewer_kind,
                    "reviewer_id": reviewer_id,
                }
            )
            continue

        decision = str(item.get("decision") or "")
        if decision not in {"approve", "deny"}:
            raise ValueError(f"Approval {approval_id} decision must be approve or deny")
        if (
            decision == "approve"
            and approval["request_kind"] != "plan"
            and item.get("allow_non_plan") is not True
        ):
            raise ValueError(
                f"Approval {approval_id} is a non-plan tool request; set "
                '"allow_non_plan": true only after reviewing the exact action'
            )
        reason = item.get("reason")
        if decision == "deny" and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError(f"Approval {approval_id} denial requires a reason")
        prepared.append(
            {
                "approval": approval,
                "kind": "approval",
                "approved": decision == "approve",
                "reason": reason.strip() if isinstance(reason, str) else None,
                "reviewer_kind": reviewer_kind,
                "reviewer_id": reviewer_id,
            }
        )
    return prepared


def cmd_respond(args: argparse.Namespace) -> int:
    payload = _read_text(args.responses, args.responses_file, "responses")
    items = _response_items(payload)
    client = CdesktopClient(args.url)
    prepared = _prepare_batch_responses(client, items, args.reviewer_session)
    audit = approvals.ApprovalAuditStore()
    results = []
    failures = 0
    for item in prepared:
        approval = item["approval"]
        attempt = None
        try:
            if item["kind"] == "question":
                response = client.respond_to_question(
                    str(approval["approval_id"]),
                    str(approval["execution_process_id"]),
                    item["answers"],
                )
            else:
                attempt = audit.begin(
                    approval=approval,
                    decision="approved" if item["approved"] else "denied",
                    reviewer_kind=item["reviewer_kind"],
                    reviewer_id=item["reviewer_id"],
                    reason=item["reason"],
                )
                response = client.respond_to_approval(
                    str(approval["approval_id"]),
                    str(approval["execution_process_id"]),
                    approved=item["approved"],
                    reason=item["reason"],
                )
                audit.finish(attempt.decision_id, succeeded=True)
            results.append(
                {
                    "approval_id": approval["approval_id"],
                    "status": "responded",
                    "response": response,
                }
            )
        except (CdesktopError, approvals.ApprovalAuditError) as exc:
            failures += 1
            if attempt is not None:
                audit.finish(attempt.decision_id, succeeded=False, error=str(exc))
            results.append(
                {
                    "approval_id": approval["approval_id"],
                    "status": "failed",
                    "error": str(exc),
                }
            )
    reporter = args.reviewer_session or os.environ.get("CDESKTOP_SESSION_ID")
    if reporter and failures == 0:
        escalation.EscalationStore().satisfy_orders(reporter)
    _emit({"results": results, "failed": failures}, args.json)
    return int(failures > 0)


def _pending_approval(client: CdesktopClient, approval_id: str) -> dict[str, Any]:
    approval = next(
        (
            item
            for item in client.pending_approvals()
            if item.get("approval_id") == approval_id
        ),
        None,
    )
    if approval is None:
        raise ValueError(f"Approval is not currently pending: {approval_id}")
    return _approval_details(client, approval)


def _approval_reviewer(
    client: CdesktopClient,
    explicit_session: str | None,
    target_session_id: str,
) -> tuple[str, str]:
    reviewer_session_id = explicit_session or os.environ.get("CDESKTOP_SESSION_ID")
    if not reviewer_session_id:
        return "human", f"{getpass.getuser()}@local"
    if reviewer_session_id == target_session_id:
        raise ValueError("A session cannot approve its own plan")
    reviewer = client.session(reviewer_session_id)
    reviewer_workspace_id = str(reviewer["workspace_id"])
    sessions = sorted(
        client.sessions(reviewer_workspace_id),
        key=lambda item: (str(item.get("created_at") or ""), str(item["id"])),
    )
    if not sessions or str(sessions[0]["id"]) != reviewer_session_id:
        raise ValueError(
            "Only the lead session in a cdesktop workspace may approve another "
            "agent's plan"
        )
    return "session", reviewer_session_id


def cmd_approval(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    if args.approval_action == "history":
        records = [
            record.to_dict()
            for record in approvals.ApprovalAuditStore().history(limit=args.limit)
        ]
        _emit(records, args.json)
        return 0

    if args.approval_action == "list":
        rows = [_approval_details(client, item) for item in client.pending_approvals()]
        if args.workspace_id:
            rows = [item for item in rows if item["workspace_id"] == args.workspace_id]
        if args.session_id:
            rows = [item for item in rows if item["session_id"] == args.session_id]
        _emit(rows, args.json)
        return 0

    approval = _pending_approval(client, args.approval_id)
    if args.approval_action == "show":
        _emit(approval, args.json)
        return 0
    if approval.get("is_question"):
        raise ValueError(
            "This request expects structured answers, not plan approval; answer it in "
            "the visible cdesktop session"
        )

    approved = args.approval_action == "approve"
    if approved and approval["request_kind"] != "plan" and not args.allow_non_plan:
        raise ValueError(
            "Refusing to approve a non-plan tool request. Review it in cdesktop or "
            "repeat with --allow-non-plan after verifying the exact tool action."
        )
    reason = None
    if not approved:
        reason = _read_text(args.reason, args.reason_file, "reason")
        if not reason.strip():
            raise ValueError("Rejection reason must not be empty")
    reviewer_kind, reviewer_id = _approval_reviewer(
        client,
        args.reviewer_session,
        str(approval["session_id"]),
    )
    store = approvals.ApprovalAuditStore()
    attempt = store.begin(
        approval=approval,
        decision="approved" if approved else "denied",
        reviewer_kind=reviewer_kind,
        reviewer_id=reviewer_id,
        reason=reason,
    )
    try:
        response = client.respond_to_approval(
            str(approval["approval_id"]),
            str(approval["execution_process_id"]),
            approved=approved,
            reason=reason,
        )
    except Exception as exc:
        store.finish(attempt.decision_id, succeeded=False, error=str(exc))
        raise
    completed = store.finish(attempt.decision_id, succeeded=True)
    _emit(
        {
            "approval": approval,
            "decision": completed.to_dict(),
            "cdesktop_response": response,
        },
        args.json,
    )
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


def cmd_update(args: argparse.Namespace) -> int:
    if args.update_action == "stage":
        pinned = RUNTIME_LOCK.cdesktop
        package = args.package or pinned.package.url
        version = args.version or pinned.version
        sha256 = args.sha256
        if args.package is None:
            sha256 = sha256 or pinned.package.sha256
        elif sha256 is None and not args.local_development:
            raise ValueError(
                "Package overrides require --sha256 or explicit --local-development"
            )
        result = updates.stage(
            package,
            version,
            expected_sha256=sha256,
        )
    elif args.update_action == "activate":
        result = updates.activate_if_idle(
            CdesktopClient(args.url),
            port=args.port,
        )
    elif args.update_action == "status":
        result = updates.read_state()
        if result.get("pending") and service.is_healthy(args.port):
            result["activity"] = updates.activity(CdesktopClient(args.url))
    elif args.update_action == "cancel":
        result = updates.cancel()
    elif args.update_action == "prune":
        result = updates.prune(keep=args.keep, dry_run=args.dry_run)
    else:
        raise ValueError(f"Unknown update action: {args.update_action}")
    if not getattr(args, "quiet", False):
        _emit(result, args.json)
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
    client.stop_workspace(args.workspace_id)
    archived = client.archive_workspace(args.workspace_id)
    routing.disable(args.workspace_id)
    released = leases.LeaseStore().release_workspace_if_present(args.workspace_id)
    _emit(
        {
            "workspace": archived,
            "action": "stopped-and-archived",
            "preserved_dirty": dirty,
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
        missing = [
            item for item in dirty if item.get("status") == "repository path is missing"
        ]
        substantive_dirty = [item for item in dirty if item not in missing]
        if missing and not args.allow_missing_repo:
            raise ValueError(
                "The archived direct workspace's repository path is missing. Refusing "
                "to delete its remaining cdesktop history without --allow-missing-repo. "
                f"Missing state: {json.dumps(missing)}"
            )
        if substantive_dirty and workspace.get("use_worktree"):
            raise ValueError(
                "Refusing to delete an archived managed worktree with dirty files. "
                f"Dirty state: {json.dumps(substantive_dirty)}"
            )
        if substantive_dirty and not args.preserve_dirty:
            raise ValueError(
                "Refusing to delete cdesktop history for a dirty direct workspace "
                "without --preserve-dirty. The repository itself will remain untouched. "
                f"Dirty state: {json.dumps(substantive_dirty)}"
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
                "preserved_dirty": substantive_dirty,
                "cdesktop_result": result,
                "released_lease": released.to_public_dict() if released else None,
            },
            args.json,
        )
        return 0
    raise ValueError(f"Unknown workspace action: {args.workspace_action}")


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


def cmd_profile(args: argparse.Namespace) -> int:
    store = ProfileStore()
    if args.profile_action == "list":
        _emit([profile.to_dict() for profile in store.list()], args.json)
        return 0
    if args.profile_action == "providers":
        providers = [
            provider_summary(item) for item in CdesktopClient(args.url).providers()
        ]
        _emit(providers, args.json)
        return 0
    if args.profile_action == "set":
        _validate_reasoning(args.executor, args.reasoning)
        profile = Profile(
            name=args.name,
            executor=args.executor,
            provider_id=args.provider,
            credential_kind=args.credential_kind,
            model=args.model,
            reasoning=args.reasoning,
            automatic_failover=args.automatic_failover,
        )
        validate_provider(profile, CdesktopClient(args.url).providers())
        _emit(store.set(profile).to_dict(), args.json)
        return 0
    if args.profile_action == "remove":
        _emit(store.remove(args.name).to_dict(), args.json)
        return 0
    raise ValueError(f"Unknown profile action: {args.profile_action}")


def cmd_routing(args: argparse.Namespace) -> int:
    store = execution_routing.ExecutionRoutingStore()
    action = args.routing_action

    if action == "show":
        _emit(store.load().to_dict(), args.json)
        return 0

    if action == "validate":
        settings = store.load()
        warnings = execution_routing.route_warnings(settings)
        _emit(
            {"valid": not warnings, "warnings": warnings},
            args.json,
        )
        return 0

    if action == "set-metered":
        updated = store.save(dataclasses.replace(store.load(), metered_fallback=args.value))
        _emit(updated.to_dict(), args.json)
        return 0

    if action == "routes":
        return _cmd_routing_routes(args, store)

    if action == "explain":
        settings = store.load()
        result = execution_routing.select_route(settings, preferred_model=args.model)
        payload = {**result.to_dict(), "workspace_id": args.workspace}
        _emit(payload, args.json)
        return 0

    raise ValueError(f"Unknown routing action: {action}")


def _cmd_routing_routes(
    args: argparse.Namespace, store: execution_routing.ExecutionRoutingStore
) -> int:
    settings = store.load()
    action = args.routes_action

    if action == "list":
        _emit([route.to_dict() for route in settings.routes], args.json)
        return 0

    if action == "add":
        if any(existing.id == args.id for existing in settings.routes):
            raise execution_routing.ExecutionRoutingError(
                f"Route already exists: {args.id}"
            )
        route = execution_routing.Route(
            id=args.id,
            executor=args.executor,
            model=args.model,
            billing_class=args.billing_class,
            account_pool=args.account_pool,
            account=args.account,
        )
        routes = [*settings.routes, route]
        if args.before:
            routes = [r for r in routes if r.id != route.id]
            index = next((i for i, r in enumerate(routes) if r.id == args.before), None)
            if index is None:
                raise execution_routing.ExecutionRoutingError(
                    f"Unknown route: {args.before}"
                )
            routes = [*routes[:index], route, *routes[index:]]
        updated = store.save(dataclasses.replace(settings, routes=tuple(routes)))
        _emit([r.to_dict() for r in updated.routes], args.json)
        return 0

    if action == "remove":
        if not any(r.id == args.id for r in settings.routes):
            raise execution_routing.ExecutionRoutingError(f"Unknown route: {args.id}")
        routes = tuple(r for r in settings.routes if r.id != args.id)
        updated = store.save(dataclasses.replace(settings, routes=routes))
        _emit([r.to_dict() for r in updated.routes], args.json)
        return 0

    if action == "order":
        current_ids = [r.id for r in settings.routes]
        if sorted(args.ids) != sorted(current_ids):
            raise execution_routing.ExecutionRoutingError(
                f"must list every route exactly once (have: {' '.join(current_ids)})"
            )
        by_id = {r.id: r for r in settings.routes}
        routes = tuple(by_id[i] for i in args.ids)
        updated = store.save(dataclasses.replace(settings, routes=routes))
        _emit([r.id for r in updated.routes], args.json)
        return 0

    raise ValueError(f"Unknown routes action: {action}")


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


def _pool_quota_text(usage: dict[str, Any]) -> str:
    if usage.get("metered"):
        return "metered"
    if not usage.get("known"):
        return usage.get("reason") or "quota unknown"
    resets = usage.get("resetsIn")
    suffix = (
        f", resets in {pool_core.fmt_delta(resets)}" if resets and resets > 0 else ""
    )
    return f"{usage.get('remaining'):.0f}% left{suffix}"


def _pool_row_mark(row: dict[str, Any]) -> str:
    if not row["hasCredential"]:
        return "NO CREDENTIAL"
    if row["coolingFor"]:
        return f"cooling {pool_core.fmt_delta(row['coolingFor'])}"
    if row["health"] == "unhealthy":
        return f"unhealthy: {row.get('healthReason') or 'failed'}"
    return _pool_quota_text(row["quota"]) if row["quota"] else "unprobed"


def _pool_listing_text(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    for provider, rows in snapshot["providers"].items():
        if not rows:
            continue
        lines.append(f"\n{provider}:")
        for row in rows:
            lines.append(
                f"  {row['position']}. {row['id']:<14} {row['label']:<40} "
                f"{_pool_row_mark(row)}"
            )
    return "\n".join(lines) if lines else "pool is empty"


def _pool_read_token_input() -> str:
    """Read a token the terminal may have wrapped onto several lines.

    Each hidden read takes one line, so a two-line paste needs two reads. It
    stops as soon as the accumulated value validates, which keeps an ordinary
    single-line paste to one Enter.
    """
    print("  Paste the token (both lines if it wrapped), then Enter on a blank line.\n")
    parts: list[str] = []
    while True:
        try:
            line = getpass.getpass("token: " if not parts else "  ...: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        parts.append(line)
        token = pool_core.normalize_token("".join(parts))
        if pool_core.validate_claude_token(token) is None:
            return token
    return pool_core.normalize_token("".join(parts))


def _pool_add_claude(args: argparse.Namespace) -> int:
    pool = pool_core.load_pool()
    if pool_core.find(pool, args.name):
        raise PoolError(f"Account already exists: {args.name}")

    identity = pool_core.ambient_claude_identity()
    if not identity.get("email"):
        raise PoolError(identity.get("error", "Cannot determine the logged-in account"))

    print(
        f"\n  Currently logged in as: {identity['email']} ({identity.get('subscription')})"
    )
    print("  `claude setup-token` mints a token for THIS account.\n")
    print("  In another terminal run:  claude setup-token")
    print("  Do NOT log out first - that revokes the token.\n")
    token = _pool_read_token_input()
    problem = pool_core.validate_claude_token(token)
    if problem:
        raise PoolError(problem)

    candidate = {
        "id": args.name,
        "provider": "claude",
        "kind": "oauth",
        "label": args.label or identity["email"],
        "identity": identity,
        "token_fp": pool_core.fingerprint(token),
    }
    duplicate = pool_core.check_duplicate(pool, candidate)
    if duplicate and not args.force:
        raise PoolError(
            f"{duplicate['id']} already uses {pool_core.identity_label(duplicate)} "
            "- pass --force to add anyway"
        )

    pool_core.write_token(args.name, token)
    print("\n  validating with a real request...")
    ok, reason = pool_core.probe(candidate)
    if not ok:
        pool_core.token_path(args.name).unlink(missing_ok=True)
        raise PoolError(
            f"Token rejected: {reason}. `claude auth logout` invalidates tokens minted "
            "by that session - mint the token and add it before switching accounts."
        )

    pool.setdefault("accounts", []).append(candidate)
    pool_core.save_pool(pool)
    print(f"  added {args.name}: {pool_core.identity_label(candidate)}")
    print(f"  token stored: {pool_core.shape(token)}")
    return 0


def _pool_add_codex(args: argparse.Namespace) -> int:
    pool = pool_core.load_pool()
    if pool_core.find(pool, args.name):
        raise PoolError(f"Account already exists: {args.name}")

    codex_home = Path(os.path.expanduser(args.home or f"~/.codex-{args.name}"))
    codex_home.mkdir(parents=True, exist_ok=True)
    primary = Path.home() / ".codex" / "config.toml"
    if primary.exists() and not (codex_home / "config.toml").exists():
        shutil.copy2(primary, codex_home / "config.toml")
        print(f"  copied config.toml from ~/.codex -> {codex_home}")

    # Codex stores exactly one auth mode per CODEX_HOME, so each account owns one.
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    token = None
    if args.mode == "apikey":
        token = getpass.getpass("OpenAI API key: ").strip()
        if not token:
            raise PoolError("No key provided")
        run = subprocess.run(
            ["codex", "login", "--with-api-key"],
            env=env,
            input=token,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if run.returncode != 0:
            raise PoolError(f"codex login failed: {(run.stderr or '').strip()[:200]}")
    else:
        print(f"\n  Opening Codex browser login for '{args.name}'.")
        print("  Sign in with the ChatGPT account holding the subscription.\n")
        if subprocess.run(["codex", "login"], env=env, check=False).returncode != 0:
            raise PoolError("codex login failed")

    candidate = {
        "id": args.name,
        "provider": "codex",
        "kind": "chatgpt" if args.mode == "sub" else "apikey",
        "codex_home": str(codex_home),
        "identity": pool_core.codex_identity(str(codex_home)),
    }
    if token:
        candidate["token_fp"] = pool_core.fingerprint(token)
    candidate["label"] = args.label or pool_core.identity_label(candidate)

    duplicate = pool_core.check_duplicate(pool, candidate)
    if duplicate and not args.force:
        raise PoolError(
            f"{duplicate['id']} already uses {pool_core.identity_label(duplicate)} "
            "- pass --force to add anyway"
        )

    if token:
        pool_core.write_token(args.name, token)
    pool.setdefault("accounts", []).append(candidate)
    pool_core.save_pool(pool)
    print(f"\n  added {args.name}: {pool_core.identity_label(candidate)}")
    return 0


def _pool_verify(as_json: bool) -> int:
    """Prove every pooled account is a distinct, usable, owned account."""
    pool = pool_core.load_pool()
    accounts = pool.get("accounts", [])
    if not accounts:
        _emit("pool is empty", as_json)
        return 0

    changed = False
    for account in accounts:
        if account.get("provider") == "codex":
            fresh = pool_core.codex_identity(
                os.path.expanduser(account.get("codex_home", ""))
            )
            if fresh and fresh != account.get("identity"):
                account["identity"] = fresh
                changed = True
    if changed:
        pool_core.save_pool(pool)

    seen: dict[str, str] = {}
    results = []
    for account in accounts:
        key = pool_core.identity_key(account)
        duplicate_of = seen.get(key) if key else None
        if key and not duplicate_of:
            seen[key] = account["id"]
        usage = pool_core.quota_cached(account, force=True)
        ok, reason = pool_core.probe(account)
        results.append(
            {
                "id": account["id"],
                "identity": pool_core.identity_label(account),
                "unique": "duplicate" if duplicate_of else "yes" if key else "unknown",
                "duplicate_of": duplicate_of,
                "quota": _pool_quota_text(usage),
                "health": "ok" if ok else reason,
            }
        )

    problems = [
        r
        for r in results
        if r["duplicate_of"] or r["health"] not in ("ok", "usage limit")
    ]
    if as_json:
        _emit({"accounts": results, "problems": [r["id"] for r in problems]}, True)
        return 1 if problems else 0

    print(f"\n{'account':<14} {'identity':<42} {'unique':<10} {'quota':<28} health")
    print("-" * 116)
    for row in results:
        print(
            f"{row['id']:<14} {row['identity']:<42} {row['unique']:<10} "
            f"{row['quota']:<28} {row['health']}"
        )
    print()
    for row in problems:
        if row["duplicate_of"]:
            print(
                f"  ! {row['id']} is the same account as {row['duplicate_of']} - remove one"
            )
        else:
            print(f"  ! {row['id']} is not usable: {row['health']}")
    if not problems:
        print("  all accounts distinct and usable")
    print()
    return 1 if problems else 0


def cmd_pool(args: argparse.Namespace) -> int:
    action = args.pool_action

    if action == "list":
        snapshot = pool_core.snapshot()
        _emit(snapshot if args.json else _pool_listing_text(snapshot), args.json)
        return 0

    if action == "status":
        pool = pool_core.load_pool()
        report = {}
        for provider in pool_core.PROVIDERS:
            if not pool_core.accounts_for(pool, provider):
                continue
            chosen, notes = pool_core.select(provider, verify=True)
            report[provider] = {
                "selected": chosen["id"] if chosen else None,
                "skipped": notes,
            }
        if args.json:
            _emit(report, True)
            return 0
        for provider, entry in report.items():
            print(f"\n{provider}:")
            for note in entry["skipped"]:
                print(f"    {note}")
            print(f"  -> {entry['selected'] or 'NO ACCOUNT AVAILABLE'}")
        print()
        return 0

    if action == "which":
        chosen, _ = pool_core.select(args.provider, verify=args.verify)
        if not chosen:
            raise PoolError(f"No {args.provider} account available")
        _emit(chosen["id"], args.json)
        return 0

    if action == "exec":
        # Preferred launcher: the credential is handed to the child process
        # directly, so it never reaches the terminal or shell history.
        chosen, notes = pool_core.select(args.provider, verify=not args.no_verify)
        for note in notes:
            print(f"# {note}", file=sys.stderr)
        if not chosen:
            raise PoolError(f"No {args.provider} account available")
        binary = "claude" if args.provider == "claude" else "codex"
        print(
            f"# using {chosen['id']} ({pool_core.identity_label(chosen)})",
            file=sys.stderr,
        )
        overlay = {
            **os.environ,
            **pool_core.env_for(chosen),
            "SIGHTMESH_POOL_ACCOUNT": chosen["id"],
        }
        try:
            os.execvpe(binary, [binary, *args.argv], overlay)
        except FileNotFoundError as exc:
            raise PoolError(f"{binary} is not on PATH") from exc

    if action == "order":
        pool = pool_core.load_pool()
        if not args.ids:
            _emit(
                [a["id"] for a in pool_core.accounts_for(pool, args.provider)],
                args.json,
            )
            return 0
        error = pool_core.reorder(pool, args.provider, args.ids)
        if error:
            raise PoolError(error)
        pool_core.save_pool(pool)
        _emit([a["id"] for a in pool_core.accounts_for(pool, args.provider)], args.json)
        return 0

    if action == "promote":
        pool = pool_core.load_pool()
        account = pool_core.find(pool, args.name)
        if not account:
            raise PoolError(f"Unknown account: {args.name}")
        order = [a["id"] for a in pool_core.accounts_for(pool, account["provider"])]
        order.remove(args.name)
        error = pool_core.reorder(pool, account["provider"], [args.name, *order])
        if error:
            raise PoolError(error)
        pool_core.save_pool(pool)
        _emit(
            [a["id"] for a in pool_core.accounts_for(pool, account["provider"])],
            args.json,
        )
        return 0

    if action == "quota":
        pool = pool_core.load_pool()
        targets = [
            pool_core.find(pool, name)
            for name in args.names
            if pool_core.find(pool, name)
        ] or pool.get("accounts", [])
        report = [
            {
                "id": account["id"],
                "identity": pool_core.identity_label(account),
                "quota": pool_core.quota_cached(account, force=args.refresh),
            }
            for account in targets
        ]
        if args.json:
            _emit(report, True)
            return 0
        for entry in report:
            print(f"\n{entry['id']}  {entry['identity']}")
            usage = entry["quota"]
            if not usage.get("known"):
                print(f"  {usage.get('reason', 'unknown')}")
                continue
            for window in usage.get("windows", []):
                print(
                    f"  {window.get('label'):<26} {window.get('remaining')}% left"
                    f"   resets in {pool_core.fmt_delta(window.get('resetsIn') or 0)}"
                    f"  ({window.get('resetsAt')})"
                )
            print(f"  effective: {usage.get('remaining')}% remaining")
        print()
        return 0

    if action == "verify":
        return _pool_verify(args.json)

    if action == "cool":
        if not pool_core.find(pool_core.load_pool(), args.name):
            raise PoolError(f"Unknown account: {args.name}")
        seconds = pool_core.parse_duration(args.duration)
        pool_core.set_cooldown(args.name, seconds)
        _emit(f"{args.name} cooling for {pool_core.fmt_delta(seconds)}", args.json)
        return 0

    if action == "clear":
        if args.all:
            pool_core.save_state({"cooldowns": {}, "probes": {}, "quota": {}})
            _emit("cleared all cooldowns", args.json)
            return 0
        if not args.name:
            raise PoolError("Provide an account id or --all")
        pool_core.clear_cooldown(args.name)
        _emit(f"cleared {args.name}", args.json)
        return 0

    if action == "remove":
        pool = pool_core.load_pool()
        if not pool_core.find(pool, args.name):
            raise PoolError(f"Unknown account: {args.name}")
        pool["accounts"] = [a for a in pool["accounts"] if a["id"] != args.name]
        pool_core.save_pool(pool)
        pool_core.token_path(args.name).unlink(missing_ok=True)
        pool_core.clear_cooldown(args.name)
        _emit(f"removed {args.name}", args.json)
        return 0

    if action == "add-claude":
        return _pool_add_claude(args)

    if action == "add-codex":
        return _pool_add_codex(args)

    if action == "serve":
        from .pool import server

        return server.serve(args.port, not args.no_open)

    raise ValueError(f"Unknown pool action: {action}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sightmesh")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--url", help="Exact local cdesktop backend URL")
    root.add_argument("--json", action="store_true", help="Emit JSON")
    sub = root.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Verify local orchestration dependencies")
    doctor.set_defaults(func=cmd_doctor)

    listing = sub.add_parser("list", help="List cdesktop workspaces and sessions")
    listing.set_defaults(func=cmd_list)

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

    inbox = sub.add_parser(
        "inbox", help="Show every pending agent question, plan, and tool request"
    )
    inbox.set_defaults(func=cmd_inbox)

    respond = sub.add_parser(
        "respond", help="Prevalidate and answer multiple pending requests in one call"
    )
    response_source = respond.add_mutually_exclusive_group(required=True)
    response_source.add_argument("--responses", help="Inline JSON response array")
    response_source.add_argument(
        "--responses-file", help="Path to a JSON response file"
    )
    respond.add_argument(
        "--reviewer-session",
        help="Lead session responding; defaults to CDESKTOP_SESSION_ID",
    )
    respond.set_defaults(func=cmd_respond)

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

    configure = sub.add_parser("configure", help="Enforce local-only cdesktop settings")
    configure.add_argument(
        "--workspace-root",
        default=str(Path.home() / ".local" / "share" / "sightmesh"),
    )
    configure.set_defaults(func=cmd_configure)

    spawn = sub.add_parser("spawn", help="Launch a full visible cdesktop workspace")
    spawn.add_argument("--name", required=True)
    spawn.add_argument("--repo", required=True)
    spawn.add_argument(
        "--base", required=True, help="Existing local or remote Git branch"
    )
    spawn.add_argument("--executor", choices=["CLAUDE_CODE", "CODEX"])
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

    approval = sub.add_parser(
        "approval", help="Review and respond to visible cdesktop plan approvals"
    )
    approval_sub = approval.add_subparsers(dest="approval_action", required=True)
    approval_list = approval_sub.add_parser(
        "list", help="List currently pending cdesktop approvals"
    )
    approval_list.add_argument("--workspace-id")
    approval_list.add_argument("--session-id")
    approval_list.set_defaults(func=cmd_approval)
    approval_show = approval_sub.add_parser(
        "show", help="Show one currently pending approval with workspace context"
    )
    approval_show.add_argument("approval_id")
    approval_show.set_defaults(func=cmd_approval)
    approval_approve = approval_sub.add_parser(
        "approve", help="Approve a reviewed plan and continue its visible session"
    )
    approval_approve.add_argument("approval_id")
    approval_approve.add_argument(
        "--reviewer-session",
        help="Lead cdesktop session performing the review; defaults to CDESKTOP_SESSION_ID",
    )
    approval_approve.add_argument(
        "--allow-non-plan",
        action="store_true",
        help="Explicitly allow a reviewed non-plan tool request",
    )
    approval_approve.set_defaults(func=cmd_approval)
    approval_reject = approval_sub.add_parser(
        "reject", help="Reject a pending approval with actionable feedback"
    )
    approval_reject.add_argument("approval_id")
    rejection_reason = approval_reject.add_mutually_exclusive_group(required=True)
    rejection_reason.add_argument("--reason")
    rejection_reason.add_argument("--reason-file")
    approval_reject.add_argument(
        "--reviewer-session",
        help="Lead cdesktop session performing the review; defaults to CDESKTOP_SESSION_ID",
    )
    approval_reject.set_defaults(func=cmd_approval, allow_non_plan=False)
    approval_history = approval_sub.add_parser(
        "history", help="Show the private local approval decision audit"
    )
    approval_history.add_argument("--limit", type=int, default=50)
    approval_history.set_defaults(func=cmd_approval)

    profile = sub.add_parser(
        "profile", help="Manage safe named mappings to configured cdesktop providers"
    )
    profile_sub = profile.add_subparsers(dest="profile_action", required=True)
    profile_list = profile_sub.add_parser("list", help="List SightMesh profiles")
    profile_list.set_defaults(func=cmd_profile)
    profile_providers = profile_sub.add_parser(
        "providers", help="List redacted cdesktop provider metadata"
    )
    profile_providers.set_defaults(func=cmd_profile)
    profile_set = profile_sub.add_parser("set", help="Create or update a named profile")
    profile_set.add_argument("name")
    profile_set.add_argument(
        "--executor", choices=["CLAUDE_CODE", "CODEX"], required=True
    )
    profile_set.add_argument(
        "--provider", required=True, help="Configured cdesktop provider UUID"
    )
    profile_set.add_argument(
        "--credential-kind", choices=["ambient", "api", "enterprise"], default="ambient"
    )
    profile_set.add_argument("--model")
    profile_set.add_argument(
        "--reasoning", choices=["low", "medium", "high", "xhigh", "max"]
    )
    profile_set.add_argument(
        "--automatic-failover",
        action="store_true",
        help="Allow checkpointed failover to this configured profile",
    )
    profile_set.set_defaults(func=cmd_profile)
    profile_remove = profile_sub.add_parser("remove", help="Remove a named profile")
    profile_remove.add_argument("name")
    profile_remove.set_defaults(func=cmd_profile)

    routing_group = sub.add_parser(
        "routing", help="Subscription-first execution routing policy"
    )
    routing_sub = routing_group.add_subparsers(dest="routing_action", required=True)

    routing_show = routing_sub.add_parser("show", help="Show execution routing settings")
    routing_show.set_defaults(func=cmd_routing)

    routing_validate = routing_sub.add_parser(
        "validate", help="Validate settings and report routes with no eligible account"
    )
    routing_validate.set_defaults(func=cmd_routing)

    routing_set_metered = routing_sub.add_parser(
        "set-metered", help="Set the metered fallback policy"
    )
    routing_set_metered.add_argument("value", choices=sorted(execution_routing.METERED_FALLBACK_VALUES))
    routing_set_metered.set_defaults(func=cmd_routing)

    routing_routes = routing_sub.add_parser("routes", help="Manage configured routes")
    routing_routes_sub = routing_routes.add_subparsers(
        dest="routes_action", required=True
    )

    routing_routes_list = routing_routes_sub.add_parser(
        "list", help="List configured routes in order"
    )
    routing_routes_list.set_defaults(func=cmd_routing)

    routing_routes_add = routing_routes_sub.add_parser("add", help="Add a route")
    routing_routes_add.add_argument("--id", required=True)
    routing_routes_add.add_argument(
        "--executor", required=True, choices=sorted(execution_routing.EXECUTORS)
    )
    routing_routes_add.add_argument("--model", required=True)
    routing_routes_add.add_argument(
        "--billing-class", required=True, choices=sorted(execution_routing.BILLING_CLASSES)
    )
    routing_routes_add.add_argument(
        "--account-pool", choices=pool_core.PROVIDERS, help="Ordered pool for a subscription route"
    )
    routing_routes_add.add_argument(
        "--account", help="Fixed pool account id for a metered route"
    )
    routing_routes_add.add_argument("--before", help="Insert before this route id")
    routing_routes_add.set_defaults(func=cmd_routing)

    routing_routes_remove = routing_routes_sub.add_parser("remove", help="Remove a route")
    routing_routes_remove.add_argument("id")
    routing_routes_remove.set_defaults(func=cmd_routing)

    routing_routes_order = routing_routes_sub.add_parser(
        "order", help="Reorder every configured route"
    )
    routing_routes_order.add_argument("ids", nargs="+")
    routing_routes_order.set_defaults(func=cmd_routing)

    routing_explain = routing_sub.add_parser(
        "explain", help="Safe selection trace for the current settings and pool"
    )
    routing_explain.add_argument("--workspace", help="Workspace id, echoed for traceability")
    routing_explain.add_argument("--model", help="Preferred model override")
    routing_explain.set_defaults(func=cmd_routing)

    pool = sub.add_parser(
        "pool",
        help="Order accounts the operator owns and select the first with quota",
    )
    pool_sub = pool.add_subparsers(dest="pool_action", required=True)

    pool_list = pool_sub.add_parser("list", help="Pool order, identity, and quota")
    pool_list.set_defaults(func=cmd_pool)

    pool_status = pool_sub.add_parser(
        "status", help="Show which account each provider would select now"
    )
    pool_status.set_defaults(func=cmd_pool)

    pool_which = pool_sub.add_parser("which", help="Print the selected account id")
    pool_which.add_argument("provider", choices=pool_core.PROVIDERS)
    pool_which.add_argument("--verify", action="store_true")
    pool_which.set_defaults(func=cmd_pool)

    pool_exec = pool_sub.add_parser(
        "exec",
        help="Run the provider CLI on the selected account",
        description=(
            "Every argument after the provider is passed to the provider CLI, so "
            "pool options must come first: sightmesh pool exec --no-verify claude -p ok"
        ),
    )
    pool_exec.add_argument("--no-verify", action="store_true")
    pool_exec.add_argument("provider", choices=pool_core.PROVIDERS)
    pool_exec.add_argument(
        "argv", nargs=argparse.REMAINDER, help="Arguments forwarded to the provider CLI"
    )
    pool_exec.set_defaults(func=cmd_pool)

    pool_order = pool_sub.add_parser("order", help="Show or set the fallback order")
    pool_order.add_argument("provider", choices=pool_core.PROVIDERS)
    pool_order.add_argument("ids", nargs="*")
    pool_order.set_defaults(func=cmd_pool)

    pool_promote = pool_sub.add_parser("promote", help="Move an account to the front")
    pool_promote.add_argument("name")
    pool_promote.set_defaults(func=cmd_pool)

    pool_quota = pool_sub.add_parser("quota", help="Live quota windows and resets")
    pool_quota.add_argument("names", nargs="*")
    pool_quota.add_argument("--refresh", action="store_true")
    pool_quota.set_defaults(func=cmd_pool)

    pool_verify = pool_sub.add_parser(
        "verify", help="Prove every account is distinct and usable"
    )
    pool_verify.set_defaults(func=cmd_pool)

    pool_cool = pool_sub.add_parser("cool", help="Mark an account exhausted")
    pool_cool.add_argument("name")
    pool_cool.add_argument("--for", dest="duration", default="5h")
    pool_cool.set_defaults(func=cmd_pool)

    pool_clear = pool_sub.add_parser("clear", help="Clear cooldowns")
    pool_clear.add_argument("name", nargs="?")
    pool_clear.add_argument("--all", action="store_true")
    pool_clear.set_defaults(func=cmd_pool)

    pool_remove = pool_sub.add_parser(
        "remove", help="Drop an account and its stored credential"
    )
    pool_remove.add_argument("name")
    pool_remove.set_defaults(func=cmd_pool)

    pool_add_claude = pool_sub.add_parser(
        "add-claude", help="Add a Claude account from `claude setup-token`"
    )
    pool_add_claude.add_argument("name")
    pool_add_claude.add_argument("--label")
    pool_add_claude.add_argument("--force", action="store_true")
    pool_add_claude.set_defaults(func=cmd_pool)

    pool_add_codex = pool_sub.add_parser(
        "add-codex", help="Add a Codex account with its own CODEX_HOME"
    )
    pool_add_codex.add_argument("name")
    pool_add_codex.add_argument("--mode", choices=["sub", "apikey"], required=True)
    pool_add_codex.add_argument("--home", help="Exact CODEX_HOME for this account")
    pool_add_codex.add_argument("--label")
    pool_add_codex.add_argument("--force", action="store_true")
    pool_add_codex.set_defaults(func=cmd_pool)

    pool_serve = pool_sub.add_parser("serve", help="Open the local pool web UI")
    pool_serve.add_argument("--port", type=int, default=7878)
    pool_serve.add_argument("--no-open", action="store_true")
    pool_serve.set_defaults(func=cmd_pool)

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
        "--allow-missing-repo",
        action="store_true",
        help="Delete remaining cdesktop history after its direct repository disappeared",
    )
    workspace_delete.add_argument(
        "--preserve-dirty",
        action="store_true",
        help="Delete cdesktop history while leaving a dirty direct repository untouched",
    )
    workspace_delete.set_defaults(func=cmd_workspace)

    managed = sub.add_parser("service", help="Manage the local cdesktop service")
    managed.add_argument(
        "action",
        choices=["install", "start", "stop", "status", "open", "cutover", "uninstall"],
    )
    managed.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    managed.add_argument("--no-start", action="store_true")
    managed.set_defaults(func=cmd_service)

    update = sub.add_parser(
        "update", help="Stage and safely activate versioned cdesktop releases"
    )
    update_sub = update.add_subparsers(dest="update_action", required=True)
    update_stage = update_sub.add_parser(
        "stage",
        help="Verify and install a release without interrupting active workers",
    )
    update_stage.add_argument(
        "--package", help="Local tgz or HTTPS URL; defaults to the runtime lock"
    )
    update_stage.add_argument("--version", help="Defaults to the runtime lock")
    update_stage.add_argument(
        "--sha256", help="Required SHA-256 digest for remote packages"
    )
    update_stage.add_argument(
        "--local-development",
        action="store_true",
        help="Explicitly allow an unverified local package override",
    )
    update_stage.set_defaults(func=cmd_update)
    update_activate = update_sub.add_parser(
        "activate",
        help="Activate now, or return safely while workers are busy",
    )
    update_activate.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    update_activate.set_defaults(func=cmd_update)
    update_status = update_sub.add_parser("status", help="Show staged update state")
    update_status.add_argument("--port", type=int, default=service.DEFAULT_PORT)
    update_status.set_defaults(func=cmd_update)
    update_cancel = update_sub.add_parser(
        "cancel", help="Cancel a staged update without removing its verified package"
    )
    update_cancel.set_defaults(func=cmd_update)
    update_prune = update_sub.add_parser(
        "prune", help="Remove superseded packages while retaining active staged paths"
    )
    update_prune.add_argument("--keep", type=int, default=1)
    update_prune.add_argument("--dry-run", action="store_true")
    update_prune.set_defaults(func=cmd_update)

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
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        code = args.func(args)
    except (
        CdesktopError,
        approvals.ApprovalAuditError,
        leases.LeaseError,
        RepowireError,
        ProfileError,
        PoolError,
        execution_routing.ExecutionRoutingError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        print(f"sightmesh: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
