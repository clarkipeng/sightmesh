from pathlib import Path

from agent_deck import service


def test_service_definition_is_local_and_cleanup_safe(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda _: "/tmp/cdesktop")
    monkeypatch.setattr(service, "state_dir", lambda: tmp_path)
    definition = service.definition(4321)
    assert definition["ProgramArguments"] == ["/tmp/cdesktop"]
    assert definition["EnvironmentVariables"]["HOST"] == "127.0.0.1"
    assert definition["EnvironmentVariables"]["PORT"] == "4321"
    assert definition["EnvironmentVariables"]["DISABLE_WORKTREE_CLEANUP"] == "1"
    assert definition["Label"] == "io.agent-deck.cdesktop"


def test_bridge_definition_targets_managed_local_cdesktop(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda _: "/tmp/agent-deck")
    monkeypatch.setattr(service, "state_dir", lambda: tmp_path)
    definition = service.bridge_definition(4321)
    assert definition["ProgramArguments"] == [
        "/tmp/agent-deck",
        "--url",
        "http://127.0.0.1:4321",
        "bridge",
    ]
    assert definition["Label"] == "io.agent-deck.bridge"


def test_service_paths_are_scoped_to_agent_deck() -> None:
    assert service.plist_path().name == "io.agent-deck.cdesktop.plist"
    assert service.bridge_plist_path().name == "io.agent-deck.bridge.plist"
    assert service.state_dir().name == "agent-deck"


def test_start_reloads_only_the_owned_launch_agent(monkeypatch, tmp_path) -> None:
    target = tmp_path / "io.agent-deck.cdesktop.plist"
    target.write_text("fixture", encoding="utf-8")
    bridge_target = tmp_path / "io.agent-deck.bridge.plist"
    bridge_target.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge_target)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(service.subprocess, "run", run)
    service.start()
    assert calls[0][0:2] == ["launchctl", "bootout"]
    assert calls[0][2].endswith("/io.agent-deck.cdesktop")
    assert calls[1][0:2] == ["launchctl", "bootstrap"]
    assert calls[1][-1] == str(target)
    assert calls[3][0:2] == ["launchctl", "bootstrap"]
    assert calls[3][-1] == str(bridge_target)


def test_wait_until_healthy_retries_until_ready(monkeypatch) -> None:
    results = iter([False, False, True])
    monkeypatch.setattr(service, "is_healthy", lambda _port: next(results))
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    service.wait_until_healthy(4321, timeout=1)
