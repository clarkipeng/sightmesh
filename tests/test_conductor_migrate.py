from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sightmesh import cli
from sightmesh.conductor_migrate import (
    apply_plan,
    build_plan,
    migration_status,
    rollback_run,
    write_plan,
)
from sightmesh.leases import LeaseStore


class FakeMigrationClient:
    def __init__(self) -> None:
        self.workspace_rows: list[dict] = []
        self.workspace_repo_rows: dict[str, list[dict]] = {}
        self.archived: list[str] = []
        self.created = 0

    def workspaces(self):
        return self.workspace_rows

    def workspace_repos(self, workspace_id):
        return self.workspace_repo_rows.get(workspace_id, [])

    def create_workspace_record(self, name, *, use_worktree):
        assert use_worktree is False
        self.created += 1
        row = {
            "id": f"workspace-{self.created}",
            "name": name,
            "archived": False,
            "use_worktree": False,
        }
        self.workspace_rows.append(row)
        return row

    def add_workspace_repo(
        self, workspace_id, repo_path, target_branch, display_name=None
    ):
        self.workspace_repo_rows[workspace_id] = [
            {
                "path": str(Path(repo_path).resolve()),
                "target_branch": target_branch,
                "display_name": display_name,
            }
        ]
        return {"workspace": {"id": workspace_id}}

    def archive_workspace(self, workspace_id):
        self.archived.append(workspace_id)
        for row in self.workspace_rows:
            if row["id"] == workspace_id:
                row["archived"] = True
                return row
        return {"id": workspace_id, "archived": True}

    def sessions(self, workspace_id):
        return []


def _git(command: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *command], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path, *, dirty: bool = False, status: str = "idle"):
    conductor = tmp_path / "conductor"
    checkout = conductor / "workspaces" / "project" / "alpha"
    checkout.mkdir(parents=True)
    _git(["init", "-b", "main"], checkout)
    (checkout / ".gitignore").write_text(".context/\n", encoding="utf-8")
    (checkout / "tracked.txt").write_text("original\n", encoding="utf-8")
    (checkout / ".context").mkdir()
    (checkout / ".context" / "notes.md").write_text(
        "durable context\n", encoding="utf-8"
    )
    _git(["add", ".gitignore", "tracked.txt"], checkout)
    _git(
        [
            "-c",
            "user.name=SightMesh",
            "-c",
            "user.email=sightmesh@localhost",
            "commit",
            "-m",
            "fixture",
        ],
        checkout,
    )
    if dirty:
        (checkout / "tracked.txt").write_text("changed\n", encoding="utf-8")

    database = tmp_path / "conductor.db"
    connection = sqlite3.connect(database)
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
        ("repo", "project", str(tmp_path / "project"), "main"),
    )
    connection.execute(
        "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "conductor-alpha",
            "alpha",
            None,
            "alpha",
            "feature/alpha",
            "ready",
            "in-progress",
            str(checkout),
            "2026-08-12T00:00:00Z",
            "main",
            "main",
            "repo",
        ),
    )
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "session-alpha",
            "conductor-alpha",
            status,
            "Implement alpha",
            "codex",
            "gpt-5",
            "default",
            20.0,
            "2026-08-12T00:00:00Z",
            "2026-08-12T00:00:00Z",
            0,
        ),
    )
    connection.execute(
        "INSERT INTO session_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "message-alpha",
            "session-alpha",
            "user",
            "Preserve this migration context",
            "2026-08-12T00:00:01Z",
            "2026-08-12T00:00:01Z",
            None,
        ),
    )
    connection.commit()
    connection.close()
    return conductor, checkout, database


