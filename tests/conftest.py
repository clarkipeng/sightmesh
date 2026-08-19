from __future__ import annotations

from pathlib import Path

import pytest

from sightmesh import succession


@pytest.fixture(autouse=True)
def _isolated_ownership(monkeypatch, tmp_path: Path) -> Path:
    """Keep every test off the operator's real durable ownership state."""
    path = tmp_path / "ownership.json"
    monkeypatch.setattr(succession, "default_ownership_path", lambda: path)
    return path
