from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import leases, routing
from .cdesktop import CdesktopClient
from .migration import git_info

SCHEMA_VERSION = 1
ACTIVE_SESSION_STATUSES = {"working", "running", "compacting", "starting"}


def default_state_root() -> Path:
    return Path.home() / ".local" / "state" / "sightmesh" / "migrations"


def default_conductor_root() -> Path:
    return Path.home() / "conductor"


def default_conductor_database() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "com.conductor.app"
        / "conductor.db"
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"Conductor database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return normalized[:80] or "workspace"


def _fingerprint(workspace: dict[str, Any]) -> str:
    fields = {
        "conductor_id": workspace.get("conductor_id"),
        "source_path": workspace.get("source_path"),
        "branch": workspace.get("git", {}).get("branch"),
        "head": workspace.get("git", {}).get("head"),
        "dirty_paths": workspace.get("git", {}).get("dirty_paths", []),
        "updated_at": workspace.get("updated_at"),
        "session_states": [
            (item.get("id"), item.get("status"), item.get("updated_at"))
            for item in workspace.get("sessions", [])
        ],
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _migration_git_info(path: Path) -> dict[str, Any]:
    info = git_info(path)
    info.pop("worktrees", None)
    return info


def _worktree_paths(root: Path) -> list[tuple[str, Path]]:
    parent = root / "workspaces"
    if not parent.is_dir():
        return []
    paths: list[tuple[str, Path]] = []
    for repository in sorted(parent.iterdir()):
        if not repository.is_dir():
            continue
        for workspace in sorted(repository.iterdir()):
            if workspace.is_dir() and (workspace / ".git").exists():
                paths.append((repository.name, workspace.resolve()))
    return paths


def _archive_paths(root: Path) -> list[tuple[str, Path]]:
    parent = root / "archived-contexts"
    if not parent.is_dir():
        return []
    paths: list[tuple[str, Path]] = []
    for repository in sorted(parent.iterdir()):
        if not repository.is_dir():
            continue
        for context in sorted(repository.iterdir()):
            if context.is_dir():
                paths.append((repository.name, context.resolve()))
    return paths


def _database_workspaces(database: Path, roots: list[Path]) -> list[dict[str, Any]]:
    with _connect_readonly(database) as connection:
        workspace_rows = connection.execute(
            """
            SELECT
              w.id AS conductor_id,
              COALESCE(w.workspace_name, w.DEPRECATED_city_name, w.directory_name)
                AS name,
              w.directory_name,
              w.branch,
              w.state,
              w.derived_status,
              w.workspace_path,
              w.updated_at,
              w.intended_target_branch,
              w.initialization_parent_branch,
              r.name AS repository_name,
              r.root_path AS repository_root,
              r.default_branch
            FROM workspaces w
            JOIN repos r ON r.id = w.repository_id
            ORDER BY w.updated_at DESC
            """
        ).fetchall()
        session_rows = connection.execute(
            """
            SELECT id, workspace_id, status, title, agent_type, model,
                   permission_mode, context_used_percent, updated_at, is_compacting
            FROM sessions
            ORDER BY created_at
            """
        ).fetchall()

    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in session_rows:
        item = dict(row)
        sessions[str(item.pop("workspace_id"))].append(item)

    archive_lookup = {
        (repository, path.name): path
        for root in roots
        for repository, path in _archive_paths(root)
    }
    worktree_lookup = {
        (repository, path.name): path
        for root in roots
        for repository, path in _worktree_paths(root)
    }
    items: list[dict[str, Any]] = []
    for row in workspace_rows:
        data = dict(row)
        key = (str(data["repository_name"]), str(data["directory_name"]))
        configured = (
            Path(str(data["workspace_path"])).expanduser()
            if data.get("workspace_path")
            else None
        )
        checkout = (
            configured.resolve()
            if configured and configured.exists()
            else worktree_lookup.get(key)
        )
        archive = archive_lookup.get(key)
        source = checkout or archive
        info = _migration_git_info(source) if source else {"is_git": False}
        session_items = sessions.get(str(data["conductor_id"]), [])
        active_sessions = [
            item["id"]
            for item in session_items
            if str(item.get("status") or "").lower() in ACTIVE_SESSION_STATUSES
            or bool(item.get("is_compacting"))
        ]
        target_branch = (
            data.get("intended_target_branch")
            or data.get("initialization_parent_branch")
            or data.get("default_branch")
            or "main"
        )
        warnings: list[str] = []
        blockers: list[str] = []
        if not source and data["state"] == "archived":
            warnings.append(
                "no retained checkout or context directory; transcript handoff only"
            )
        elif not source:
            blockers.append("source checkout and archived context are both missing")
        if active_sessions:
            blockers.append("Conductor session is active")
        if info.get("dirty_paths"):
            warnings.append(
                "dirty Git state requires an explicit checkpoint confirmation"
            )
        item = {
            "conductor_id": data["conductor_id"],
            "name": data["name"] or data["directory_name"],
            "directory_name": data["directory_name"],
            "repository_name": data["repository_name"],
            "repository_root": data["repository_root"],
            "source_path": str(source) if source else None,
            "checkout_path": str(checkout) if checkout else None,
            "archived_context_path": str(archive) if archive else None,
            "context_path": str(checkout / ".context")
            if checkout and (checkout / ".context").exists()
            else (str(archive) if archive else None),
            "state": data["state"],
            "derived_status": data["derived_status"],
            "updated_at": data["updated_at"],
            "target_branch": target_branch,
            "git": info,
            "sessions": session_items,
            "active_session_ids": active_sessions,
            "blockers": blockers,
            "warnings": warnings,
            "kind": "conductor-record",
        }
        item["fingerprint"] = _fingerprint(item)
        items.append(item)
    return items


def _add_orphan_sources(
    items: list[dict[str, Any]], roots: list[Path]
) -> list[dict[str, Any]]:
    known = {
        Path(item["source_path"]).resolve() for item in items if item.get("source_path")
    }
    for root in roots:
        for repository, path in _worktree_paths(root):
            if path in known:
                continue
            info = _migration_git_info(path)
            item = {
                "conductor_id": f"filesystem-{hashlib.sha256(str(path).encode()).hexdigest()[:20]}",
                "name": path.name,
                "directory_name": path.name,
                "repository_name": repository,
                "repository_root": info.get("top_level"),
                "source_path": str(path),
                "checkout_path": str(path),
                "archived_context_path": None,
                "context_path": str(path / ".context")
                if (path / ".context").exists()
                else None,
                "state": "orphaned-checkout",
                "derived_status": None,
                "updated_at": None,
                "target_branch": "main",
                "git": info,
                "sessions": [],
                "active_session_ids": [],
                "blockers": [],
                "warnings": [
                    "checkout is not linked to a current Conductor database record"
                ],
                "kind": "orphaned-checkout",
            }
            if info.get("dirty_paths"):
                item["warnings"].append(
                    "dirty Git state requires an explicit checkpoint confirmation"
                )
            item["fingerprint"] = _fingerprint(item)
            items.append(item)
            known.add(path)
        for repository, path in _archive_paths(root):
            if path in known:
                continue
            item = {
                "conductor_id": f"archive-{hashlib.sha256(str(path).encode()).hexdigest()[:20]}",
                "name": path.name,
                "directory_name": path.name,
                "repository_name": repository,
                "repository_root": None,
                "source_path": str(path),
                "checkout_path": None,
                "archived_context_path": str(path),
                "context_path": str(path),
                "state": "archived",
                "derived_status": None,
                "updated_at": None,
                "target_branch": "",
                "git": {"is_git": False},
                "sessions": [],
                "active_session_ids": [],
                "blockers": [],
                "warnings": ["context-only archive; no Git checkout is attached"],
                "kind": "orphaned-archive",
            }
            item["fingerprint"] = _fingerprint(item)
            items.append(item)
            known.add(path)
    return items


def build_plan(
    *,
    conductor_roots: Iterable[str | Path] | None = None,
    database: str | Path | None = None,
) -> dict[str, Any]:
    roots = [
        Path(item).expanduser().resolve()
        for item in (conductor_roots or [default_conductor_root()])
    ]
    roots = [item for item in roots if item.exists()]
    if not roots:
        raise ValueError("No Conductor root exists")
    database_path = (
        Path(database or default_conductor_database()).expanduser().resolve()
    )
    items = _database_workspaces(database_path, roots)
    items = _add_orphan_sources(items, roots)
    items.sort(
        key=lambda item: (item["repository_name"], item["name"], item["conductor_id"])
    )
    now = time.time()
    run_id = (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + f"-{uuid.uuid4().hex[:8]}"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "conductor_roots": [str(item) for item in roots],
        "conductor_database": str(database_path),
        "mode": "adopt-in-place",
        "source_is_never_deleted": True,
        "workspace_count": len(items),
        "workspaces": items,
    }


def write_plan(plan: dict[str, Any], output: str | Path | None = None) -> Path:
    target = (
        Path(output).expanduser().resolve()
        if output
        else default_state_root() / str(plan["run_id"]) / "plan.json"
    )
    _atomic_json(target, plan)
    return target


def load_plan(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read migration plan {source}: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported migration plan schema")
    data["_plan_path"] = str(source)
    return data


def _extract_text(content: str, role: str) -> str | None:
    stripped = content.strip()
    if not stripped:
        return None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped if role in {"user", "assistant"} else None
    if not isinstance(value, dict):
        return None
    if value.get("type") == "result" and isinstance(value.get("result"), str):
        return value["result"].strip() or None
    message = value.get("message")
    if not isinstance(message, dict):
        return None
    blocks = message.get("content")
    if isinstance(blocks, str):
        return blocks.strip() or None
    if not isinstance(blocks, list):
        return None
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return "\n\n".join(texts) or None


def _session_context(
    connection: sqlite3.Connection, session: dict[str, Any], semantic_limit: int
) -> dict[str, Any]:
    count_row = connection.execute(
        "SELECT COUNT(*) AS count, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
        "FROM session_messages WHERE session_id = ?",
        (session["id"],),
    ).fetchone()
    rows = connection.execute(
        """
        SELECT role, content, created_at
        FROM session_messages
        WHERE session_id = ? AND cancelled_at IS NULL AND content IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 5000
        """,
        (session["id"],),
    )
    semantic: deque[dict[str, str]] = deque(maxlen=semantic_limit)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        text = _extract_text(str(row["content"]), str(row["role"]))
        if not text:
            continue
        text = text[:12000]
        key = (str(row["role"]), text)
        if key in seen:
            continue
        seen.add(key)
        semantic.appendleft(
            {
                "role": str(row["role"]),
                "created_at": str(row["created_at"]),
                "text": text,
            }
        )
        if len(semantic) >= semantic_limit:
            break
    return {
        **session,
        "message_count": int(count_row["count"] or 0),
        "first_message_at": count_row["first_at"],
        "last_message_at": count_row["last_at"],
        "recent_semantic_messages": list(semantic),
    }


def _write_context_bundle(
    workspace: dict[str, Any], database: Path, run_dir: Path, semantic_limit: int
) -> dict[str, str]:
    context_dir = (
        run_dir
        / "contexts"
        / (
            f"{_slug(workspace['repository_name'])}-{_slug(workspace['name'])}-"
            f"{str(workspace['conductor_id'])[:8]}"
        )
    )
    session_contexts: list[dict[str, Any]] = []
    if workspace.get("sessions"):
        with _connect_readonly(database) as connection:
            session_contexts = [
                _session_context(connection, session, semantic_limit)
                for session in workspace["sessions"]
            ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "conductor_id": workspace["conductor_id"],
        "name": workspace["name"],
        "repository_name": workspace["repository_name"],
        "source_path": workspace["source_path"],
        "context_path": workspace.get("context_path"),
        "branch": workspace.get("git", {}).get("branch"),
        "head": workspace.get("git", {}).get("head"),
        "dirty_paths": workspace.get("git", {}).get("dirty_paths", []),
        "conductor_database": str(database),
        "sessions": session_contexts,
    }
    manifest_path = context_dir / "manifest.json"
    handoff_path = context_dir / "handoff.md"
    lines = [
        f"# Conductor migration handoff: {workspace['name']}",
        "",
        f"- Source: `{workspace['source_path']}`",
        f"- Repository: `{workspace['repository_name']}`",
        f"- Branch: `{manifest['branch']}`",
        f"- HEAD: `{manifest['head']}`",
        f"- Original context: `{workspace.get('context_path')}`",
        f"- Conductor database: `{database}`",
        "",
        "The source checkout and original Conductor database remain authoritative and were not deleted.",
    ]
    for session in session_contexts:
        lines.extend(
            [
                "",
                f"## Session: {session.get('title') or session['id']}",
                "",
                f"- ID: `{session['id']}`",
                f"- Agent: `{session.get('agent_type')}`",
                f"- Model: `{session.get('model')}`",
                f"- Original message rows: {session['message_count']}",
            ]
        )
        for message in session["recent_semantic_messages"]:
            lines.extend(
                [
                    "",
                    f"### {message['role']} at {message['created_at']}",
                    "",
                    message["text"],
                ]
            )
    _atomic_json(manifest_path, manifest)
    _atomic_text(handoff_path, "\n".join(lines).rstrip() + "\n")
    return {"manifest": str(manifest_path), "handoff": str(handoff_path)}


def _install_context_pointer(
    workspace: dict[str, Any], run_id: str, bundle: dict[str, str]
) -> str | None:
    checkout = workspace.get("checkout_path")
    if not checkout:
        return None
    checkout_path = Path(checkout)
    context_dir = checkout_path / ".context"
    if not context_dir.is_dir():
        return None
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", ".context"],
        cwd=checkout_path,
        capture_output=True,
        check=False,
    )
    if ignored.returncode != 0:
        return None
    pointer = context_dir / "sightmesh-migration.json"
    _atomic_json(pointer, {"run_id": run_id, **bundle})
    return str(pointer)


def _existing_cdesktop_by_path(client: CdesktopClient) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for workspace in client.workspaces():
        for repo in client.workspace_repos(workspace["id"]):
            path = repo.get("path")
            if path:
                result[str(Path(path).expanduser().resolve())] = workspace
    return result


def _currently_active_sessions(database: Path, conductor_id: str) -> list[str]:
    if conductor_id.startswith(("filesystem-", "archive-")):
        return []
    with _connect_readonly(database) as connection:
        rows = connection.execute(
            "SELECT id, status, is_compacting FROM sessions WHERE workspace_id = ?",
            (conductor_id,),
        ).fetchall()
    return [
        str(row["id"])
        for row in rows
        if str(row["status"] or "").lower() in ACTIVE_SESSION_STATUSES
        or bool(row["is_compacting"])
    ]


def _selection(
    plan: dict[str, Any], names: Iterable[str], include_archived: bool
) -> list[dict[str, Any]]:
    requested = set(names)
    items = []
    for item in plan["workspaces"]:
        selected = (
            not requested
            or item["name"] in requested
            or item["conductor_id"] in requested
        )
        if not selected:
            continue
        is_archived = (
            item["state"] in {"archived", "orphaned-checkout"}
            or item["kind"] == "orphaned-archive"
        )
        if is_archived and not include_archived:
            continue
        items.append(item)
    if requested:
        found = {item["name"] for item in items} | {
            item["conductor_id"] for item in items
        }
        missing = sorted(requested - found)
        if missing:
            raise ValueError(
                f"Migration selection was not found or requires --include-archived: {', '.join(missing)}"
            )
    return items


def apply_plan(
    plan_path: str | Path,
    *,
    names: Iterable[str] = (),
    include_archived: bool = False,
    include_dirty: bool = False,
    confirm_conductor_paused: bool = False,
    confirm_checkpointed: bool = False,
    semantic_limit: int = 20,
    client: CdesktopClient | None = None,
    lease_store: leases.LeaseStore | None = None,
) -> dict[str, Any]:
    if not confirm_conductor_paused:
        raise ValueError("Apply requires --confirm-conductor-paused")
    if include_dirty and not confirm_checkpointed:
        raise ValueError("--include-dirty requires --confirm-checkpointed")
    if semantic_limit <= 0:
        raise ValueError("--semantic-messages must be positive")
    plan = load_plan(plan_path)
    source = Path(plan["_plan_path"])
    run_dir = source.parent
    run_path = run_dir / "run.json"
    if run_path.exists():
        run = json.loads(run_path.read_text(encoding="utf-8"))
    else:
        run = {
            "schema_version": SCHEMA_VERSION,
            "run_id": plan["run_id"],
            "plan": str(source),
            "applications": {},
            "created_at": time.time(),
        }
    selected = _selection(plan, names, include_archived)
    if not selected:
        raise ValueError("No workspaces matched the migration selection")
    client = client or CdesktopClient()
    lease_store = lease_store or leases.LeaseStore()
    existing = _existing_cdesktop_by_path(client)
    database = Path(plan["conductor_database"])

    for workspace in selected:
        if workspace["blockers"]:
            raise ValueError(f"{workspace['name']}: {', '.join(workspace['blockers'])}")
        if _currently_active_sessions(database, str(workspace["conductor_id"])):
            raise ValueError(
                f"{workspace['name']}: Conductor session became active after planning"
            )
        source_path = workspace.get("source_path")
        current = (
            _migration_git_info(Path(source_path)) if source_path else {"is_git": False}
        )
        if source_path and (
            current.get("head") != workspace.get("git", {}).get("head")
            or current.get("dirty_paths", [])
            != workspace.get("git", {}).get("dirty_paths", [])
        ):
            raise ValueError(
                f"{workspace['name']}: source changed after the plan was created; create a new plan"
            )
        if current.get("dirty_paths") and not include_dirty:
            raise ValueError(
                f"{workspace['name']}: dirty Git state requires "
                "--include-dirty --confirm-checkpointed"
            )

    for workspace in selected:
        key = str(workspace["conductor_id"])
        prior = run["applications"].get(key)
        if prior and prior.get("status") in {"created", "reused"}:
            continue
        source_path = workspace.get("source_path")

        bundle = _write_context_bundle(workspace, database, run_dir, semantic_limit)
        pointer = _install_context_pointer(workspace, str(plan["run_id"]), bundle)
        resolved_source = str(
            Path(source_path).resolve()
            if source_path
            else Path(bundle["handoff"]).resolve().parent
        )
        matched = existing.get(resolved_source)
        if matched:
            application = {
                "status": "reused",
                "workspace_id": matched["id"],
                "source_path": resolved_source,
                "context_bundle": bundle,
                "context_pointer": pointer,
                "source_archived": matched.get("archived"),
            }
        else:
            cdesktop_name = (
                f"migrated-{workspace['repository_name']}-{workspace['name']}"
            )
            created = client.create_workspace_record(cdesktop_name, use_worktree=False)
            workspace_id = str(created["id"])
            try:
                client.add_workspace_repo(
                    workspace_id,
                    Path(resolved_source),
                    str(workspace.get("target_branch") or ""),
                    f"{workspace['repository_name']}:{workspace['name']}",
                )
                source_archived = (
                    workspace["state"] in {"archived", "orphaned-checkout"}
                    or workspace["kind"] == "orphaned-archive"
                )
                lease = None
                if source_archived:
                    client.archive_workspace(workspace_id)
                else:
                    lease = lease_store.acquire(
                        f"conductor-migration:{plan['run_id']}",
                        resolved_source,
                        workspace_id=workspace_id,
                    )
                application = {
                    "status": "created",
                    "workspace_id": workspace_id,
                    "source_path": resolved_source,
                    "context_bundle": bundle,
                    "context_pointer": pointer,
                    "source_archived": source_archived,
                    "lease": lease.to_dict() if lease else None,
                }
                existing[resolved_source] = created
            except Exception:
                client.archive_workspace(workspace_id)
                raise
        run["applications"][key] = application
        run["updated_at"] = time.time()
        _atomic_json(run_path, run)

    return {**run, "run_path": str(run_path)}


def migration_status(run_path: str | Path) -> dict[str, Any]:
    source = Path(run_path).expanduser().resolve()
    if source.name == "plan.json":
        source = source.with_name("run.json")
    if not source.is_file():
        raise ValueError(f"Migration run does not exist: {source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    counts: dict[str, int] = defaultdict(int)
    for item in data.get("applications", {}).values():
        counts[str(item.get("status") or "unknown")] += 1
    return {**data, "counts": dict(counts), "run_path": str(source)}


def rollback_run(
    run_path: str | Path,
    *,
    confirm: bool,
    client: CdesktopClient | None = None,
    lease_store: leases.LeaseStore | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("Rollback requires --confirm")
    run = migration_status(run_path)
    source = Path(run["run_path"])
    client = client or CdesktopClient()
    lease_store = lease_store or leases.LeaseStore()
    for application in run.get("applications", {}).values():
        if application.get("status") != "created":
            continue
        workspace_id = str(application["workspace_id"])
        if client.sessions(workspace_id):
            raise ValueError(
                f"Workspace {workspace_id} has sessions; reconcile it before rollback"
            )
        client.archive_workspace(workspace_id)
        routing.disable(workspace_id)
        lease_store.release_workspace_if_present(workspace_id)
        application["status"] = "rolled-back"
        application["rolled_back_at"] = time.time()
        run["updated_at"] = time.time()
        persisted = {
            key: value
            for key, value in run.items()
            if key not in {"counts", "run_path"}
        }
        _atomic_json(source, persisted)
    return migration_status(source)
