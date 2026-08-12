#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import subprocess
import tempfile
from pathlib import Path

from sightmesh.cdesktop import CdesktopClient
from sightmesh.conductor_migrate import apply_plan, build_plan, rollback_run, write_plan
from sightmesh.leases import LeaseStore


def run_git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sightmesh-migration-smoke-") as raw:
        root = Path(raw)
        conductor = root / "conductor"
        checkout = conductor / "workspaces" / "smoke-repo" / "smoke-workspace"
        checkout.mkdir(parents=True)
        run_git(checkout, "init", "-b", "main")
        run_git(
            checkout,
            "-c",
            "user.name=SightMesh",
            "-c",
            "user.email=sightmesh@localhost",
            "commit",
            "--allow-empty",
            "-m",
            "migration smoke",
        )

        database = root / "conductor.db"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE repos (
                  id TEXT PRIMARY KEY, name TEXT, root_path TEXT, default_branch TEXT
                );
                CREATE TABLE workspaces (
                  id TEXT PRIMARY KEY, workspace_name TEXT, DEPRECATED_city_name TEXT,
                  directory_name TEXT, branch TEXT, state TEXT, derived_status TEXT,
                  workspace_path TEXT, updated_at TEXT, intended_target_branch TEXT,
                  initialization_parent_branch TEXT, repository_id TEXT
                );
                CREATE TABLE sessions (
                  id TEXT PRIMARY KEY, workspace_id TEXT, status TEXT, title TEXT,
                  agent_type TEXT, model TEXT, permission_mode TEXT,
                  context_used_percent REAL, updated_at TEXT, created_at TEXT,
                  is_compacting INTEGER
                );
                CREATE TABLE session_messages (
                  id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                  created_at TEXT, sent_at TEXT, cancelled_at TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO repos VALUES (?, ?, ?, ?)",
                ("repo", "smoke-repo", str(checkout), "main"),
            )
            connection.execute(
                "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "conductor-smoke",
                    "smoke-workspace",
                    None,
                    "smoke-workspace",
                    "main",
                    "ready",
                    "done",
                    str(checkout),
                    "2026-08-12T00:00:00Z",
                    "main",
                    "main",
                    "repo",
                ),
            )
            connection.commit()

        plan_path = write_plan(
            build_plan(conductor_roots=[conductor], database=database),
            root / "run" / "plan.json",
        )
        client = CdesktopClient()
        lease_store = LeaseStore(root / "leases")
        applied = apply_plan(
            plan_path,
            names=["smoke-workspace"],
            confirm_conductor_paused=True,
            client=client,
            lease_store=lease_store,
        )
        application = applied["applications"]["conductor-smoke"]
        workspace_id = application["workspace_id"]
        assert application["status"] == "created"
        assert client.sessions(workspace_id) == []
        assert client.workspace(workspace_id)["use_worktree"] is False
        assert Path(application["context_bundle"]["handoff"]).is_file()

        rolled_back = rollback_run(
            plan_path,
            confirm=True,
            client=client,
            lease_store=lease_store,
        )
        assert rolled_back["applications"]["conductor-smoke"]["status"] == "rolled-back"
        print(
            f"migration-smoke: created and rolled back {workspace_id} without a session"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
