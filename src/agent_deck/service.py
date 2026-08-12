from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


LABEL = "io.agent-deck.cdesktop"
BRIDGE_LABEL = "io.agent-deck.bridge"
DEFAULT_PORT = 3210


def service_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def bridge_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{BRIDGE_LABEL}.plist"


def state_dir() -> Path:
    return Path.home() / ".local" / "state" / "agent-deck"


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


def definition(port: int = DEFAULT_PORT) -> dict[str, Any]:
    executable = shutil.which("cdesktop")
    if not executable:
        raise RuntimeError("cdesktop is not installed")
    logs = state_dir()
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [executable],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "DISABLE_WORKTREE_CLEANUP": "1",
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
        "StandardOutPath": str(logs / "cdesktop.stdout.log"),
        "StandardErrorPath": str(logs / "cdesktop.stderr.log"),
    }


def bridge_definition(port: int = DEFAULT_PORT) -> dict[str, Any]:
    executable = shutil.which("agent-deck")
    if not executable:
        raise RuntimeError("agent-deck is not installed")
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
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
        "StandardOutPath": str(logs / "bridge.stdout.log"),
        "StandardErrorPath": str(logs / "bridge.stderr.log"),
    }


def install(port: int = DEFAULT_PORT, start_now: bool = True) -> Path:
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(definition(port)))
    bridge_target = bridge_plist_path()
    bridge_target.write_bytes(plistlib.dumps(bridge_definition(port)))
    if start_now:
        start(port)
    return target


def _bootstrap(label: str, service_path: Path) -> None:
    subprocess.run(
        ["launchctl", "bootout", _service_target(label)],
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", _domain(), str(service_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())


def wait_until_healthy(port: int = DEFAULT_PORT, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_healthy(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"Managed cdesktop did not become healthy at {service_url(port)}")


def start(port: int = DEFAULT_PORT) -> None:
    target = plist_path()
    bridge_target = bridge_plist_path()
    for service_path in (target, bridge_target):
        if not service_path.exists():
            raise RuntimeError(f"Service is not installed: {service_path}")
    _bootstrap(LABEL, target)
    wait_until_healthy(port)
    _bootstrap(BRIDGE_LABEL, bridge_target)


def stop() -> None:
    for label in (BRIDGE_LABEL, LABEL):
        result = subprocess.run(
            ["launchctl", "bootout", _service_target(label)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and "No such process" not in result.stderr:
            raise RuntimeError((result.stderr or result.stdout).strip())


def uninstall() -> None:
    stop()
    target = plist_path()
    if target.exists():
        target.unlink()
    bridge_target = bridge_plist_path()
    if bridge_target.exists():
        bridge_target.unlink()


def status(port: int = DEFAULT_PORT) -> dict[str, Any]:
    result = subprocess.run(
        ["launchctl", "print", _service_target()],
        capture_output=True,
        text=True,
        check=False,
    )
    bridge_result = subprocess.run(
        ["launchctl", "print", _service_target(BRIDGE_LABEL)],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "installed": plist_path().exists(),
        "loaded": result.returncode == 0,
        "bridge_installed": bridge_plist_path().exists(),
        "bridge_loaded": bridge_result.returncode == 0,
        "healthy": is_healthy(port),
        "url": service_url(port),
        "plist": str(plist_path()),
        "bridge_plist": str(bridge_plist_path()),
        "logs": str(state_dir()),
    }


def open_ui(port: int = DEFAULT_PORT) -> None:
    if not is_healthy(port):
        raise RuntimeError(f"Managed cdesktop is not healthy at {service_url(port)}")
    webbrowser.open(service_url(port))
