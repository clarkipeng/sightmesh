import argparse
import subprocess
from pathlib import Path

import pytest

from sightmesh import cli, delivery
from sightmesh.cli import _primary_session_id, _read_text, parser
from sightmesh.delivery import DeliveryStore, make_record
from sightmesh.leases import LeaseStore
from sightmesh.profiles import Profile, ProfileStore


def test_read_text_requires_one_source(tmp_path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("from file", encoding="utf-8")
    assert _read_text(None, str(prompt), "prompt") == "from file"
    assert _read_text("inline", None, "prompt") == "inline"


def test_namespace_import_is_available() -> None:
    assert argparse.Namespace is not None


def test_primary_session_id_reads_execution_process() -> None:
    assert (
        _primary_session_id({"execution_process": {"session_id": "session-a"}})
        == "session-a"
    )


def test_delivery_status_and_list_commands(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "delivery.sqlite3"
    monkeypatch.setattr(delivery, "delivery_db_path", lambda: path)
    DeliveryStore(path).enqueue(
        make_record(
            session_id="session-1",
            message_type="ask",
            prompt="prompt",
            delivery_id="delivery-1",
            correlation_id="correlation-1",
            from_peer="sender",
            text="text",
        )
    )

    args = parser().parse_args(["--json", "delivery", "status"])
    assert args.func(args) == 0
    assert '"pending"' in capsys.readouterr().out

    args = parser().parse_args(["--json", "delivery", "list", "--status", "pending"])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "delivery-1" in output
    assert '"prompt":' not in output


def test_delivery_retry_and_purge_require_exact_keys(
    monkeypatch, tmp_path, capsys
) -> None:
    path = tmp_path / "delivery.sqlite3"
    monkeypatch.setattr(delivery, "delivery_db_path", lambda: path)
    store = DeliveryStore(path)
    record = store.enqueue(
        make_record(
            session_id="session-1",
            message_type="ask",
            prompt="prompt",
            delivery_id="delivery-1",
            correlation_id="correlation-1",
            from_peer="sender",
            text="text",
        )
    )
    claimed = store.claim(record.idempotency_key)
    assert claimed and claimed.claim_token
    store.mark_failed(record.idempotency_key, claimed.claim_token, "offline")

    args = parser().parse_args(["--json", "delivery", "retry", record.idempotency_key])
    assert args.func(args) == 0
    assert '"status": "pending"' in capsys.readouterr().out

    args = parser().parse_args(["--json", "delivery", "purge", record.idempotency_key])
    assert args.func(args) == 0
    assert '"deleted": 1' in capsys.readouterr().out


class FakeSpawnClient:
    def __init__(self, _url=None) -> None:
        self.stopped = []
        self.archived = []
        self.dirty = []
        self.workspace_data = {
            "id": "workspace-a",
            "container_ref": None,
            "use_worktree": False,
        }
        self.last_spawn = None

    def spawn_workspace(self, **kwargs):
        self.last_spawn = kwargs
        return {
            "workspace": dict(self.workspace_data),
            "sessions": [{"id": "session-a"}],
        }

    def workspaces(self):
        return []

    def workspace(self, workspace_id):
        assert workspace_id == "workspace-a"
        return dict(self.workspace_data)

    def stop_workspace(self, workspace_id):
        self.stopped.append(workspace_id)

    def archive_workspace(self, workspace_id):
        self.archived.append(workspace_id)
        return {"id": workspace_id, "archived": True}

    def dirty_repositories(self, workspace_id):
        assert workspace_id == "workspace-a"
        return list(self.dirty)

    def sessions(self, _workspace_id):
        return [{"id": "session-a", "created_at": "2026-08-12T00:00:00Z"}]

    def providers(self):
        return []


def test_prompt_idle_sends_only_when_not_running(monkeypatch, capsys) -> None:
    class IdleClient(FakeSpawnClient):
        def session(self, session_id):
            assert session_id == "session-a"
            return {"id": session_id, "workspace_id": "workspace-a"}

        def workspace_summaries(self, archived=False):
            assert archived is False
            return [
                {
                    "workspace_id": "workspace-a",
                    "latest_process_status": "completed",
                    "has_pending_approval": False,
                }
            ]

        def send(self, session_id, prompt, sender_session=None):
            return {
                "session_id": session_id,
                "prompt": prompt,
                "sender": sender_session,
            }

    monkeypatch.setattr(cli, "CdesktopClient", IdleClient)
    args = argparse.Namespace(
        session_id="session-a",
        message="continue",
        message_file=None,
        sender_session="manager",
        url=None,
        json=True,
    )

    assert cli.cmd_prompt_idle(args) == 0
    assert '"verified_idle": true' in capsys.readouterr().out


def test_prompt_idle_refuses_running_workspace(monkeypatch) -> None:
    class RunningClient(FakeSpawnClient):
        def session(self, _session_id):
            return {"workspace_id": "workspace-a"}

        def workspace_summaries(self, archived=False):
            return [{"workspace_id": "workspace-a", "latest_process_status": "running"}]

    monkeypatch.setattr(cli, "CdesktopClient", RunningClient)
    args = argparse.Namespace(
        session_id="session-a",
        message="continue",
        message_file=None,
        sender_session=None,
        url=None,
        json=True,
    )

    with pytest.raises(ValueError, match="running"):
        cli.cmd_prompt_idle(args)


def test_failover_starts_visible_successor_on_approved_profile(
    monkeypatch, tmp_path, capsys
) -> None:
    profile_store = ProfileStore(tmp_path / "profiles.json")
    profile_store.set(
        Profile(
            name="claude-api",
            executor="CLAUDE_CODE",
            provider_id="provider-a",
            credential_kind="api",
            automatic_failover=True,
        )
    )

    class FailoverClient(FakeSpawnClient):
        spawned = None

        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data = {"id": "workspace-a", "archived": False}

        def providers(self):
            return [
                {
                    "id": "provider-a",
                    "enabled": True,
                    "kind": "Custom",
                    "perAgentEnabled": {"CLAUDE_CODE": True},
                }
            ]

        def spawn_teammate(self, **kwargs):
            type(self).spawned = kwargs
            return {"session": {"id": "session-b"}}

    monkeypatch.setattr(cli, "CdesktopClient", FailoverClient)
    monkeypatch.setattr(cli, "ProfileStore", lambda: profile_store)
    args = argparse.Namespace(
        workspace_id="workspace-a",
        profile_name="claude-api",
        checkpoint="resume tests",
        checkpoint_file=None,
        name=None,
        unattended=True,
        new_worktree=False,
        archive_source=False,
        confirm_reconciled=False,
        no_bridge=False,
        lease_ttl_seconds=60,
        url=None,
        json=True,
    )

    assert cli.cmd_failover(args) == 0
    assert FailoverClient.spawned["caller_session"] == "session-a"
    assert FailoverClient.spawned["provider_id"] == "provider-a"
    assert FailoverClient.spawned["permission_policy"] == "BYPASS_PERMISSIONS"
    assert '"action": "visible-successor-started"' in capsys.readouterr().out


def test_spawn_direct_acquires_workspace_lease(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "CdesktopClient", FakeSpawnClient)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)

    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=False,
        json=True,
    )

    assert cli.cmd_spawn(args) == 0

    leases = LeaseStore(lease_dir).list()
    assert len(leases) == 1
    assert leases[0].repo_path == str(repo.resolve())
    assert leases[0].worktree_path is None
    assert leases[0].workspace_id == "workspace-a"
    assert leases[0].session_id == "session-a"


