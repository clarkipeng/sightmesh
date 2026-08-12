from pathlib import Path

from agent_deck.cdesktop import CdesktopClient


class FakeClient(CdesktopClient):
    def __init__(self) -> None:
        self.base_url = "http://127.0.0.1:1"
        self.calls = []

    def repos(self):
        return [{"id": "existing", "path": "/tmp/repo"}]

    def request(self, method, path, payload=None, query=None, headers=None):
        self.calls.append((method, path, payload, query, headers))
        return {"id": "created"}


def test_register_repo_reuses_exact_path() -> None:
    client = FakeClient()
    repo = client.register_repo(Path("/tmp/repo"))
    assert repo["id"] == "existing"
    assert client.calls == []


def test_send_marks_sender_when_provided() -> None:
    client = FakeClient()
    client.send("target", "hello", "sender")
    assert client.calls == [
        (
            "POST",
            "/sessions/target/follow-up",
            {"prompt": "hello"},
            None,
            {"x-cdesktop-from-session": "sender"},
        )
    ]


def test_configure_local_preserves_config_and_forces_privacy(tmp_path) -> None:
    client = FakeClient()
    client.info = lambda: {
        "config": {"theme": "DARK", "analytics_enabled": True, "relay_enabled": True}
    }
    client.configure_local(tmp_path)
    method, path, payload, _, _ = client.calls[0]
    assert (method, path) == ("PUT", "/config")
    assert payload["theme"] == "DARK"
    assert payload["analytics_enabled"] is False
    assert payload["relay_enabled"] is False
    assert payload["workspace_dir"] == str(tmp_path.resolve())


def test_dirty_repositories_reports_direct_checkout(monkeypatch, tmp_path) -> None:
    client = FakeClient()
    client.workspace = lambda _: {"use_worktree": False}
    client.workspace_repos = lambda _: [
        {"is_git": True, "path": str(tmp_path), "name": "repo"}
    ]

    class Result:
        returncode = 0
        stdout = "?? pending.txt\n"
        stderr = ""

    monkeypatch.setattr("agent_deck.cdesktop.subprocess.run", lambda *args, **kwargs: Result())
    assert client.dirty_repositories("workspace") == [
        {"path": str(tmp_path), "status": "?? pending.txt"}
    ]
