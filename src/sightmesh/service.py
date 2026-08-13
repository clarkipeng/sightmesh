from __future__ import annotations

import os
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

LABEL = "io.sightmesh.cdesktop"
BRIDGE_LABEL = "io.sightmesh.bridge"
UPDATER_LABEL = "io.sightmesh.updater"
LEGACY_LABEL = "io.agent-deck.cdesktop"
LEGACY_BRIDGE_LABEL = "io.agent-deck.bridge"
DEFAULT_PORT = 3210


def service_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def bridge_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{BRIDGE_LABEL}.plist"


def updater_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{UPDATER_LABEL}.plist"


def state_dir() -> Path:
    return Path.home() / ".local" / "state" / "sightmesh"


def harden_local_storage() -> list[str]:
    secured: list[str] = []
    private_trees = [
        state_dir(),
        Path.home() / ".config" / "sightmesh",
    ]
    for root in private_trees:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        secured.append(str(root))
        for path in root.rglob("*"):
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)

    private_roots = [
        Path.home() / ".repowire",
        Path.home() / "Library" / "Application Support" / "ai.cdesktop.cdesktop",
        Path.home() / ".local" / "share" / "sightmesh",
    ]
    for root in private_roots:
        if root.exists():
            root.chmod(0o700)
            secured.append(str(root))
    return secured


def local_storage_is_private() -> tuple[bool, list[str]]:
    insecure: list[str] = []
    for root in (
        state_dir(),
        Path.home() / ".config" / "sightmesh",
        Path.home() / ".repowire",
        Path.home() / "Library" / "Application Support" / "ai.cdesktop.cdesktop",
        Path.home() / ".local" / "share" / "sightmesh",
    ):
        if root.exists() and root.stat().st_mode & 0o077:
            insecure.append(str(root))
    return not insecure, insecure


def legacy_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"


def legacy_bridge_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_BRIDGE_LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target(label: str = LABEL) -> str:
    return f"{_domain()}/{label}"


def is_healthy(port: int = DEFAULT_PORT) -> bool:
    try:
        with urlopen(f"{service_url(port)}/api/health", timeout=1) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def definition(
    port: int = DEFAULT_PORT, *, executable: Path | str | None = None
) -> dict[str, Any]:
    resolved_executable = str(executable) if executable else shutil.which("cdesktop")
    if not resolved_executable:
        raise RuntimeError("cdesktop is not installed")
    logs = state_dir()
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [resolved_executable],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "Umask": 0o077,
        "EnvironmentVariables": {
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
        "StandardOutPath": str(logs / "cdesktop.stdout.log"),
        "StandardErrorPath": str(logs / "cdesktop.stderr.log"),
    }


def bridge_definition(port: int = DEFAULT_PORT) -> dict[str, Any]:
    executable = shutil.which("sightmesh")
    if not executable:
        raise RuntimeError("sightmesh is not installed")
    logs = state_dir()
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": BRIDGE_LABEL,
        "ProgramArguments": [
            executable,
            "--url",
            service_url(port),
            "bridge",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "Umask": 0o077,
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
        "StandardOutPath": str(logs / "bridge.stdout.log"),
        "StandardErrorPath": str(logs / "bridge.stderr.log"),
    }


def updater_definition(port: int = DEFAULT_PORT) -> dict[str, Any]:
    executable = shutil.which("sightmesh")
    if not executable:
        raise RuntimeError("sightmesh is not installed")
    logs = state_dir()
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": UPDATER_LABEL,
        "ProgramArguments": [
            executable,
            "--url",
            service_url(port),
            "update",
            "activate",
            "--if-idle",
            "--quiet",
        ],
        "RunAtLoad": True,
        "StartInterval": 5,
        "ProcessType": "Background",
        "Umask": 0o077,
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
        "StandardOutPath": str(logs / "updater.stdout.log"),
        "StandardErrorPath": str(logs / "updater.stderr.log"),
    }


