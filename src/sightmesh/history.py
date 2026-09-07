"""Append-only task history: the facts projection updates destroy.

The scheduler keeps reading current rows; this table preserves what an
UPDATE overwrites, in the same transaction as the projection mutation, so
a rollback discards both together and a commit can never record one
without the other.

Each entry stores only the sparse changed columns (their new values) plus
the cause named where the mutation originates. A row's creation entry
records its full initial values, and the pre-upgrade contents of an
existing database become one ``observed-baseline`` entry per row with an
explicit missing-history marker - observed state, never fabricated
failures or times. Creation values plus every later sparse change is a
complete timeline: nothing a projection update destroys goes unrecorded.

``seq`` orders occurrences; it deliberately does not deduplicate them. A
replayed operation that genuinely writes twice appends twice, and two
distinct real occurrences with equal content stay distinct entries. An
update that changes nothing appends nothing.

Two families of projection values are deliberately not mirrored here:

* ``child_event_seq``/``last_woken_seq`` are monotonic counters whose
  every step is implied by a recorded event (a child terminal entry, a
  delivered wake entry), so mirroring them would be a second copy of
  facts this table already holds.
* a wake's persisted payload bytes are written once, never overwritten
  (settlement records its note in ``resolution``), so the wake row itself
  is the durable home of those bytes.

Schema versioning is an explicit contract, not a shape guess: the
``evidence_contract`` table names each component's version, and an
unrecognised version is a refusal to run, never a silent reinterpretation
(the #123 lesson: table shape does not establish accounting semantics).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

#: Component names in ``evidence_contract``.
HISTORY_COMPONENT = "task_history"
HISTORY_VERSION = 1

#: Entities whose projection rows this history preserves.
_PROJECTIONS = {
    "task": ("managed_tasks", ("task_id",)),
    "effect": ("task_effects", ("task_id", "epoch")),
    "wake": ("task_wakes", ("wake_id",)),
    "outgoing_command": ("task_outgoing_commands", ("task_id", "epoch", "dedupe_key")),
    "cleanup_intent": (
        "task_cleanup_intents",
        ("task_id", "epoch", "kind", "native_id"),
    ),
}
ENTITIES = tuple(_PROJECTIONS)
#: ``created`` records a row's full initial values; ``transition`` a sparse
#: change to them; ``observation`` a liveness/budget finding that is not task
#: progress; ``baseline`` the one observed pre-upgrade snapshot per row.
KINDS = ("created", "transition", "observation", "baseline")

#: Projection bookkeeping columns whose values are derivable (``recorded_at``
#: carries the entry's own time) and would only add noise to sparse diffs.
_DERIVED_COLUMNS = frozenset({"updated_at"})

_ENTITY_CHECK = "entity IN (" + ", ".join(f"'{value}'" for value in ENTITIES) + ")"
_KIND_CHECK = "kind IN (" + ", ".join(f"'{value}'" for value in KINDS) + ")"

_CONTRACT_DDL = """
    CREATE TABLE IF NOT EXISTS evidence_contract (
        component TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK (version > 0)
    )
"""

#: AUTOINCREMENT so ``seq`` is monotonic for the life of the database rather
#: than a reusable rowid; history is append-only and never deleted.
_HISTORY_DDL = f"""
    CREATE TABLE task_history (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        entity TEXT NOT NULL CHECK ({_ENTITY_CHECK}),
        task_id TEXT NOT NULL,
        epoch INTEGER,
        entity_id TEXT,
        kind TEXT NOT NULL CHECK ({_KIND_CHECK}),
        cause TEXT NOT NULL,
        changed TEXT NOT NULL,
        missing_history INTEGER NOT NULL DEFAULT 0
            CHECK (missing_history IN (0, 1)),
        recorded_at REAL NOT NULL
    )