def test_plan_apply_status_and_rollback(tmp_path: Path, capsys) -> None:
    conductor, checkout, database = _fixture(tmp_path)
    plan = build_plan(conductor_roots=[conductor], database=database)
    assert plan["workspace_count"] == 1
    assert plan["workspaces"][0]["blockers"] == []
    plan_path = write_plan(plan, tmp_path / "run" / "plan.json")
    client = FakeMigrationClient()
    leases = LeaseStore(tmp_path / "leases")

    result = apply_plan(
        plan_path,
        names=["alpha"],
        confirm_conductor_paused=True,
        client=client,
        lease_store=leases,
    )

    application = result["applications"]["conductor-alpha"]
    capability_token = leases.workspace_token("workspace-1")
    assert capability_token
    assert "token" not in application["lease"]
    assert capability_token not in json.dumps(result)
    assert application["status"] == "created"
    assert client.workspace_rows[0]["name"] == "alpha"
    assert client.workspace_rows[0]["use_worktree"] is False
    assert client.workspace_repo_rows["workspace-1"][0]["path"] == str(checkout)
    pointer = json.loads(
        (checkout / ".context" / "sightmesh-migration.json").read_text()
    )
    assert Path(pointer["handoff"]).is_file()
    assert "Preserve this migration context" in Path(pointer["handoff"]).read_text()
    persisted = json.loads((plan_path.parent / "run.json").read_text(encoding="utf-8"))
    assert capability_token not in json.dumps(persisted)
    status = migration_status(plan_path)
    assert status["counts"] == {"created": 1}
    assert capability_token not in json.dumps(status)

    status_args = argparse.Namespace(
        migrate_action="status", run=str(plan_path), json=True
    )
    assert cli.cmd_migrate(status_args) == 0
    assert capability_token not in capsys.readouterr().out

    rolled_back = rollback_run(
        plan_path, confirm=True, client=client, lease_store=leases
    )
    assert rolled_back["counts"] == {"rolled-back": 1}
    assert leases.workspace_token("workspace-1") is None


def test_apply_dirty_requires_two_explicit_confirmations(tmp_path: Path) -> None:
    conductor, _, database = _fixture(tmp_path, dirty=True)
    plan_path = write_plan(
        build_plan(conductor_roots=[conductor], database=database),
        tmp_path / "run" / "plan.json",
    )
    with pytest.raises(ValueError, match="dirty Git state"):
        apply_plan(
            plan_path,
            names=["alpha"],
            confirm_conductor_paused=True,
            client=FakeMigrationClient(),
            lease_store=LeaseStore(tmp_path / "leases"),
        )
    with pytest.raises(ValueError, match="confirm-checkpointed"):
        apply_plan(
            plan_path,
            names=["alpha"],
            include_dirty=True,
            confirm_conductor_paused=True,
            client=FakeMigrationClient(),
            lease_store=LeaseStore(tmp_path / "leases"),
        )


def test_plan_blocks_active_conductor_session(tmp_path: Path) -> None:
    conductor, _, database = _fixture(tmp_path, status="working")
    plan = build_plan(conductor_roots=[conductor], database=database)
    assert plan["workspaces"][0]["blockers"] == ["Conductor session is active"]
    plan_path = write_plan(plan, tmp_path / "run" / "plan.json")
    client = FakeMigrationClient()
    with pytest.raises(ValueError, match="Conductor session is active"):
        apply_plan(
            plan_path,
            names=["alpha"],
            confirm_conductor_paused=True,
            client=client,
            lease_store=LeaseStore(tmp_path / "leases"),
        )
    assert client.created == 0


def test_archived_record_without_files_uses_private_handoff(tmp_path: Path) -> None:
    conductor, checkout, database = _fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE workspaces SET state = 'archived', workspace_path = ?",
            (str(tmp_path / "missing"),),
        )
        connection.commit()
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(checkout)],
        cwd=checkout,
        check=False,
        capture_output=True,
    )
    # The fixture is a standalone repository, so remove its files through its
    # temporary-directory owner after making the path undiscoverable.
    moved = tmp_path / "outside-conductor"
    checkout.rename(moved)

    plan = build_plan(conductor_roots=[conductor], database=database)
    workspace = plan["workspaces"][0]
    assert workspace["source_path"] is None
    assert workspace["blockers"] == []
    plan_path = write_plan(plan, tmp_path / "run" / "plan.json")
    client = FakeMigrationClient()
    result = apply_plan(
        plan_path,
        names=["alpha"],
        include_archived=True,
        confirm_conductor_paused=True,
        client=client,
        lease_store=LeaseStore(tmp_path / "leases"),
    )
    application = result["applications"]["conductor-alpha"]
    assert application["status"] == "cataloged"
    assert application["workspace_id"] is None
    assert client.created == 0
    handoff = Path(application["context_bundle"]["handoff"])
    assert "Preserve this migration context" in handoff.read_text()


def test_archived_record_requires_explicit_materialization(tmp_path: Path) -> None:
    conductor, _, database = _fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE workspaces SET state = 'archived'")
        connection.commit()
    plan_path = write_plan(
        build_plan(conductor_roots=[conductor], database=database),
        tmp_path / "run" / "plan.json",
    )
    client = FakeMigrationClient()
    result = apply_plan(
        plan_path,
        names=["alpha"],
        include_archived=True,
        materialize_archived=True,
        confirm_conductor_paused=True,
        client=client,
        lease_store=LeaseStore(tmp_path / "leases"),
    )
    application = result["applications"]["conductor-alpha"]
    assert application["status"] == "created"
    assert client.archived == ["workspace-1"]
