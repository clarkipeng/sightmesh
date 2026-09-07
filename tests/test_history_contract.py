from __future__ import annotations
import sqlite3
import pytest
from sightmesh import history
from sightmesh.effects import EffectJournal, request_hash
from sightmesh.task_store import TaskStore

def test_effect_projection_mutations_append_distinct_history(tmp_path):
    store=TaskStore(tmp_path/'state.sqlite'); journal=EffectJournal(store)
    journal.reserve('task',1,request_hash({'x':1}),'a',ttl=-1)
    journal.reserve('task',1,request_hash({'x':1}),'b')
    journal.mark_launched('task',1,'work','session')
    journal.mark_terminal('task',1,'done')
    with store.connect() as conn:
        rows=history.task_history(conn,'task',entity='effect')
    assert [row['kind'] for row in rows] == ['created','transition','transition','transition']
    assert len({row['seq'] for row in rows}) == len(rows)

def test_history_write_rolls_back_with_projection(tmp_path):
    path=tmp_path/'history.sqlite'; store=TaskStore(path)
    with pytest.raises(RuntimeError):
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("INSERT INTO task_effects(task_id,epoch,request_hash,state,owner_instance,lease_expires_at,created_at,updated_at) VALUES('x',1,'h','reserved','o',1,1,1)")
            row=conn.execute("SELECT * FROM task_effects WHERE task_id='x'").fetchone()
            history.record_change(conn,entity='effect',task_id='x',epoch=1,cause='test',kind='created',changed=history.changed_columns(None,row))
            raise RuntimeError()
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM task_effects WHERE task_id='x'").fetchone()[0] == 0
        assert history.task_history(conn,'x') == []

def test_baseline_marks_existing_projection_as_unknown_history():
    conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE managed_tasks (task_id TEXT, epoch INTEGER, state TEXT)')
    conn.execute("INSERT INTO managed_tasks VALUES ('old',2,'blocked')")
    history.ensure_schema(conn)
    row=history.task_history(conn,'old')[0]
    assert row['kind'] == 'baseline' and row['missing_history'] == 1 and row['cause'] == 'observed-baseline'