"""


class HistoryContractError(RuntimeError):
    """The stored contract names a version this code does not implement."""


@dataclass
class Change:
    before: sqlite3.Row | None
    after: sqlite3.Row | None = None


@contextmanager
def change(
    conn: sqlite3.Connection,
    entity: str,
    key: tuple[object, ...],
    cause: str,
    *,
    kind: str = "transition",
) -> Iterator[Change]:
    """Record one projection mutation atomically on the caller's connection.

    The savepoint nests without committing an enclosing transaction, and also
    protects direct callers using an autocommit connection. Callers retain their
    write locks, guards and domain errors; this owns only the sparse history.
    """
    table, columns = _PROJECTIONS[entity]
    query = f"SELECT * FROM {table} WHERE " + " AND ".join(f"{c}=?" for c in columns)
    conn.execute("SAVEPOINT history_change")
    try:
        snapshot = Change(conn.execute(query, key).fetchone())
        yield snapshot
        snapshot.after = conn.execute(query, key).fetchone()
        if snapshot.after is not None:
            changed = changed_columns(snapshot.before, snapshot.after)
            if entity == "wake":
                changed.pop("payload", None)
            record_change(
                conn,
                entity=entity,
                **_identity(entity, snapshot.after),
                cause=cause,
                kind="created" if snapshot.before is None else kind,
                changed=changed,
            )
    except BaseException:
        conn.execute("ROLLBACK TO history_change")
        raise
    finally:
        conn.execute("RELEASE history_change")


def _identity(entity: str, values: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    _, columns = _PROJECTIONS[entity]
    return {
        "task_id": values["parent_task_id" if entity == "wake" else "task_id"],
        "epoch": None if entity == "wake" else values["epoch"],
        "entity_id": ":".join(
            str(values[column])
            for column in columns
            if column not in {"task_id", "epoch"}
        )
        or None,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring the history component to version 1 inside the caller's transaction.

    The caller (``TaskStore._initialize``) holds ``BEGIN IMMEDIATE`` and has
    already migrated the projection tables, so a baseline taken here observes
    kernel-v1 rows. Three cases, decided by the contract row alone:

    * no contract row: first upgrade - create the table, snapshot every
      existing projection row as its ``observed-baseline`` entry, record
      version 1;
    * version 1: nothing to do;
    * anything else: refuse. A future component rewrote the semantics and
      guessing from table shape is exactly the #123 failure.
    """
    conn.execute(_CONTRACT_DDL)
    row = conn.execute(
        "SELECT version FROM evidence_contract WHERE component = ?",
        (HISTORY_COMPONENT,),
    ).fetchone()
    if row is not None:
        version = int(row["version"] if isinstance(row, sqlite3.Row) else row[0])
        if version != HISTORY_VERSION:
            raise HistoryContractError(
                f"Task history contract version {version} is not supported by "
                f"this SightMesh (expected {HISTORY_VERSION}); refusing to "
                "reinterpret recorded semantics"
            )
        return
    conn.execute(_HISTORY_DDL)
    conn.execute("CREATE INDEX idx_task_history_task ON task_history(task_id, seq)")
    _record_baselines(conn)
    conn.execute(
        "INSERT INTO evidence_contract (component, version) VALUES (?, ?)",
        (HISTORY_COMPONENT, HISTORY_VERSION),
    )


def _record_baselines(conn: sqlite3.Connection) -> None:
    """One observed snapshot per pre-upgrade row, with history marked missing.

    The baseline is what the row says now - state, counters, timestamps as
    observed - and nothing more. How the row got there was destroyed before
    this table existed; ``missing_history = 1`` says so explicitly instead of
    fabricating transitions, failures, or times that were never recorded.
    """
    now = time.time()
    for entity, (table, _) in _PROJECTIONS.items():
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is None
        ):
            continue
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            values = dict(row)
            # A wake payload is the durable evidence object itself. History
            # links the wake occurrence; copying those bytes would make a
            # second transcript store.
            if entity == "wake":
                values.pop("payload", None)
            record_change(
                conn,
                entity=entity,
                **_identity(entity, values),
                kind="baseline",
                cause="observed-baseline",
                changed=values,
                missing_history=True,
                now=now,
            )


def changed_columns(
    before: Mapping[str, Any] | sqlite3.Row | None,
    after: Mapping[str, Any] | sqlite3.Row,
) -> dict[str, Any]:
    """The sparse new values an update just wrote; empty when nothing moved."""
    previous = {} if before is None else dict(before)
    return {
        key: value
        for key, value in dict(after).items()
        if key not in _DERIVED_COLUMNS
        and (key not in previous or previous[key] != value)
    }


def record_change(
    conn: sqlite3.Connection,
    *,
    entity: str,
    task_id: str,
    changed: Mapping[str, Any],
    cause: str,
    epoch: int | None = None,
    entity_id: str | None = None,
    kind: str = "transition",
    now: float | None = None,
    missing_history: bool = False,
) -> None:
    """Append one history entry on the caller's own connection.

    Runs inside whatever transaction the projection mutation runs in - that
    shared transaction is the whole invariant. An empty ``changed`` mapping
    appends nothing: an update that moved no column destroyed no fact.
    """
    if not changed:
        return
    conn.execute(
        "INSERT INTO task_history (entity, task_id, epoch, entity_id, kind, "
        "cause, changed, missing_history, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entity,
            str(task_id),
            int(epoch) if epoch is not None else None,
            entity_id,
            kind,
            str(cause),
            _encode(changed),
            int(missing_history),
            time.time() if now is None else now,
        ),
    )


def task_history(
    conn: sqlite3.Connection, task_id: str, *, entity: str | None = None
) -> list[sqlite3.Row]:
    """Every recorded entry for one task, oldest first."""
    if entity is None:
        return conn.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY seq",
            (str(task_id),),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM task_history WHERE task_id = ? AND entity = ? ORDER BY seq",
        (str(task_id), entity),
    ).fetchall()


def _encode(values: Mapping[str, Any]) -> str:
    return json.dumps(dict(values), sort_keys=True, separators=(",", ":"))
