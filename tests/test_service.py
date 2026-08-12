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


def test_service_paths_are_scoped_to_agent_deck() -> None:
    assert service.plist_path().name == "io.agent-deck.cdesktop.plist"
    assert service.state_dir().name == "agent-deck"


def test_start_reloads_only_the_owned_launch_agent(monkeypatch, tmp_path) -> None:
    target = tmp_path / "io.agent-deck.cdesktop.plist"
    target.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(service, "plist_path", lambda: target)
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
