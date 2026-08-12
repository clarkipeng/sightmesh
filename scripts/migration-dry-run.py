#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


SECRET_WORDS = ("token", "secret", "password", "authorization", "cookie", "credential", "key")
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".cache",
}


def run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False, timeout=20)


def parse_porcelain_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        paths.append(path)
    return paths


def git_info(path: Path) -> dict[str, Any]:
    result = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return {"is_git": False}
    branch = run(["git", "-C", str(path), "branch", "--show-current"]).stdout.strip()
    head = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    status = run(["git", "-C", str(path), "status", "--porcelain=v1"]).stdout
    worktrees = run(["git", "-C", str(path), "worktree", "list", "--porcelain"]).stdout
    return {
        "is_git": True,
        "top_level": result.stdout.strip(),
        "branch": branch or None,
        "head": head or None,
        "dirty_paths": parse_porcelain_paths(status),
        "worktrees": parse_worktrees(worktrees),
    }


def parse_worktrees(output: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        rows.append(current)
    return rows


def safe_metadata_files(path: Path) -> list[str]:
    roots = [path / ".context", path / ".conductor", path / ".cdesktop"]
    files: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for item in root.rglob("*"):
            if item.is_file() and not any(word in item.name.lower() for word in SECRET_WORDS):
                files.append(str(item.relative_to(path)))
    return sorted(files)


def bounded_files(root: Path, suffixes: tuple[str, ...], max_depth: int) -> list[Path]:
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name in SKIP_DIRS:
                    continue
                stack.append((child, depth + 1))
            elif child.suffix in suffixes:
                found.append(child)
    return found


def sqlite_inventory(path: Path) -> dict[str, Any]:
    if not path.exists() or any(word in path.name.lower() for word in SECRET_WORDS):
        return {}
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return {"path": str(path), "error": str(exc)}
    with conn:
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                )
            ]
        except sqlite3.Error as exc:
            return {"path": str(path), "error": str(exc)}
        counts: dict[str, int | str] = {}
        for table in tables:
            if any(word in table.lower() for word in SECRET_WORDS):
                counts[table] = "redacted-name"
                continue
            try:
                quoted = table.replace('"', '""')
                counts[table] = int(conn.execute(f'select count(*) from "{quoted}"').fetchone()[0])
            except sqlite3.Error as exc:
                counts[table] = f"error: {exc}"
    return {"path": str(path), "tables": counts}


def conductor_roots(explicit: list[str]) -> list[Path]:
    candidates = [Path(item).expanduser() for item in explicit]
    candidates.extend(
        [
            Path.home() / "conductor",
            Path.home() / ".conductor",
            Path.home() / ".local" / "share" / "conductor",
        ]
    )
    seen: set[Path] = set()
    roots: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved.exists() and resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


def inventory_workspace(path: Path) -> dict[str, Any]:
    info = git_info(path)
    blockers: list[str] = []
    if info.get("dirty_paths"):
        blockers.append("dirty git state")
    if not info.get("is_git"):
        blockers.append("not a git checkout")
    return {
        "path": str(path),
        "name": path.name,
        "git": info,
        "metadata_files": safe_metadata_files(path),
        "proposed_cdesktop_name": f"migrated-{path.name}",
        "blockers": blockers,
    }


def inventory(args: argparse.Namespace) -> dict[str, Any]:
    roots = conductor_roots(args.conductor_root)
    workspace_paths: list[Path] = []
    for root in roots:
        workspaces = root / "workspaces"
        if workspaces.exists():
            for org in workspaces.iterdir():
                if not org.is_dir():
                    continue
                candidates = [org]
                candidates.extend([child for child in org.iterdir() if child.is_dir()])
                workspace_paths.extend([item for item in candidates if (item / ".git").exists()])
    sqlite_paths: list[Path] = []
    for root in roots:
        sqlite_paths.extend(bounded_files(root, (".sqlite", ".db"), max_depth=4))
    return {
        "dry_run": True,
        "read_only": True,
        "conductor_roots": [str(path) for path in roots],
        "workspace_count": len(workspace_paths),
        "workspaces": [inventory_workspace(path) for path in sorted(set(workspace_paths))],
        "sqlite": [sqlite_inventory(path) for path in sorted(set(sqlite_paths))],
        "environment": {
            "cwd": os.getcwd(),
            "user": os.environ.get("USER"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Conductor to cdesktop migration dry-run")
    parser.add_argument("--conductor-root", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = inventory(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Conductor roots: {', '.join(result['conductor_roots']) or '(none)'}")
        print(f"Workspaces: {result['workspace_count']}")
        for workspace in result["workspaces"]:
            blockers = ", ".join(workspace["blockers"]) or "none"
            print(f"- {workspace['name']}: {workspace['git'].get('branch')} blockers={blockers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
