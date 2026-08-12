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
