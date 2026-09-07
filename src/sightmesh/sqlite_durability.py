"""Writing-connection policy and directory barriers for retained task references.

SQLite owns and syncs its database/WAL files. Never open an extra DB file
descriptor to fsync it: closing that descriptor can release SQLite's POSIX locks.
This verifies supported VFS/fsync acknowledgements, not faulty hardware behavior.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


class DurabilityUnavailable(RuntimeError):
    pass


def configure_connection(conn: sqlite3.Connection) -> None:
    # WAL + FULL makes each commit durable. fullfsync also covers checkpoints
    # on macOS; the separate checkpoint_fullfsync switch is then redundant.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA fullfsync=ON")


def require_policy(conn: sqlite3.Connection) -> Path:
    """Check the actual writing connection; never infer a historical commit."""
    journal = conn.execute("PRAGMA main.journal_mode").fetchone()
    synchronous = conn.execute("PRAGMA main.synchronous").fetchone()
    fullfsync = conn.execute("PRAGMA fullfsync").fetchone()
    if (
        journal is None
        or str(journal[0]).lower() != "wal"
        or synchronous is None
        or synchronous[0] not in (2, 3)
        or fullfsync is None
        or fullfsync[0] != 1
    ):
        raise DurabilityUnavailable(
            "task reference requires WAL/FULL/fullfsync on its writing connection"
        )
    main = next(
        (row for row in conn.execute("PRAGMA database_list") if row[1] == "main"), None
    )
    if main is None or not main[2]:
        raise DurabilityUnavailable("task reference has no persistent SQLite owner")
    return Path(main[2])


def confirm_database(conn: sqlite3.Connection, configured_path: Path) -> None:
    """After COMMIT, confirm the actual database path and its configured alias.

    This is a supported Unix acknowledgement only. Directory trees must not be
    renamed concurrently by an external actor, matching the native owner contract.
    An error retains the working copy and can be retried; it never deletes data.
    """
    if conn.in_transaction:
        raise DurabilityUnavailable("task reference transaction is not committed")
    actual = require_policy(conn)
    if not os.path.samefile(actual, configured_path):
        raise DurabilityUnavailable("task reference belongs to a different database")
    confirm_directory_entries(actual)
    if actual != configured_path.absolute():
        confirm_directory_entries(configured_path)


def confirm_directory_entries(path: Path) -> None:
    """Confirm every reachable parent, including intermediate symlink entries."""
    if os.name != "posix":
        raise DurabilityUnavailable("no verified directory barrier on this platform")
    pending, visited = [path.absolute()], set()
    while pending:
        current = pending.pop()
        for entry in (current, *current.parents):
            if entry in visited:
                continue
            visited.add(entry)
            if entry == entry.parent:
                continue
            descriptor = os.open(entry.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if entry.is_symlink():
                pending.append(entry.parent / os.readlink(entry))
