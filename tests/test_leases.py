from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agent_deck.leases import LeaseError, LeaseStore


def test_acquire_fails_closed_for_live_repo_owner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = LeaseStore(tmp_path / "leases")
    first = store.acquire("owner-a", repo, ttl_seconds=60)

    with pytest.raises(LeaseError, match="already owned"):
        store.acquire("owner-b", repo, ttl_seconds=60)

    assert store.release(first.token).owner == "owner-a"
    assert store.acquire("owner-b", repo, ttl_seconds=60).owner == "owner-b"


def test_expired_lease_is_recovered_on_next_acquire(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = LeaseStore(tmp_path / "leases")
    store.acquire("old", repo, ttl_seconds=1)
    for path in (tmp_path / "leases").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["expires_at"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")

    lease = store.acquire("new", repo, ttl_seconds=60)

    assert lease.owner == "new"
    assert [item.owner for item in store.list()] == ["new"]


def test_conflict_includes_worktree_path(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    worktree = tmp_path / "worktree"
    for path in (repo_a, repo_b, worktree):
        path.mkdir()
    store = LeaseStore(tmp_path / "leases")
    store.acquire("owner-a", repo_a, worktree, ttl_seconds=60)

    with pytest.raises(LeaseError, match="already owned"):
        store.acquire("owner-b", repo_b, worktree, ttl_seconds=60)


def test_distinct_worktrees_from_same_repo_can_coexist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    for path in (repo, worktree_a, worktree_b):
        path.mkdir()
    store = LeaseStore(tmp_path / "leases")

    first = store.acquire("owner-a", repo, worktree_a, ttl_seconds=60)
    second = store.acquire("owner-b", repo, worktree_b, ttl_seconds=60)

    assert first.worktree_path != second.worktree_path
    assert sorted(item.owner for item in store.list()) == ["owner-a", "owner-b"]


def test_direct_checkout_refuses_existing_worktree_and_blocks_new_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    store = LeaseStore(tmp_path / "leases")

    store.acquire("worktree-owner", repo, worktree, ttl_seconds=60)
    with pytest.raises(LeaseError, match="already owned"):
        store.acquire("direct-owner", repo, ttl_seconds=60)

    other = tmp_path / "other-repo"
    other.mkdir()
    store.acquire("direct-owner", other, ttl_seconds=60)
    with pytest.raises(LeaseError, match="already owned"):
        store.acquire("worktree-owner", other, tmp_path / "other-worktree", ttl_seconds=60)


def test_one_shot_cli_style_lease_lives_until_ttl(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = LeaseStore(tmp_path / "leases")

    lease = store.acquire("one-shot", repo, ttl_seconds=60)

    assert lease.live is True
    assert store.list(include_stale=False)[0].token == lease.token


def test_release_checks_token_and_owner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = LeaseStore(tmp_path / "leases")
    lease = store.acquire("owner-a", repo, ttl_seconds=60, workspace_id="workspace-a")

    with pytest.raises(LeaseError, match="No lease"):
        store.release("wrong-token")
    with pytest.raises(LeaseError, match="owner"):
        store.release(lease.token, owner="owner-b")
    with pytest.raises(LeaseError, match="workspace"):
        store.release(lease.token, workspace_id="workspace-b")

    assert store.release(lease.token, owner="owner-a", workspace_id="workspace-a").token == lease.token


def test_corrupt_lease_state_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir()
    (lease_dir / "broken.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(LeaseError, match="Invalid lease file"):
        LeaseStore(lease_dir).acquire("owner", repo, ttl_seconds=60)


def test_renew_and_workspace_release(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = LeaseStore(tmp_path / "leases")
    lease = store.acquire("owner", repo, ttl_seconds=60, workspace_id="workspace-a")

    renewed = store.renew(lease.token, ttl_seconds=120, workspace_id="workspace-a")

    assert renewed.expires_at > lease.expires_at
    assert store.workspace_token("workspace-a") == lease.token
    assert store.release_workspace("workspace-a").token == lease.token
    assert store.workspace_token("workspace-a") is None


def test_recover_stale_removes_expired_leases(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = LeaseStore(tmp_path / "leases")
    store.acquire("old", repo, ttl_seconds=1)
    for path in (tmp_path / "leases").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["expires_at"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")

    recovered = store.recover_stale()

    assert [item.owner for item in recovered] == ["old"]
    assert store.list() == []


def test_migration_git_status_parsing() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "migration-dry-run.py"
    spec = importlib.util.spec_from_file_location("migration_dry_run", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.parse_porcelain_paths(" M a.txt\nR  old.txt -> new.txt\n?? scratch\n") == [
        "a.txt",
        "new.txt",
        "scratch",
    ]
