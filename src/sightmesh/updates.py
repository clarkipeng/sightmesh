from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from urllib.request import urlopen

from . import service
from .cdesktop import CdesktopClient

SCHEMA_VERSION = 1
QUIET_SECONDS = 2.0
UNDRAINED_BOOTSTRAP_VERSIONS = {"0.2.3-sightmesh.1"}


def root_dir() -> Path:
    return Path.home() / ".local" / "share" / "sightmesh" / "updates"


def state_path() -> Path:
    return service.state_dir() / "update.json"


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "status": "idle", "pending": None}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported update state schema: {value.get('schema_version')!r}")
    return value


@contextmanager
def _activation_lock() -> Iterator[None]:
    path = service.state_dir() / "update.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.flush()
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _download(source: str, destination: Path) -> None:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        with urlopen(source, timeout=60) as response, destination.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        return
    if parsed.scheme:
        raise ValueError(f"Unsupported update package URL scheme: {parsed.scheme}")
    local = Path(source).expanduser().resolve()
    if not local.is_file():
        raise ValueError(f"Update package does not exist: {local}")
    shutil.copy2(local, destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_executable(executable: Path, version: str) -> str:
    result = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    detail = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Staged cdesktop failed version validation: {detail}")
    if version not in detail:
        raise RuntimeError(
            f"Staged cdesktop reports {detail!r}, expected version containing {version!r}"
        )
    return detail


def stage(
    source: str,
    version: str,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if not version.strip() or any(character in version for character in "/\\"):
        raise ValueError("Update version must be a non-empty path-safe label")
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"} and not expected_sha256:
        raise ValueError("Remote update packages require --sha256")

    updates_root = root_dir()
    updates_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    updates_root.chmod(0o700)
    temporary = Path(tempfile.mkdtemp(prefix="stage.", dir=updates_root))
    archive = temporary / "package.tgz"
    try:
        _download(source, archive)
        digest = _sha256(archive)
        if expected_sha256 and digest.casefold() != expected_sha256.casefold():
            raise ValueError(
                f"Update package checksum mismatch: got {digest}, expected {expected_sha256}"
            )
        release = updates_root / f"cdesktop-{version}-{digest[:12]}"
        if not release.exists():
            candidate = temporary / "release"
            result = subprocess.run(
                [
                    "npm",
                    "install",
                    "--prefix",
                    str(candidate),
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    str(archive),
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Could not stage cdesktop: "
                    + (result.stderr or result.stdout).strip()
                )
            try:
                candidate.rename(release)
            except FileExistsError:
                pass
        executable = release / "node_modules" / ".bin" / "cdesktop"
        reported_version = _validate_executable(executable, version)
        now = time.time()
        state = {
            "schema_version": SCHEMA_VERSION,
            "status": "staged",
            "pending": {
                "version": version,
                "source": source,
                "sha256": digest,
                "executable": str(executable),
                "reported_version": reported_version,
                "staged_at": now,
            },
            "last_error": None,
            "updated_at": now,
        }
        _write_json_atomic(state_path(), state)
        return state
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def activity(client: CdesktopClient) -> dict[str, Any]:
    approvals = client.pending_approvals()
    running: list[dict[str, Any]] = []
    for workspace in client.workspaces():
        if workspace.get("archived"):
            continue
        for session in client.sessions(str(workspace["id"])):
            for process in client.execution_processes(str(session["id"])):
                if (
                    process.get("status") == "running"
                    and process.get("run_reason") != "devserver"
                ):
                    running.append(
                        {
                            "workspace_id": workspace["id"],
                            "session_id": session["id"],
                            "execution_process_id": process.get("id"),
                            "run_reason": process.get("run_reason"),
                        }
                    )
    return {
        "idle": not running and not approvals,
        "running": running,
        "pending_approvals": [
            {
                "approval_id": item.get("approval_id"),
                "session_id": item.get("session_id"),
                "is_question": bool(item.get("is_question")),
            }
            for item in approvals
        ],
    }


def _restore_bridge() -> None:
    service._bootstrap(service.BRIDGE_LABEL, service.bridge_plist_path())


def activate_if_idle(client: CdesktopClient, *, port: int) -> dict[str, Any]:
    try:
        with _activation_lock():
            return _activate_locked(client, port=port)
    except BlockingIOError:
        return {**read_state(), "action": "activation-already-running"}


def _activate_locked(client: CdesktopClient, *, port: int) -> dict[str, Any]:
    state = read_state()
    pending = state.get("pending")
    if not pending:
        return {**state, "action": "no-staged-update"}

    current_activity = activity(client)
    if not current_activity["idle"]:
        waiting = {
            **state,
            "status": "waiting-for-idle",
            "activity": current_activity,
            "updated_at": time.time(),
        }
        _write_json_atomic(state_path(), waiting)
        return {**waiting, "action": "waiting-for-idle"}

    service._bootout(service.BRIDGE_LABEL)
    service._wait_until_unloaded(service.BRIDGE_LABEL)
    drain_enabled = False
    bootstrap_without_drain = False
    try:
        running_version = str(client.info().get("version") or "")
        try:
            client.set_update_drain(15)
            drain_enabled = True
        except Exception:
            if running_version not in UNDRAINED_BOOTSTRAP_VERSIONS:
                raise
            bootstrap_without_drain = True
        time.sleep(QUIET_SECONDS)
        current_activity = activity(client)
        if not current_activity["idle"]:
            client.set_update_drain(0)
            drain_enabled = False
            _restore_bridge()
            waiting = {
                **state,
                "status": "waiting-for-idle",
                "activity": current_activity,
                "updated_at": time.time(),
            }
            _write_json_atomic(state_path(), waiting)
            return {**waiting, "action": "activity-resumed"}

        target = service.plist_path()
        previous_definition = target.read_bytes()
        backup_dir = root_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_dir / f"cdesktop-{int(time.time())}.plist"
        service._write_bytes_atomic(backup, previous_definition)
        new_definition = plistlib.dumps(
            service.definition(port, executable=Path(str(pending["executable"])))
        )
        service._write_bytes_atomic(target, new_definition)
        activating = {
            **state,
            "status": "activating",
            "previous_plist": str(backup),
            "bootstrap_without_drain": bootstrap_without_drain,
            "updated_at": time.time(),
        }
        _write_json_atomic(state_path(), activating)
        try:
            service._bootstrap(service.LABEL, target)
            service.wait_until_healthy(port)
            info = CdesktopClient(service.service_url(port)).info()
            running_version = str(info.get("version") or "")
            if str(pending["version"]) not in running_version:
                raise RuntimeError(
                    f"Updated cdesktop reports {running_version!r}, expected "
                    f"{pending['version']!r}"
                )
            _restore_bridge()
        except Exception as update_error:
            try:
                client.set_update_drain(0)
            except Exception:
                pass
            drain_enabled = False
            service._write_bytes_atomic(target, previous_definition)
            rollback_error: Exception | None = None
            try:
                service._bootstrap(service.LABEL, target)
                service.wait_until_healthy(port)
                _restore_bridge()
            except Exception as exc:
                rollback_error = exc
            failed = {
                **activating,
                "status": "failed",
                "last_error": str(update_error),
                "rollback_error": str(rollback_error) if rollback_error else None,
                "updated_at": time.time(),
            }
            _write_json_atomic(state_path(), failed)
            if rollback_error:
                raise RuntimeError(
                    f"Update failed ({update_error}) and rollback failed ({rollback_error})"
                ) from update_error
            raise RuntimeError(
                f"Update failed and the previous cdesktop was restored: {update_error}"
            ) from update_error

        active = {
            "schema_version": SCHEMA_VERSION,
            "status": "active",
            "pending": None,
            "active": {
                **pending,
                "activated_at": time.time(),
            },
            "previous_plist": str(backup),
            "last_error": None,
            "updated_at": time.time(),
        }
        _write_json_atomic(state_path(), active)
        return {**active, "action": "activated"}
    except Exception:
        if drain_enabled and service.is_healthy(port):
            try:
                client.set_update_drain(0)
            except Exception:
                pass
        if not service._loaded(service.BRIDGE_LABEL) and service.is_healthy(port):
            _restore_bridge()
        raise


def cancel() -> dict[str, Any]:
    state = read_state()
    pending = state.get("pending")
    cancelled = {
        **state,
        "status": "cancelled" if pending else state.get("status", "idle"),
        "pending": None,
        "cancelled": pending,
        "updated_at": time.time(),
    }
    _write_json_atomic(state_path(), cancelled)
    return cancelled
