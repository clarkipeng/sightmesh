from __future__ import annotations

from pathlib import Path

import pytest

from sightmesh import escalation, succession, task_store


@pytest.fixture(autouse=True)
def _isolated_ownership(monkeypatch, tmp_path: Path) -> Path:
    """Keep every test off the operator's real durable ownership state."""
    path = tmp_path / "ownership.json"
    monkeypatch.setattr(succession, "default_ownership_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def _isolate_escalation_store(monkeypatch, tmp_path):
    """Every test gets its own escalation store so spawn-path tests never
    touch the real ~/.local/state/sightmesh directory."""
    isolated = tmp_path / "escalations.sqlite3"
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: isolated)
    # TaskStore imported the path function directly, so patching only
    # ``escalation`` left SDK/CLI default construction pointed at the live
    # operator store. Guard the constructor too: an explicit test path must be
    # below this test's tmp root, and an implicit one resolves to ``isolated``.
    monkeypatch.setattr(task_store, "escalation_db_path", lambda: isolated)
    original_init = task_store.TaskStore.__init__

    def isolated_task_store(self, path=None):
        chosen = Path(path) if path is not None else isolated
        if not chosen.resolve().is_relative_to(tmp_path.resolve()):
            raise AssertionError(f"test attempted to open non-isolated TaskStore: {chosen}")
        original_init(self, chosen)

    monkeypatch.setattr(task_store.TaskStore, "__init__", isolated_task_store)


@pytest.fixture(autouse=True)
def _neutral_launcher_env(monkeypatch):
    """Launcher detection and parent resolution read the ambient environment.

    Running the suite from inside a real cdesktop or Conductor session would
    otherwise feed live ids into tests that never asked for one. Clearing the
    hints here makes every test's launcher context explicit; a test that wants
    one still sets it.
    """
    for name in (
        escalation.CDESKTOP_SESSION_ENV,
        *escalation.CONDUCTOR_ENV_HINTS,
    ):
        monkeypatch.delenv(name, raising=False)