def install_updater(port: int = DEFAULT_PORT, start_now: bool = True) -> Path:
    harden_local_storage()
    target = updater_plist_path()
    previous = target.read_bytes() if target.exists() else None
    _write_bytes_atomic(target, plistlib.dumps(updater_definition(port)))
    if start_now:
        try:
            _bootstrap(UPDATER_LABEL, target)
        except RuntimeError:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                _write_bytes_atomic(target, previous)
                _bootstrap(UPDATER_LABEL, target)
            raise
    return target


def install(port: int = DEFAULT_PORT, start_now: bool = True) -> Path:
    harden_local_storage()
    target = plist_path()
    bridge_target = bridge_plist_path()
    updater_target = updater_plist_path()
    previous = {
        target: target.read_bytes() if target.exists() else None,
        bridge_target: bridge_target.read_bytes() if bridge_target.exists() else None,
        updater_target: updater_target.read_bytes() if updater_target.exists() else None,
    }
    _write_bytes_atomic(target, plistlib.dumps(definition(port)))
    _write_bytes_atomic(bridge_target, plistlib.dumps(bridge_definition(port)))
    _write_bytes_atomic(updater_target, plistlib.dumps(updater_definition(port)))
    if start_now:
        try:
            start(port)
        except RuntimeError as install_error:
            for path, payload in previous.items():
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_bytes_atomic(path, payload)
            if previous[target] is not None and previous[bridge_target] is not None:
                try:
                    _bootstrap(LABEL, target)
                    wait_until_healthy(port)
                    _bootstrap(BRIDGE_LABEL, bridge_target)
                    if previous[updater_target] is not None:
                        _bootstrap(UPDATER_LABEL, updater_target)
                except RuntimeError as rollback_error:
                    raise RuntimeError(
                        "Managed service reload failed and the previous definition "
                        f"could not be restored: {rollback_error}"
                    ) from install_error
            raise
    return target


