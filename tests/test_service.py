import sqlite3

import pytest

from sightmesh import service


def test_service_definition_is_local_and_uses_native_cleanup(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda _: "/tmp/cdesktop")
    monkeypatch.setattr(service, "state_dir", lambda: tmp_path)
    definition = service.definition(4321)
    assert definition["ProgramArguments"] == ["/tmp/cdesktop"]
    assert definition["EnvironmentVariables"]["HOST"] == "127.0.0.1"
    assert definition["EnvironmentVariables"]["PORT"] == "4321"
    assert "DISABLE_WORKTREE_CLEANUP" not in definition["EnvironmentVariables"]
    assert definition["Umask"] == 0o077
    assert definition["Label"] == "io.sightmesh.cdesktop"


def test_bridge_definition_targets_managed_local_cdesktop(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda _: "/tmp/sightmesh")
    monkeypatch.setattr(service, "state_dir", lambda: tmp_path)
    definition = service.bridge_definition(4321)
    assert definition["ProgramArguments"] == [
        "/tmp/sightmesh",
        "--url",
        "http://127.0.0.1:4321",
        "bridge",
    ]
    assert definition["Label"] == "io.sightmesh.bridge"


def test_service_paths_are_scoped_to_sightmesh() -> None:
    assert service.plist_path().name == "io.sightmesh.cdesktop.plist"
    assert service.bridge_plist_path().name == "io.sightmesh.bridge.plist"
    assert service.state_dir().name == "sightmesh"


def test_harden_local_storage_makes_state_private(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".local" / "state" / "sightmesh"
    state.mkdir(parents=True)
    log = state / "service.log"
    log.write_text("local", encoding="utf-8")
    state.chmod(0o755)
    log.chmod(0o644)

    service.harden_local_storage()

    assert state.stat().st_mode & 0o777 == 0o700
    assert log.stat().st_mode & 0o777 == 0o600
    assert service.local_storage_is_private() == (True, [])


def test_start_reloads_only_the_owned_launch_agent(monkeypatch, tmp_path) -> None:
    target = tmp_path / "io.sightmesh.cdesktop.plist"
    target.write_text("fixture", encoding="utf-8")
    bridge_target = tmp_path / "io.sightmesh.bridge.plist"
    bridge_target.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge_target)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
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
    assert calls[0][2].endswith("/io.sightmesh.bridge")
    assert calls[1][0:2] == ["launchctl", "bootout"]
    assert calls[1][2].endswith("/io.sightmesh.cdesktop")
    assert calls[2][0:2] == ["launchctl", "bootstrap"]
    assert calls[2][-1] == str(target)
    assert calls[4][0:2] == ["launchctl", "bootstrap"]
    assert calls[4][-1] == str(bridge_target)


def test_bootstrap_retries_transient_launchd_input_output_error(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(service, "_bootout", lambda _label: None)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    outcomes = iter(
        [
            (5, "Bootstrap failed: 5: Input/output error"),
            (0, ""),
        ]
    )

    class Result:
        def __init__(self, returncode, stderr) -> None:
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *_args, **_kwargs: Result(*next(outcomes)),
    )

    service._bootstrap(service.LABEL, tmp_path / "service.plist")


def test_install_restores_previous_definitions_when_reload_fails(
    monkeypatch, tmp_path
) -> None:
    target = tmp_path / "io.sightmesh.cdesktop.plist"
    bridge_target = tmp_path / "io.sightmesh.bridge.plist"
    target.write_bytes(b"old-cdesktop")
    bridge_target.write_bytes(b"old-bridge")
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge_target)
    monkeypatch.setattr(service, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/tmp/{name}")
    attempts = []

    def start(_port) -> None:
        attempts.append((target.read_bytes(), bridge_target.read_bytes()))
        if len(attempts) == 1:
            raise RuntimeError("reload failed")

    monkeypatch.setattr(service, "start", start)

    with pytest.raises(RuntimeError, match="reload failed"):
        service.install(4321)

    assert target.read_bytes() == b"old-cdesktop"
    assert bridge_target.read_bytes() == b"old-bridge"
    assert len(attempts) == 2


def test_wait_until_healthy_retries_until_ready(monkeypatch) -> None:
    results = iter([False, False, True])
    monkeypatch.setattr(service, "is_healthy", lambda _port: next(results))
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    service.wait_until_healthy(4321, timeout=1)


def test_migrate_legacy_state_copies_routing_delivery_and_leases(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    old_config = tmp_path / ".config" / "agent-deck" / "bridge.json"
    old_config.parent.mkdir(parents=True)
    old_config.write_text('{"enabled_workspaces": ["workspace-a"]}', encoding="utf-8")
    old_state = tmp_path / ".local" / "state" / "agent-deck"
    old_state.mkdir(parents=True)
    database = old_state / "delivery.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("create table sample(value text)")
        connection.execute("insert into sample values ('preserved')")
    old_lease = old_state / "leases" / "lease.json"
    old_lease.parent.mkdir(parents=True)
    old_lease.write_text('{"token": "lease-token"}', encoding="utf-8")

    migrated = service.migrate_legacy_state()

    assert set(migrated) == {"routing", "delivery", "leases"}
    assert (
        tmp_path / ".config" / "sightmesh" / "bridge.json"
    ).read_text() == old_config.read_text()
    with sqlite3.connect(
        tmp_path / ".local" / "state" / "sightmesh" / "delivery.sqlite3"
    ) as connection:
        assert (
            connection.execute("select value from sample").fetchone()[0] == "preserved"
        )
    assert (
        tmp_path / ".local" / "state" / "sightmesh" / "leases" / "lease.json"
    ).exists()
