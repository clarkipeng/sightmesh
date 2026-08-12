import json

from sightmesh import routing


def test_routing_preserves_enabled_workspaces_and_peer_identities(monkeypatch, tmp_path) -> None:
    path = tmp_path / "bridge.json"
    monkeypatch.setattr(routing, "routing_path", lambda: path)

    routing.enable("workspace-b")
    routing.enable("workspace-a")
    routing.set_peer_identity("session-1", "peer-1")
    routing.disable("workspace-b")

    assert routing.enabled_workspaces() == {"workspace-a"}
    assert routing.peer_identity("session-1") == "peer-1"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "enabled_workspaces": ["workspace-a"],
        "peer_ids": {"session-1": "peer-1"},
    }

    routing.clear_peer_identity("session-1")
    assert routing.peer_identity("session-1") is None


def test_invalid_routing_file_fails_closed(monkeypatch, tmp_path) -> None:
    path = tmp_path / "bridge.json"
    path.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(routing, "routing_path", lambda: path)
    assert routing.enabled_workspaces() == set()
    assert routing.peer_identity("session") is None
