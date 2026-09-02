"""Fixtures and small helpers shared by the adversarial kernel simulator.

The suite wires the *real* ``TaskStore`` and ``SightMesh`` SDK
(``src/sightmesh/task_store.py`` / ``src/sightmesh/sdk.py``) to
``FakeCdesktop`` (see ``fake_cdesktop.py``) - it never mocks the store. Every
test gets its own SQLite file under ``tmp_path`` per the design notes in the
task brief, so scenarios never share state or race each other.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sightmesh.sdk import SightMesh, WorkerSpec
from sightmesh.succession import QuarantinedSessionError, TerminalOwnership
from sightmesh.task_store import TaskStore

from .fake_cdesktop import FakeCdesktop


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "simulator: adversarial kernel-contract scenario (see docs/kernel-spec.md)",
    )


class FakeOwnership:
    """Mirrors ``tests/test_sdk.py``'s in-memory ownership double."""

    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {}

    def get(self, session_id: str) -> SimpleNamespace | None:
        return self.records.get(session_id)

    def assert_deliverable(self, session_id: str) -> None:
        record = self.records.get(session_id)
        if record is not None:
            # Match the real OwnershipStore so the wake outbox's deliverability
            # guard (F6) can catch a retired holder by one exception type.
            raise QuarantinedSessionError(
                TerminalOwnership(
                    session_id=record.session_id,
                    state=record.state,
                    reason=record.reason,
                    retired_at="2026-09-01T00:00:00+00:00",
                    logical_key=record.logical_key,
                    successor_session_id=record.successor_session_id,
                )
            )

    def retire(
        self, session_id: str, *, state: str = "retired", reason: str = "retired",
        logical_key: str | None = None,
    ):
        return self.records.setdefault(
            session_id,
            SimpleNamespace(
                session_id=session_id,
                state=state,
                reason=reason,
                logical_key=logical_key,
                successor_session_id=None,
            ),
        )

    def link_successor(self, session_id: str, successor_session_id: str):
        record = self.records[session_id]
        record.successor_session_id = successor_session_id
        return record

    def is_quarantined(self, session_id: str) -> bool:
        return session_id in self.records


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    return repo


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "state.sqlite3")


@pytest.fixture
def client(repo_path: Path) -> FakeCdesktop:
    return FakeCdesktop(repo_path)


@pytest.fixture
def ownership() -> FakeOwnership:
    return FakeOwnership()


@pytest.fixture
def mesh(client: FakeCdesktop, store: TaskStore, ownership: FakeOwnership) -> SightMesh:
    return SightMesh(client=client, store=store, ownership=ownership, environment={})


def make_mesh(
    client: FakeCdesktop,
    store: TaskStore,
    ownership: FakeOwnership,
    *,
    session_id: str | None = None,
) -> SightMesh:
    """Build a second SDK handle sharing durable state, as a child process would."""
    environment = {"CDESKTOP_SESSION_ID": session_id} if session_id else {}
    return SightMesh(client=client, store=store, ownership=ownership, environment=environment)


def worker_spec(key: str = "audit", **kwargs: object) -> WorkerSpec:
    values: dict[str, object] = {
        "key": key,
        "prompt": "Audit the boundary",
        "repo": "project",
        "executor": "CODEX",
    }
    values.update(kwargs)
    return WorkerSpec(**values)


def table_exists(store: TaskStore, name: str) -> bool:
    """Schema-level feature detection: does kernel v1's table exist yet?

    Scenarios that depend on a kernel v1 primitive check the primitive by its
    exact schema shape from docs/kernel-spec.md rather than guessing at a
    Python class name that has not been written yet. That keeps the test body
    stable across the pre-transplant (red) and post-transplant (green) runs.
    """
    with store._database._connect() as conn:  # noqa: SLF001 - test-only introspection
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    return row is not None


def column_exists(store: TaskStore, table: str, column: str) -> bool:
    with store._database._connect() as conn:  # noqa: SLF001 - test-only introspection
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    return column in columns


def fail_missing_kernel_v1(detail: str) -> None:
    """Fail (not error) a scenario whose kernel v1 primitive is not built yet.

    Red-first requirement (docs/kernel-spec.md, "Simulator"): scenarios that
    depend on a not-yet-implemented primitive must show up as a clean FAIL in
    pytest's summary today, and need no edits to pass once the primitive
    lands.
    """
    pytest.fail(f"kernel v1 not implemented: {detail}", pytrace=False)


def query(store: TaskStore, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with store._database._connect() as conn:  # noqa: SLF001 - test-only introspection
        return conn.execute(sql, params).fetchall()
