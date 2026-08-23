from __future__ import annotations

from pathlib import Path

import pytest

from sightmesh import escalation, succession


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
    monkeypatch.setattr(
        escalation, "escalation_db_path", lambda: tmp_path / "escalations.sqlite3"
    )


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