def migrate_legacy_state() -> dict[str, str]:
    migrated: dict[str, str] = {}
    old_config = Path.home() / ".config" / "agent-deck" / "bridge.json"
    new_config = Path.home() / ".config" / "sightmesh" / "bridge.json"
    if old_config.exists() and not new_config.exists():
        _copy_atomic(old_config, new_config)
        migrated["routing"] = str(new_config)

    old_state = Path.home() / ".local" / "state" / "agent-deck"
    old_delivery = old_state / "delivery.sqlite3"
    new_delivery = state_dir() / "delivery.sqlite3"
    if old_delivery.exists() and not new_delivery.exists():
        new_delivery.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix="delivery.", suffix=".sqlite3", dir=new_delivery.parent
        )
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            source = sqlite3.connect(f"file:{old_delivery}?mode=ro", uri=True)
            destination = sqlite3.connect(temp_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            os.replace(temp_path, new_delivery)
        finally:
            temp_path.unlink(missing_ok=True)
        migrated["delivery"] = str(new_delivery)

    old_leases = old_state / "leases"
    new_leases = state_dir() / "leases"
    if old_leases.exists():
        for source in sorted(old_leases.rglob("*.json")):
            relative = source.relative_to(old_leases)
            target = new_leases / relative
            if not target.exists():
                _copy_atomic(source, target)
        if new_leases.exists():
            migrated["leases"] = str(new_leases)
    return migrated


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_bytes_atomic(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def _bootstrap(label: str, service_path: Path) -> None:
    _bootout(label)
    _wait_until_unloaded(label)
    for attempt in range(3):
        result = subprocess.run(
            ["launchctl", "bootstrap", _domain(), str(service_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout).strip()
        if attempt == 2 or "Input/output error" not in detail:
            raise RuntimeError(detail)
        time.sleep(0.2 * (attempt + 1))


def _wait_until_unloaded(label: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _loaded(label):
            return
        time.sleep(0.05)
    raise RuntimeError(f"Managed launchd service did not unload: {label}")


def _bootout(label: str) -> None:
    result = subprocess.run(
        ["launchctl", "bootout", _service_target(label)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and "No such process" not in result.stderr:
        raise RuntimeError((result.stderr or result.stdout).strip())


def _loaded(label: str) -> bool:
    result = subprocess.run(
        ["launchctl", "print", _service_target(label)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def wait_until_healthy(port: int = DEFAULT_PORT, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_healthy(port):
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"Managed cdesktop did not become healthy at {service_url(port)}"
    )


def start(port: int = DEFAULT_PORT) -> None:
    target = plist_path()
    bridge_target = bridge_plist_path()
    updater_target = updater_plist_path()
    for service_path in (target, bridge_target, updater_target):
        if not service_path.exists():
            raise RuntimeError(f"Service is not installed: {service_path}")
    _bootout(UPDATER_LABEL)
    _wait_until_unloaded(UPDATER_LABEL)
    _bootout(BRIDGE_LABEL)
    _wait_until_unloaded(BRIDGE_LABEL)
    _bootstrap(LABEL, target)
    wait_until_healthy(port)
    _bootstrap(BRIDGE_LABEL, bridge_target)
    _bootstrap(UPDATER_LABEL, updater_target)


def stop() -> None:
    for label in (UPDATER_LABEL, BRIDGE_LABEL, LABEL):
        _bootout(label)


def cutover(port: int = DEFAULT_PORT) -> dict[str, Any]:
    install(port, start_now=False)
    legacy_cdesktop_loaded = _loaded(LEGACY_LABEL)
    legacy_bridge_loaded = _loaded(LEGACY_BRIDGE_LABEL)
    backups: dict[str, str] = {}
    for source in (legacy_plist_path(), legacy_bridge_plist_path()):
        if source.exists():
            backup = source.with_suffix(source.suffix + ".sightmesh-backup")
            _copy_atomic(source, backup)
            backups[source.name] = str(backup)

    _bootout(LEGACY_BRIDGE_LABEL)
    migrated = migrate_legacy_state()
    _bootout(LEGACY_LABEL)
    try:
        _bootstrap(LABEL, plist_path())
        wait_until_healthy(port)
        _bootstrap(BRIDGE_LABEL, bridge_plist_path())
        if not _loaded(BRIDGE_LABEL):
            raise RuntimeError("SightMesh bridge did not remain loaded")
        _bootstrap(UPDATER_LABEL, updater_plist_path())
    except Exception:
        _bootout(UPDATER_LABEL)
        _bootout(BRIDGE_LABEL)
        _bootout(LABEL)
        if legacy_cdesktop_loaded and legacy_plist_path().exists():
            _bootstrap(LEGACY_LABEL, legacy_plist_path())
            wait_until_healthy(port)
        if legacy_bridge_loaded and legacy_bridge_plist_path().exists():
            _bootstrap(LEGACY_BRIDGE_LABEL, legacy_bridge_plist_path())
        raise

    legacy_plist_path().unlink(missing_ok=True)
    legacy_bridge_plist_path().unlink(missing_ok=True)
    return {
        **status(port),
        "legacy_backups": backups,
        "migrated_state": migrated,
        "legacy_labels_loaded": {
            LEGACY_LABEL: _loaded(LEGACY_LABEL),
            LEGACY_BRIDGE_LABEL: _loaded(LEGACY_BRIDGE_LABEL),
        },
    }


def uninstall() -> None:
    stop()
    target = plist_path()
    if target.exists():
        target.unlink()
    bridge_target = bridge_plist_path()
    if bridge_target.exists():
        bridge_target.unlink()
    updater_target = updater_plist_path()
    if updater_target.exists():
        updater_target.unlink()


def status(port: int = DEFAULT_PORT) -> dict[str, Any]:
    return {
        "installed": plist_path().exists(),
        "loaded": _loaded(LABEL),
        "bridge_installed": bridge_plist_path().exists(),
        "bridge_loaded": _loaded(BRIDGE_LABEL),
        "updater_installed": updater_plist_path().exists(),
        "updater_loaded": _loaded(UPDATER_LABEL),
        "healthy": is_healthy(port),
        "url": service_url(port),
        "plist": str(plist_path()),
        "bridge_plist": str(bridge_plist_path()),
        "updater_plist": str(updater_plist_path()),
        "logs": str(state_dir()),
    }


def open_ui(port: int = DEFAULT_PORT) -> None:
    if not is_healthy(port):
        raise RuntimeError(f"Managed cdesktop is not healthy at {service_url(port)}")
    webbrowser.open(service_url(port))
