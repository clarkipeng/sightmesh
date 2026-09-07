"""Failure budgets have an explicit meaning, not one inferred from table shape."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path

from . import history
from .sqlite_durability import configure_connection, confirm_database, require_policy


class AccountingContractError(RuntimeError):
    pass


def version(conn: sqlite3.Connection) -> int | None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='evidence_contract' AND type='table'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT version FROM evidence_contract WHERE component='failure_accounting'"
    ).fetchone()
    if row is not None and row[0] != 1:
        raise AccountingContractError("Unsupported failure accounting contract")
    return row[0] if row is not None else None


def initialize(conn: sqlite3.Connection, *, existing: bool) -> None:
    """Called under the initializer's write lock, before changing task rows."""
    if version(conn) is not None:
        return
    if existing:
        raise AccountingContractError(
            "Unversioned failure accounting requires an explicit budget reset; "
            "refusing to infer counter semantics"
        )
    # Only a fresh task store can establish its meaning without a cutover.
    conn.execute(history.CONTRACT_DDL)
    conn.execute("INSERT INTO evidence_contract VALUES ('failure_accounting', 1)")


def fingerprint(conn: sqlite3.Connection) -> str:
    """Hash all schema and rows in the caller's consistent read transaction.

    Include non-task writes (even ones without a version bump). Python's SQLite
    value representations preserve NUL text and blobs, unlike SQL quote().
    """
    if not conn.in_transaction:
        raise ValueError("fingerprint requires a consistent read transaction")
    contents = [
        tuple(row)
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    ]
    contents.extend(
        (name, conn.execute(f"PRAGMA {name}").fetchone()[0])
        for name in ("user_version", "application_id")
    )
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        quoted = '"' + name.replace('"', '""') + '"'
        contents.append(
            (
                name,
                sorted(
                    repr(tuple(row)) for row in conn.execute(f"SELECT * FROM {quoted}")
                ),
            )
        )
    return hashlib.sha256(json.dumps(contents).encode()).hexdigest()


def reset_budget(path: Path, *, expected_fingerprint: str) -> bool:
    """Reset an explicitly approved, unversioned WAL store; return False on retry.

    The operator must stop ALL old writers before taking the approved snapshot
    and keep them stopped through activation. SQLite locks and task versions do
    not fence an old binary that resumes later. No default path or force mode.
    """
    from .task_store import _MANAGED_TASKS_DDL

    with closing(
        sqlite3.connect(
            path.absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None
        )
    ) as conn:
        conn.row_factory = sqlite3.Row
        configure_connection(conn)
        require_policy(conn)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = version(conn) is None
            if changed:
                if fingerprint(conn) != expected_fingerprint:
                    raise AccountingContractError(
                        "Database changed since approved snapshot"
                    )
                schema = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='managed_tasks' "
                    "AND NOT EXISTS (SELECT 1 FROM sqlite_master WHERE type='trigger')"
                ).fetchone()
                expected_sql = _MANAGED_TASKS_DDL.format(name="managed_tasks")
                if (
                    schema is None
                    or " ".join(
                        schema[0].replace('"managed_tasks"', "managed_tasks").split()
                    )
                    != " ".join(expected_sql.split())
                ):
                    raise AccountingContractError(
                        "Budget reset requires the current task schema"
                    )
                tasks = conn.execute("SELECT task_id FROM managed_tasks").fetchall()
                has_history = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='task_history' AND type='table'"
                ).fetchone()
                for task in tasks:
                    key = (task["task_id"],)
                    with (
                        history.change(conn, "task", key, "failure-budget-reset")
                        if has_history
                        else nullcontext()
                    ):
                        conn.execute(
                            "UPDATE managed_tasks SET attempts=0,version=version+1 WHERE task_id=?",
                            key,
                        )
                # Baselines begin at the new budget; no legacy counter mirror.
                history.ensure_schema(conn)
                conn.execute(
                    "INSERT INTO evidence_contract VALUES ('failure_accounting', 1)"
                )
        confirm_database(conn, path)
    return changed
