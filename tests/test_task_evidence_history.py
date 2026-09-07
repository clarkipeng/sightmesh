from __future__ import annotations

import json
import sqlite3

import pytest

from sightmesh import history
from sightmesh.task_store import TaskStore


def test_outgoing_and_cleanup_lifecycle_preserve_each_occurrence_without_replay_noise(
    tmp_path,
):
    store = TaskStore(tmp_path / "task.sqlite")
    store.begin_outgoing_command("task", 1, "session", "send")
    store.settle_outgoing_command("task", 1, "send", "native", terminal=True)
    store.acknowledge_cleanup_intent("task", 1, "command_cancel", "native")
    with store.connect() as conn:
        outgoing = history.task_history(conn, "task", entity="outgoing_command")
        cleanup = history.task_history(conn, "task", entity="cleanup_intent")
    assert [json.loads(row["changed"])["state"] for row in outgoing] == [
        "sending",
        "cleanup",
        "acknowledged",
    ]
    assert [json.loads(row["changed"])["state"] for row in cleanup] == [
        "pending",
        "acknowledged",
    ]
    assert {row["entity_id"] for row in cleanup} == {"command_cancel:native"}
    store.acknowledge_cleanup_intent("task", 1, "command_cancel", "native")
    with store.connect() as conn:
        assert len(history.task_history(conn, "task")) == len(outgoing) + len(cleanup)


@pytest.mark.parametrize("mutation", ["settle", "acknowledge"])
def test_history_failure_rolls_back_real_outgoing_and_cleanup_mutations(
    tmp_path, monkeypatch, mutation
):
    store = TaskStore(tmp_path / "task.sqlite")
    store.begin_outgoing_command("task", 1, "session", "send")
    if mutation == "acknowledge":
        store.settle_outgoing_command("task", 1, "send", "native", terminal=True)
    with store.connect() as conn:
        before = [
            tuple(row) for row in conn.execute("SELECT * FROM task_outgoing_commands")
        ]
        before_cleanup = [
            tuple(row) for row in conn.execute("SELECT * FROM task_cleanup_intents")
        ]
        before_history = [tuple(row) for row in history.task_history(conn, "task")]

    def fail(*args, **kwargs):
        raise sqlite3.DatabaseError("injected history failure")

    monkeypatch.setattr(history, "record_change", fail)
    with pytest.raises(sqlite3.DatabaseError):
        if mutation == "settle":
            store.settle_outgoing_command("task", 1, "send", "native", terminal=True)
        else:
            store.acknowledge_cleanup_intent("task", 1, "command_cancel", "native")
    with store.connect() as conn:
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM task_outgoing_commands")
        ] == before
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM task_cleanup_intents")
        ] == before_cleanup
        assert [
            tuple(row) for row in history.task_history(conn, "task")
        ] == before_history