def test_spawn_worktree_acquires_container_lease(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    container = tmp_path / "container"
    repo.mkdir()
    (container / repo.name).mkdir(parents=True)
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)

    class WorktreeClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data = {
                "id": "workspace-a",
                "container_ref": str(container),
                "use_worktree": True,
            }

    monkeypatch.setattr(cli, "CdesktopClient", WorktreeClient)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=True,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=False,
        json=True,
    )

    assert cli.cmd_spawn(args) == 0

    lease = LeaseStore(lease_dir).list()[0]
    assert lease.repo_path == str(repo.resolve())
    assert lease.worktree_path == str((container / repo.name).resolve())
    assert lease.workspace_id == "workspace-a"


def test_close_archive_releases_only_workspace_lease(
    monkeypatch, tmp_path: Path
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    lease_dir = tmp_path / "leases"
    store = LeaseStore(lease_dir)
    store.acquire("owner-a", repo_a, ttl_seconds=60, workspace_id="workspace-a")
    other = store.acquire("owner-b", repo_b, ttl_seconds=60, workspace_id="workspace-b")
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "CdesktopClient", FakeSpawnClient)
    monkeypatch.setattr(cli.routing, "disable", lambda _workspace_id: None)

    args = argparse.Namespace(
        workspace_id="workspace-a",
        url=None,
        archive=True,
        confirm_reconciled=True,
        preserve_dirty=False,
        json=True,
        message=None,
        message_file=None,
        sender_session=None,
    )

    assert cli.cmd_close(args) == 0

    remaining = LeaseStore(lease_dir).list()
    assert [lease.token for lease in remaining] == [other.token]


def test_spawn_direct_fails_closed_when_repo_leased(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    LeaseStore(lease_dir).acquire("other", repo, ttl_seconds=60)
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)

    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )

    with pytest.raises(cli.leases.LeaseError):
        cli.cmd_spawn(args)


def test_spawn_direct_releases_pending_lease_when_cdesktop_start_fails(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)

    class FailingClient(FakeSpawnClient):
        def spawn_workspace(self, **_kwargs):
            raise RuntimeError("start failed")

    monkeypatch.setattr(cli, "CdesktopClient", FailingClient)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )

    with pytest.raises(RuntimeError, match="start failed"):
        cli.cmd_spawn(args)

    assert LeaseStore(lease_dir).list() == []


def test_validate_base_branch_rejects_raw_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    cli._validate_base_branch(repo, "main")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="raw commit"):
        cli._validate_base_branch(repo, head)


def test_unattended_requires_worktree(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission=None,
        unattended=True,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )
    with pytest.raises(ValueError, match="requires --worktree"):
        cli.cmd_spawn(args)


def test_unattended_worktree_selects_bypass(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    container = tmp_path / "container"
    repo.mkdir()
    (container / repo.name).mkdir(parents=True)
    lease_dir = tmp_path / "leases"
    instances = []

    class WorktreeClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data["container_ref"] = str(container)
            self.workspace_data["use_worktree"] = True
            instances.append(self)

    monkeypatch.setattr(cli, "CdesktopClient", WorktreeClient)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=True,
        permission=None,
        unattended=True,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )
    assert cli.cmd_spawn(args) == 0
    assert instances[0].last_spawn["permission_policy"] == "BYPASS_PERMISSIONS"
