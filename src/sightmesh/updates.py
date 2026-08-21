from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from . import service
from .cdesktop import CdesktopClient, CdesktopError
from .runtime_lock import RUNTIME_LOCK

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
        raise RuntimeError(
            f"Unsupported update state schema: {value.get('schema_version')!r}"
        )
    value.pop("previous_plist", None)
    value.pop("rollback_error", None)
    return value


def _release_for_executable(executable: object, updates_root: Path) -> Path | None:
    if not executable:
        return None
    try:
        relative = (
            Path(str(executable))
            .expanduser()
            .resolve()
            .relative_to(updates_root.resolve())
        )
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return None
    release = updates_root / relative.parts[0]
    if not release.name.startswith("cdesktop-") or not release.is_dir():
        return None
    return release


def prune(*, keep: int = 1, dry_run: bool = False) -> dict[str, Any]:
    if keep < 0:
        raise ValueError("--keep must not be negative")
    updates_root = root_dir()
    if not updates_root.exists():
        return {"removed": [], "retained": [], "dry_run": dry_run}

    state = read_state()
    protected: set[Path] = set()
    for release_state in (state.get("active"), state.get("pending")):
        if isinstance(release_state, dict):
            release = _release_for_executable(
                release_state.get("executable"), updates_root
            )
            if release:
                protected.add(release.resolve())
    releases = sorted(
        (
            path
            for path in updates_root.glob("cdesktop-*")
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained_unprotected = 0
    removed: list[str] = []
    retained: list[str] = []
    for release in releases:
        resolved = release.resolve()
        if resolved in protected or retained_unprotected < keep:
            retained.append(str(release))
            if resolved not in protected:
                retained_unprotected += 1
            continue
        removed.append(str(release))
        if not dry_run:
            shutil.rmtree(release)

    backup_root = updates_root / "backups"
    for legacy_backup in sorted(backup_root.glob("cdesktop-*.plist")):
        if not legacy_backup.is_file() or legacy_backup.is_symlink():
            continue
        removed.append(str(legacy_backup))
        if not dry_run:
            legacy_backup.unlink()

    return {"removed": removed, "retained": retained, "dry_run": dry_run}


def _automatic_prune() -> dict[str, Any]:
    try:
        return prune()
    except (OSError, RuntimeError, ValueError) as exc:
        return {"removed": [], "retained": [], "dry_run": False, "error": str(exc)}


def _sqlite_rows(path: Path, query: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()


def _archive_sqlite(path: Path) -> str:
    archive_dir = service.state_dir() / "legacy"
    archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = archive_dir / f"{path.stem}.{int(time.time())}.sqlite3"
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", dir=archive_dir)
    os.close(handle)
    temporary = Path(temp_name)
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    destination = sqlite3.connect(temporary)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.replace(temporary, target)
    path.unlink()
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    return str(target)


def migrate_native_state(client: CdesktopClient) -> dict[str, Any]:
    result: dict[str, Any] = {
        "parent_relationships": 0,
        "pending_commands": 0,
        "archives": [],
    }
    relationships = service.state_dir() / "relationships.sqlite3"
    if relationships.exists():
        rows = _sqlite_rows(
            relationships,
            "SELECT child_session_id, parent_session_id FROM parent_edges",
        )
        for row in rows:
            client.set_parent(row["child_session_id"], row["parent_session_id"])
        result["parent_relationships"] = len(rows)
        result["archives"].append(_archive_sqlite(relationships))

    delivery = service.state_dir() / "delivery.sqlite3"
    if delivery.exists():
        rows = _sqlite_rows(
            delivery,
            "SELECT session_id, prompt, idempotency_key FROM deliveries "
            "WHERE status IN ('pending', 'inflight') ORDER BY created_at",
        )
        for row in rows:
            if not row["prompt"]:
                raise RuntimeError(
                    f"Legacy delivery {row['idempotency_key']} has no retained prompt"
                )
            client.send(
                row["session_id"],
                row["prompt"],
                dedupe_key=f"legacy:{row['idempotency_key']}",
            )
        result["pending_commands"] = len(rows)
        result["archives"].append(_archive_sqlite(delivery))
    return result


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


def _platform_directory() -> str:
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    operating_system = {
        "darwin": "macos",
        "linux": "linux",
        "windows": "windows",
    }.get(system)
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if not operating_system or machine not in {
        "arm64",
        "aarch64",
        "x86_64",
        "amd64",
    }:
        raise RuntimeError(f"Unsupported cdesktop update platform: {system}-{machine}")
    return f"{operating_system}-{architecture}"


def _release_asset_url(name: str) -> str:
    runtime = RUNTIME_LOCK.cdesktop
    return (
        f"https://github.com/{runtime.repository}/releases/download/"
        f"{runtime.tag}/{name}"
    )


def _download_backend_archive(archive: Path, temporary: Path) -> None:
    platform_name = _platform_directory()
    asset_name = f"cdesktop-{platform_name}.zip"
    manifest = temporary / "manifest.json"
    downloaded = temporary / asset_name
    _download(_release_asset_url("manifest.json"), manifest)
    try:
        assets = json.loads(manifest.read_text(encoding="utf-8"))["assets"]
        expected = assets[asset_name]["sha256"]
    except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Staged cdesktop release manifest lacks {asset_name}"
        ) from exc
    if not isinstance(expected, str) or len(expected) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in expected
    ):
        raise RuntimeError(
            f"Staged cdesktop release manifest has an invalid checksum for {asset_name}"
        )
    _download(_release_asset_url(asset_name), downloaded)
    digest = _sha256(downloaded)
    if digest.casefold() != expected.casefold():
        raise RuntimeError(
            f"Staged cdesktop backend archive checksum mismatch: got {digest}, "
            f"expected {expected}"
        )
    downloaded.replace(archive)


def _validate_package_payload(package_root: Path, temporary: Path) -> str:
    archive = package_root / "dist" / _platform_directory() / "cdesktop.zip"
    if not archive.is_file():
        _download_backend_archive(archive, temporary)
    try:
        with zipfile.ZipFile(archive) as bundle:
            expected = "cdesktop.exe" if platform.system() == "Windows" else "cdesktop"
            members = {Path(name).name for name in bundle.namelist()}
            if expected not in members:
                raise RuntimeError(
                    f"Staged backend archive does not contain {expected}: {archive}"
                )
            corrupt_member = bundle.testzip()
            if corrupt_member:
                raise RuntimeError(
                    f"Staged backend archive has a corrupt member {corrupt_member}: {archive}"
                )
    except zipfile.BadZipFile as exc:
        raise RuntimeError(
            f"Staged cdesktop backend archive is invalid: {archive}"
        ) from exc
    return str(archive)


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
    current_state = read_state()
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
        backend_archive = _validate_package_payload(
            release / "node_modules" / "cdesktop", temporary
        )
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
                "backend_archive": backend_archive,
                "staged_at": now,
            },
            "active": current_state.get("active"),
            "last_error": None,
            "updated_at": now,
        }
        _write_json_atomic(state_path(), state)
        state["cleanup"] = _automatic_prune()
        return state
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def activity(client: CdesktopClient) -> dict[str, Any]:
    approvals = client.pending_approvals()
    running: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    for workspace in client.workspaces():
        if workspace.get("archived"):
            continue
        for session in client.sessions(str(workspace["id"])):
            session_running = False
            for process in client.execution_processes(str(session["id"])):
                if (
                    process.get("status") == "running"
                    and process.get("run_reason") != "devserver"
                ):
                    session_running = True
                    running.append(
                        {
                            "workspace_id": workspace["id"],
                            "session_id": session["id"],
                            "execution_process_id": process.get("id"),
                            "run_reason": process.get("run_reason"),
                        }
                    )
            if session_running:
                continue
            try:
                queue_status = client.queue_status(str(session["id"]))
            except CdesktopError as exc:
                unreadable.append(
                    {
                        "workspace_id": workspace["id"],
                        "session_id": session["id"],
                        "error": str(exc),
                    }
                )
            else:
                if queue_status.get("status") == "queued":
                    queued.append(
                        {
                            "workspace_id": workspace["id"],
                            "session_id": session["id"],
                        }
                    )
    return {
        # A queued follow-up has no live executor or approval responder to
        # preserve. It remains durable in cdesktop's database and can resume
        # after the replacement backend starts, so it must not deadlock an
        # otherwise idle update.
        "idle": not running and not approvals and not unreadable,
        "running": running,
        "queued_follow_ups": queued,
        "unreadable_sessions": unreadable,
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
    bridge_restored = False
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
        new_definition = plistlib.dumps(
            service.definition(port, executable=Path(str(pending["executable"])))
        )
        service._write_bytes_atomic(target, new_definition)
        activating = {
            **state,
            "status": "activating",
            "bootstrap_without_drain": bootstrap_without_drain,
            "updated_at": time.time(),
        }
        _write_json_atomic(state_path(), activating)
        native_state_migration: dict[str, Any] | None = None
        try:
            service._bootstrap(service.LABEL, target)
            service.wait_until_healthy(port)
            updated_client = CdesktopClient(service.service_url(port))
            info = updated_client.info()
            running_version = str(info.get("version") or "")
            if str(pending["version"]) not in running_version:
                raise RuntimeError(
                    f"Updated cdesktop reports {running_version!r}, expected "
                    f"{pending['version']!r}"
                )
            native_state_migration = migrate_native_state(updated_client)
            _restore_bridge()
            bridge_restored = True
        except Exception as update_error:
            drain_enabled = False
            if service.is_healthy(port):
                try:
                    CdesktopClient(service.service_url(port)).set_update_drain(0)
                except Exception:
                    pass
                try:
                    _restore_bridge()
                    bridge_restored = True
                except Exception:
                    pass
            failed = {
                **activating,
                "status": "failed",
                "pending": None,
                "failed_package": pending,
                "last_error": str(update_error),
                "updated_at": time.time(),
            }
            _write_json_atomic(state_path(), failed)
            raise RuntimeError(
                f"Update failed and will not be retried automatically: {update_error}"
            ) from update_error

        active = {
            "schema_version": SCHEMA_VERSION,
            "status": "active",
            "pending": None,
            "active": {
                **pending,
                "activated_at": time.time(),
            },
            "last_error": None,
            "native_state_migration": native_state_migration,
            "updated_at": time.time(),
        }
        _write_json_atomic(state_path(), active)
        active["cleanup"] = _automatic_prune()
        return {**active, "action": "activated"}
    except Exception:
        if drain_enabled and service.is_healthy(port):
            try:
                client.set_update_drain(0)
            except Exception:
                pass
        if not bridge_restored and service.is_healthy(port):
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
