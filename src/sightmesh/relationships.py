from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import service


def default_database() -> Path:
    configured = os.environ.get("SIGHTMESH_RELATIONSHIPS_DB")
    return (
        Path(configured).expanduser()
        if configured
        else service.state_dir() / "relationships.sqlite3"
    )


@dataclass(frozen=True)
class ParentEdge:
    child_session_id: str
    child_workspace_id: str
    parent_session_id: str
    parent_workspace_id: str | None
    created_at: float

    def to_dict(self) -> dict[str, str | float | None]:
        return asdict(self)


class RelationshipStore:
    def __init__(self, database: Path | None = None) -> None:
        self.database = (database or default_database()).expanduser()
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database.parent.chmod(0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS parent_edges (
                    child_session_id TEXT PRIMARY KEY NOT NULL,
                    child_workspace_id TEXT NOT NULL,
                    parent_session_id TEXT NOT NULL,
                    parent_workspace_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS parent_edges_parent_idx "
                "ON parent_edges(parent_session_id, created_at)"
            )
        self.database.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def record(
        self,
        *,
        child_session_id: str,
        child_workspace_id: str,
        parent_session_id: str,
        parent_workspace_id: str | None,
    ) -> ParentEdge:
        if child_session_id == parent_session_id:
            raise ValueError("A session cannot be its own parent")
        edge = ParentEdge(
            child_session_id=child_session_id,
            child_workspace_id=child_workspace_id,
            parent_session_id=parent_session_id,
            parent_workspace_id=parent_workspace_id,
            created_at=time.time(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO parent_edges (
                    child_session_id, child_workspace_id, parent_session_id,
                    parent_workspace_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(child_session_id) DO UPDATE SET
                    child_workspace_id = excluded.child_workspace_id,
                    parent_session_id = excluded.parent_session_id,
                    parent_workspace_id = excluded.parent_workspace_id,
                    created_at = excluded.created_at
                """,
                (
                    edge.child_session_id,
                    edge.child_workspace_id,
                    edge.parent_session_id,
                    edge.parent_workspace_id,
                    edge.created_at,
                ),
            )
        return edge

    def parent(self, child_session_id: str) -> ParentEdge | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM parent_edges WHERE child_session_id = ?",
                (child_session_id,),
            ).fetchone()
        return self._edge(row) if row else None

    def children(self, parent_session_id: str) -> list[ParentEdge]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parent_edges WHERE parent_session_id = ? "
                "ORDER BY created_at",
                (parent_session_id,),
            ).fetchall()
        return [self._edge(row) for row in rows]

    @staticmethod
    def _edge(row: sqlite3.Row) -> ParentEdge:
        return ParentEdge(
            child_session_id=str(row["child_session_id"]),
            child_workspace_id=str(row["child_workspace_id"]),
            parent_session_id=str(row["parent_session_id"]),
            parent_workspace_id=(
                str(row["parent_workspace_id"])
                if row["parent_workspace_id"] is not None
                else None
            ),
            created_at=float(row["created_at"]),
        )
