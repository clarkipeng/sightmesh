from __future__ import annotations

import os
import sqlite3

import pytest

from sightmesh import sqlite_durability as durability
from sightmesh.task_store import TaskStore, TaskStoreError


def test_every_task_connection_uses_wal_full_and_fullfsync(tmp_path):
    store = TaskStore(tmp_path / "nested" / "task.sqlite")
    for _ in range(2):
        with store.connect() as conn:
            assert durability.require_policy(conn) == store.path
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
            durability.confirm_database(conn, store.path)


@pytest.mark.parametrize(
    "weaken", ["PRAGMA synchronous=NORMAL", "PRAGMA fullfsync=OFF"]
)
def test_reference_policy_is_checked_on_actual_writer(tmp_path, weaken):
    store = TaskStore(tmp_path / "task.sqlite")
    with store.connect() as conn:
        conn.execute(weaken)
        with pytest.raises(durability.DurabilityUnavailable):
            durability.require_policy(conn)
    with store.connect() as conn:
        assert durability.require_policy(conn) == store.path


def test_directory_barrier_never_opens_the_database_and_propagates_failure(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "nested" / "task.sqlite")
    actual_open = os.open
    opened = []

    def directories_only(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY and path.is_dir()
        opened.append(path)
        return actual_open(path, flags, *args, **kwargs)

    with store.connect() as conn:
        monkeypatch.setattr(os, "open", directories_only)
        durability.confirm_database(conn, store.path)
        assert store.path.parent in opened and tmp_path in opened
        monkeypatch.setattr(
            os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("barrier failed"))
        )
        with pytest.raises(OSError, match="barrier failed"):
            durability.confirm_database(conn, store.path)


def test_directory_chain_includes_intermediate_symlink_targets(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    file = target / "evidence"
    file.touch()
    intermediate, visible = tmp_path / "intermediate", tmp_path / "visible"
    intermediate.mkdir()
    visible.mkdir()
    (intermediate / "link").symlink_to("../target")
    (visible / "link").symlink_to("../intermediate/link")
    opened = []
    actual_open = os.open

    def capture(path, flags, *args, **kwargs):
        opened.append(path)
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", capture)
    durability.confirm_directory_entries(visible / "link" / "evidence")
    confirmed = {path.resolve() for path in opened}
    assert intermediate in confirmed and visible in confirmed and target in confirmed


def test_uncommitted_and_memory_references_are_not_confirmed(tmp_path):
    store = TaskStore(tmp_path / "task.sqlite")
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(durability.DurabilityUnavailable, match="not committed"):
            durability.confirm_database(conn, store.path)
    with sqlite3.connect(":memory:") as conn:
        durability.configure_connection(conn)
        with pytest.raises(durability.DurabilityUnavailable):
            durability.require_policy(conn)


@pytest.mark.parametrize("contract", [None, 2])
def test_checkpoint_reference_contract_is_explicit_not_inferred_from_shape(
    tmp_path, contract
):
    path = tmp_path / "task.sqlite"
    store = TaskStore(path)
    with store.connect() as conn:
        if contract is None:
            conn.execute(
                "DELETE FROM evidence_contract WHERE component='checkpoint_retention'"
            )
        else:
            conn.execute(
                "UPDATE evidence_contract SET version=? WHERE component='checkpoint_retention'",
                (contract,),
            )
    with pytest.raises(TaskStoreError, match="checkpoint"):
        TaskStore(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT version FROM evidence_contract WHERE component='checkpoint_retention'"
        ).fetchone()
        assert row == (None if contract is None else (contract,))
