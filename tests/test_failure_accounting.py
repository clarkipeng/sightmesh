from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from sightmesh import failure_accounting as accounting
from sightmesh import history
from sightmesh.escalation import EscalationStore
from sightmesh.sqlite_durability import DurabilityUnavailable
from sightmesh.task_store import _MANAGED_TASKS_DDL, TaskStore, TaskStoreError


def unversioned_store(path, *, state="active"):
    """The #123 hybrid: current schema, unversioned counters, real saved progress."""
    database = EscalationStore(path)
    with database._connect() as conn:
        conn.execute(_MANAGED_TASKS_DDL.format(name='"managed_tasks"'))
        conn.execute(
            "INSERT INTO managed_tasks "
            "(task_id,scope,task_key,state,epoch,attempts,max_attempts,child_limit,"
            "spec_json,workspace_id,holder_session_id,checkpoint,result,version,"
            "child_event_seq,last_woken_seq,liveness,liveness_episode,liveness_since,"
            "liveness_wakes,liveness_evidence,over_budget,checkpoint_at,created_at,updated_at) "
            "VALUES('t','operator','worker',?,4,3,3,0,'{}','ws','session','progress',"
            "'saved result',7,11,9,'stalled',2,10,1,'evidence',1,12,1,2)",
            (state,),
        )
    return database


def digest(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("BEGIN")
        return accounting.fingerprint(conn)


@pytest.mark.parametrize("count", [None, 0, 3])
def test_open_refuses_unversioned_current_schema_without_mutation(tmp_path, count):
    """Even an empty table or zero counters cannot attest accounting semantics."""
    path = tmp_path / "tasks.sqlite"
    database = unversioned_store(path)
    with database._connect() as conn:
        if count is None:
            conn.execute("DELETE FROM managed_tasks")
        else:
            conn.execute("UPDATE managed_tasks SET attempts=?", (count,))
    before = digest(path)
    for _ in range(2):
        with pytest.raises(TaskStoreError, match="Unversioned failure accounting"):
            TaskStore(path)
        assert digest(path) == before


@pytest.mark.parametrize(
    "state",
    [
        "reserved",
        "active",
        "replacing",
        "blocked",
        "completed",
        "cancelled",
        "lost",
        "exhausted",
    ],
)
def test_reset_preserves_every_non_counter_field_and_never_resurrects(tmp_path, state):
    path = tmp_path / "tasks.sqlite"
    database = unversioned_store(path, state=state)
    with database._connect() as conn:
        conn.execute('CREATE TABLE "unrelated facts"(value BLOB)')
        conn.execute('INSERT INTO "unrelated facts" VALUES(?)', (b"untouched\x00",))
        before = dict(conn.execute("SELECT * FROM managed_tasks").fetchone())
    assert accounting.reset_budget(path, expected_fingerprint=digest(path))
    with database._connect() as conn:
        after = dict(conn.execute("SELECT * FROM managed_tasks").fetchone())
        assert after == {**before, "attempts": 0, "version": before["version"] + 1}
        assert (
            conn.execute('SELECT value FROM "unrelated facts"').fetchone()[0]
            == b"untouched\x00"
        )
        rows = history.task_history(conn, "t")
        assert len(rows) == 1 and rows[0]["missing_history"] == 1
        assert json.loads(rows[0]["changed"])["attempts"] == 0  # no legacy mirror
    task = TaskStore(path).get_by_id("t")
    assert task.state == state and task.version == 8 and task.attempts == 0


def test_confirmed_retry_never_resets_new_failures_or_history(tmp_path):
    """A repeated cutover is harmless even after a real failure is charged."""
    path = tmp_path / "tasks.sqlite"
    unversioned_store(path)
    approved = digest(path)
    assert accounting.reset_budget(path, expected_fingerprint=approved)
    store = TaskStore(path)
    for text in ("one", "two", "three"):
        assert store.checkpoint("t", text).attempts == 0
    assert store.finish("t", "lost", "real failure").attempts == 1
    before = digest(path)
    assert not accounting.reset_budget(path, expected_fingerprint=approved)
    assert digest(path) == before
    assert TaskStore(path).get_by_id("t").attempts == 1


@pytest.mark.parametrize(
    "sql,args",
    [
        ("UPDATE managed_tasks SET version=version+1", ()),
        ("UPDATE managed_tasks SET checkpoint_at=99", ()),
        ("UPDATE managed_tasks SET checkpoint=?", ("progress\x00new",)),
        ("INSERT INTO extra VALUES(?)", (b"new\x00bytes",)),
        ("CREATE INDEX changed ON extra(value)", ()),
        ("CREATE VIEW changed AS SELECT * FROM extra", ()),
    ],
)
def test_any_snapshot_drift_refuses_without_further_changes(tmp_path, sql, args):
    """Task versions alone miss observations, effects, binary values and schema."""
    path = tmp_path / "tasks.sqlite"
    database = unversioned_store(path)
    with database._connect() as conn:
        conn.execute("CREATE TABLE extra(value)")
    approved = digest(path)
    with database._connect() as conn:
        conn.execute(sql, args)
    changed = digest(path)
    assert changed != approved
    with pytest.raises(accounting.AccountingContractError, match="changed since"):
        accounting.reset_budget(path, expected_fingerprint=approved)
    assert digest(path) == changed


@pytest.mark.parametrize("existing_history", [False, True])
def test_history_failure_rolls_back_reset_and_contract(
    tmp_path, monkeypatch, existing_history
):
    """Neither baseline failure nor an existing-history append may half-commit."""
    path = tmp_path / "tasks.sqlite"
    database = unversioned_store(path)
    if existing_history:
        with database._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            history.ensure_schema(conn)
    before = digest(path)
    original = history.record_change

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected after history insert")

    monkeypatch.setattr(history, "record_change", fail)
    with pytest.raises(RuntimeError, match="injected"):
        accounting.reset_budget(path, expected_fingerprint=before)
    assert digest(path) == before


def test_existing_history_gets_only_the_new_reset_values(tmp_path):
    path = tmp_path / "tasks.sqlite"
    database = unversioned_store(path)
    with database._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        history.ensure_schema(conn)
        old = [tuple(row) for row in history.task_history(conn, "t")]
    accounting.reset_budget(path, expected_fingerprint=digest(path))
    with database._connect() as conn:
        rows = history.task_history(conn, "t")
        assert [tuple(row) for row in rows[:-1]] == old
        assert rows[-1]["cause"] == "failure-budget-reset"
        assert json.loads(rows[-1]["changed"]) == {"attempts": 0, "version": 8}


@pytest.mark.parametrize("component", ["failure_accounting", "task_history"])
def test_future_contract_refuses_atomically(tmp_path, component):
    path = tmp_path / "tasks.sqlite"
    database = unversioned_store(path)
    with database._connect() as conn:
        conn.execute(history.CONTRACT_DDL)
        conn.execute("INSERT INTO evidence_contract VALUES(?,99)", (component,))
    before = digest(path)
    with pytest.raises(
        (accounting.AccountingContractError, history.HistoryContractError)
    ):
        accounting.reset_budget(path, expected_fingerprint=before)
    assert digest(path) == before
    with pytest.raises(TaskStoreError):
        TaskStore(path)
    assert digest(path) == before


def test_retry_after_post_commit_barrier_failure_recovers_forward(
    tmp_path, monkeypatch
):
    path = tmp_path / "tasks.sqlite"
    unversioned_store(path)
    approved = digest(path)
    with monkeypatch.context() as fault:
        fault.setattr(
            accounting,
            "confirm_database",
            lambda *_: (_ for _ in ()).throw(DurabilityUnavailable("barrier failed")),
        )
        with pytest.raises(DurabilityUnavailable):
            accounting.reset_budget(path, expected_fingerprint=approved)
    store = TaskStore(path)
    assert store.finish("t", "lost", "new failure").attempts == 1
    before = digest(path)
    assert not accounting.reset_budget(path, expected_fingerprint=approved)
    assert digest(path) == before


def test_concurrent_reset_commits_once(tmp_path):
    path = tmp_path / "tasks.sqlite"
    unversioned_store(path)
    approved = digest(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: accounting.reset_budget(path, expected_fingerprint=approved),
                range(2),
            )
        )
    assert sorted(outcomes) == [False, True]
    assert TaskStore(path).get_by_id("t").version == 8


