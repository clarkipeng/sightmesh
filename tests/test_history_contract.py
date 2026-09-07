from __future__ import annotations
import sqlite3
import pytest
from sightmesh import history
from sightmesh.effects import EffectJournal, request_hash
from sightmesh import effects
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

def test_real_effect_reserve_rolls_back_when_its_history_append_fails(tmp_path, monkeypatch):
    store=TaskStore(tmp_path/'state.sqlite'); journal=EffectJournal(store)
    monkeypatch.setattr(effects.history, 'record_change', lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.DatabaseError('history failed')))
    with pytest.raises(Exception): journal.reserve('task',1,request_hash({'x':1}),'owner')
    assert journal.get('task',1) is None

def test_effect_side_doors_each_leave_an_observed_history_entry(tmp_path):
    store=TaskStore(tmp_path/'state.sqlite'); journal=EffectJournal(store)
    journal.reserve('task',1,request_hash({'x':1}),'a',ttl=-1)
    journal.reserve('task',1,request_hash({'x':1}),'b') # takeover
    journal.mark_terminal('task',1,'done')
    journal.mark_cleanup_workspace('task',1,'workspace')
    class Client:
        def stop_workspace(self, _workspace): return None
    journal.stop_terminal(Client(), journal.get('task',1))
    journal.reserve('expired',1,request_hash({'x':2}),'a',ttl=-1)
    journal.expire_reservations()
    with store.connect() as conn:
        task_causes=[row['cause'] for row in history.task_history(conn,'task',entity='effect')]
        expired_causes=[row['cause'] for row in history.task_history(conn,'expired',entity='effect')]
    assert {'reservation-takeover','cleanup-workspace','cleanup-acknowledged'} <= set(task_causes)
    assert 'reservation-expired' in expired_causes

def test_real_old_shape_upgrade_baselines_migrated_task_and_effect(tmp_path):
    from test_task_store import _round1_kernel_store
    path=tmp_path/'old.sqlite'
    database=_round1_kernel_store(path, [('old','operator','legacy',None,'blocked','session-old')])
    with database._connect() as conn:
        conn.execute("CREATE TABLE task_effects (task_id TEXT, epoch INTEGER, request_hash TEXT, state TEXT, workspace_id TEXT, session_id TEXT, outcome TEXT, owner_instance TEXT, lease_expires_at REAL, created_at REAL, updated_at REAL, PRIMARY KEY(task_id,epoch))")
        conn.execute("INSERT INTO task_effects VALUES ('old',1,'h','terminal','ws','session-old','legacy','owner',1,1,1)")
    store=TaskStore(path)
    with store.connect() as conn:
        rows=history.task_history(conn,'old')
    assert {(row['entity'],row['kind'],row['missing_history'],row['cause']) for row in rows} >= {('task','baseline',1,'observed-baseline'),('effect','baseline',1,'observed-baseline')}