def test_reset_requires_existing_current_schema_and_explicit_isolated_path(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        accounting.reset_budget(missing, expected_fingerprint="unused")
    assert not missing.exists()
    with pytest.raises(AssertionError, match="non-isolated budget reset"):
        accounting.reset_budget(
            tmp_path.parent / "forbidden.sqlite", expected_fingerprint="unused"
        )
    database = EscalationStore(tmp_path / "old.sqlite")
    with database._connect() as conn:
        conn.execute(
            _MANAGED_TASKS_DDL.replace("attempts >= 0", "attempts > 0").format(
                name="managed_tasks"
            )
        )
    before = digest(database.path)
    with pytest.raises(accounting.AccountingContractError, match="current task schema"):
        accounting.reset_budget(database.path, expected_fingerprint=before)
    assert digest(database.path) == before


def test_fingerprint_requires_consistency_and_preserves_value_types(tmp_path):
    with closing(sqlite3.connect(tmp_path / "types.sqlite")) as conn:
        conn.execute("CREATE TABLE values_to_check(value)")
        with pytest.raises(ValueError, match="consistent read"):
            accounting.fingerprint(conn)
        conn.execute("BEGIN")
        values = ["a\x00b", "a\x00c", b"a\x00b", 1, "1"]
        hashes = set()
        for value in values:
            conn.execute("DELETE FROM values_to_check")
            conn.execute("INSERT INTO values_to_check VALUES(?)", (value,))
            hashes.add(accounting.fingerprint(conn))
        assert len(hashes) == len(values)
