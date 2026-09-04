import hashlib
import io
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

from sightmesh import service, updates
from sightmesh.cdesktop import CdesktopError
from sightmesh.fence import assert_external_io_allowed


class FakeClock:
    """Virtual monotonic clock.

    A drain TTL only means anything if a test can let time pass, and no
    real-time test can wait out a 30-second TTL. Advancing this clock from
    `sleep` is what lets the suite observe a lapsed drain at all.
    """

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = float(now)

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


@pytest.mark.parametrize("contents", ["[]", "null"])
def test_read_state_rejects_valid_json_that_is_not_an_object(tmp_path, monkeypatch, contents):
    """A syntactically valid but malformed state follows the normal error path."""
    path = tmp_path / "update.json"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(updates, "state_path", lambda: path)

    with pytest.raises(RuntimeError, match="expected an object"):
        updates.read_state()


class FakeClient:
    #: cdesktop refuses anything longer; see `CdesktopClient.set_update_drain`.
    DRAIN_CAP_SECONDS = 30

    def __init__(
        self,
        *,
        running: bool = False,
        approvals: bool = False,
        version: str = "0.2.4-sightmesh.1",
        drain_supported: bool = True,
        queued: bool = False,
        clock: FakeClock | None = None,
        turn_seconds: float = 0.0,
    ) -> None:
        self.running = running
        self.approvals = approvals
        self.version = version
        self.drain_supported = drain_supported
        self.queued = queued
        self.drain_calls = []
        self.parents = []
        self.sent = []
        self.clock = clock or FakeClock()
        #: A host that keeps launching work: every turn runs this long, and a
        #: new one starts the moment admission is no longer refused. Zero
        #: models a host whose activity a test drives directly.
        self.turn_seconds = float(turn_seconds)
        self.drain_expires_at: float | None = None
        self.busy_until = (
            self.clock.monotonic() + self.turn_seconds
            if running and self.turn_seconds
            else None
        )

    def set_update_drain(self, seconds):
        if not self.drain_supported:
            raise RuntimeError("HTTP 404")
        # The executor's own bound. Requesting more is rejected there, so a
        # kernel that tries to cover a long wait with one call gets nothing.
        if not 0 <= seconds <= self.DRAIN_CAP_SECONDS:
            raise ValueError("Update drain seconds must be between 0 and 30")
        self.drain_calls.append(seconds)
        self.drain_expires_at = (
            self.clock.monotonic() + seconds if seconds else None
        )
        return {"draining": seconds > 0}

    @property
    def admission_refused(self) -> bool:
        return (
            self.drain_expires_at is not None
            and self.clock.monotonic() < self.drain_expires_at
        )

    def pending_approvals(self):
        assert_external_io_allowed()
        if not self.approvals:
            return []
        return [
            {
                "approval_id": "approval-1",
                "session_id": "session-1",
                "is_question": True,
            }
        ]

    def workspaces(self):
        return [{"id": "workspace-1", "archived": False}]

    def sessions(self, _workspace_id):
        return [{"id": "session-1"}]

    def execution_processes(self, _session_id=None, *, status=None):
        assert_external_io_allowed()
        assert status in {None, "running"}
        if self.turn_seconds:
            if not self.admission_refused:
                # Admission is open again, so the host starts another turn.
                self.busy_until = self.clock.monotonic() + self.turn_seconds
            self.running = self.clock.monotonic() < (self.busy_until or 0.0)
        return [
            {
                "id": "process-1",
                "status": "running" if self.running else "completed",
                "run_reason": "codingagent",
                "session_id": "session-1",
                "session_name": "worker-one",
                "workspace_id": "workspace-1",
            }
        ]

    def queue_status(self, _session_id):
        return {"status": "queued" if self.queued else "empty"}

    def info(self):
        return {"version": self.version}

    def set_parent(self, session_id, parent_session_id):
        self.parents.append((session_id, parent_session_id))
        return {"id": session_id, "parent_session_id": parent_session_id}

    def send(self, session_id, prompt, sender_session=None, *, dedupe_key=None):
        self.sent.append((session_id, prompt, sender_session, dedupe_key))
        return {"state": "pending"}


def isolated_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(updates, "root_dir", lambda: tmp_path / "releases")
    monkeypatch.setattr(
        updates, "state_path", lambda: tmp_path / "state" / "update.json"
    )
    monkeypatch.setattr(service, "state_dir", lambda: tmp_path / "state")


def test_remote_stage_requires_checksum() -> None:
    with pytest.raises(ValueError, match="require --sha256"):
        updates.stage("https://example.test/cdesktop.tgz", "0.2.4-sightmesh.1")


def test_stage_verifies_and_installs_into_versioned_directory(
    monkeypatch, tmp_path
) -> None:
    isolated_state(monkeypatch, tmp_path)
    package = tmp_path / "cdesktop.tgz"
    package.write_bytes(b"verified-package")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()

    class Result:
        returncode = 0
        stdout = "cdesktop/0.2.4-sightmesh.1 darwin-arm64"
        stderr = ""

    def run(command, **_kwargs):
        if command[0] == "npm":
            prefix = Path(command[command.index("--prefix") + 1])
            executable = prefix / "node_modules" / ".bin" / "cdesktop"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture", encoding="utf-8")
            archive = (
                prefix
                / "node_modules"
                / "cdesktop"
                / "dist"
                / "macos-arm64"
                / "cdesktop.zip"
            )
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("cdesktop", b"backend")
        return Result()

    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setattr(updates, "_platform_directory", lambda: "macos-arm64")

    state = updates.stage(
        str(package),
        "0.2.4-sightmesh.1",
        expected_sha256=digest,
    )

    assert state["status"] == "staged"
    assert state["pending"]["sha256"] == digest
    assert Path(state["pending"]["executable"]).exists()
    assert Path(state["pending"]["backend_archive"]).exists()
    assert updates.read_state()["pending"]["version"] == "0.2.4-sightmesh.1"


def test_stage_keeps_active_release_metadata(monkeypatch, tmp_path) -> None:
    isolated_state(monkeypatch, tmp_path)
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "active",
            "pending": None,
            "active": {"version": "current", "executable": "/tmp/current"},
        },
    )
    package = tmp_path / "cdesktop.tgz"
    package.write_bytes(b"verified-package")

    class Result:
        returncode = 0
        stdout = "cdesktop/next darwin-arm64"
        stderr = ""

    def run(command, **_kwargs):
        if command[0] == "npm":
            prefix = Path(command[command.index("--prefix") + 1])
            executable = prefix / "node_modules" / ".bin" / "cdesktop"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture", encoding="utf-8")
            archive = (
                prefix
                / "node_modules"
                / "cdesktop"
                / "dist"
                / "macos-arm64"
                / "cdesktop.zip"
            )
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("cdesktop", b"backend")
        return Result()

    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setattr(updates, "_platform_directory", lambda: "macos-arm64")

    state = updates.stage(str(package), "next")

    assert state["active"]["version"] == "current"
    assert "previous_plist" not in state


def test_stage_downloads_and_verifies_missing_backend_archive(monkeypatch, tmp_path) -> None:
    """Wrapper-only packages recover their backend from the locked release assets."""
    isolated_state(monkeypatch, tmp_path)
    package = tmp_path / "cdesktop.tgz"
    package.write_bytes(b"wrapper-only")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("cdesktop", b"backend")
    archive_bytes = payload.getvalue()
    asset_name = "cdesktop-macos-arm64.zip"
    manifest = json.dumps(
        {"assets": {asset_name: {"sha256": hashlib.sha256(archive_bytes).hexdigest()}}}
    )

    class Result:
        returncode = 0
        stdout = "cdesktop/0.2.4-sightmesh.1 darwin-arm64"
        stderr = ""

    def run(command, **_kwargs):
        if command[0] == "npm":
            prefix = Path(command[command.index("--prefix") + 1])
            executable = prefix / "node_modules" / ".bin" / "cdesktop"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture", encoding="utf-8")
            # Deliberately no dist/ tree: the real wrapper-only package
            # ships none, and staging must create the platform directory.
            (prefix / "node_modules" / "cdesktop").mkdir(parents=True)
        return Result()

    downloads = []
    original_download = updates._download

    def download(source, destination):
        downloads.append(source)
        if source.endswith("/manifest.json"):
            destination.write_text(manifest, encoding="utf-8")
        elif source.endswith(f"/{asset_name}"):
            destination.write_bytes(archive_bytes)
        else:
            original_download(source, destination)

    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setattr(updates, "_platform_directory", lambda: "macos-arm64")
    monkeypatch.setattr(updates, "_download", download)

    state = updates.stage(str(package), "0.2.4-sightmesh.1")

    runtime = updates.RUNTIME_LOCK.cdesktop
    base_url = f"https://github.com/{runtime.repository}/releases/download/{runtime.tag}"
    assert downloads[-2:] == [f"{base_url}/manifest.json", f"{base_url}/{asset_name}"]
    assert Path(state["pending"]["backend_archive"]).read_bytes() == archive_bytes


def test_stage_refuses_mismatched_downloaded_backend_archive(monkeypatch, tmp_path) -> None:
    """A release asset with a manifest checksum mismatch is never placed in the stage."""
    isolated_state(monkeypatch, tmp_path)
    package = tmp_path / "cdesktop.tgz"
    package.write_bytes(b"wrapper-only")

    class Result:
        returncode = 0
        stdout = "cdesktop/0.2.4-sightmesh.1 darwin-arm64"
        stderr = ""

    def run(command, **_kwargs):
        if command[0] == "npm":
            prefix = Path(command[command.index("--prefix") + 1])
            executable = prefix / "node_modules" / ".bin" / "cdesktop"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture", encoding="utf-8")
            # Deliberately no dist/ tree: the real wrapper-only package
            # ships none, and staging must create the platform directory.
            (prefix / "node_modules" / "cdesktop").mkdir(parents=True)
        return Result()

    asset_name = "cdesktop-macos-arm64.zip"
    original_download = updates._download

    def download(source, destination):
        if source.endswith("/manifest.json"):
            destination.write_text(
                json.dumps({"assets": {asset_name: {"sha256": "0" * 64}}}),
                encoding="utf-8",
            )
        elif source.endswith(f"/{asset_name}"):
            destination.write_bytes(b"untrusted archive")
        else:
            original_download(source, destination)

    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setattr(updates, "_platform_directory", lambda: "macos-arm64")
    monkeypatch.setattr(updates, "_download", download)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        updates.stage(str(package), "0.2.4-sightmesh.1")
    releases = list((tmp_path / "releases").glob("cdesktop-*"))
    assert len(releases) == 1
    assert not (
        releases[0]
        / "node_modules"
        / "cdesktop"
        / "dist"
        / "macos-arm64"
        / "cdesktop.zip"
    ).exists()


def test_activity_ignores_devservers_and_reports_agent_work() -> None:
    client = FakeClient(running=True, approvals=True)
    result = updates.activity(client)
    assert result["idle"] is False
    assert result["running"][0]["execution_process_id"] == "process-1"
    assert result["running"][0]["session_name"] == "worker-one"
    assert result["pending_approvals"][0]["approval_id"] == "approval-1"

    client.running = False
    client.approvals = False
    assert updates.activity(client)["idle"] is True


def test_activity_does_not_walk_queues_to_find_durable_follow_ups() -> None:
    result = updates.activity(FakeClient(queued=True))

    assert result["idle"] is True
    assert result["queued_follow_ups"] == []


def test_activity_does_not_depend_on_per_session_queue_reads() -> None:
    client = FakeClient()

    def fail_queue(_session_id):
        raise CdesktopError("queue unavailable")

    client.queue_status = fail_queue
    result = updates.activity(client)

    assert result["idle"] is True
    assert result["unreadable_sessions"] == []


def test_drain_retries_a_transient_activity_probe_failure(monkeypatch) -> None:
    client = FakeClient()
    real = client.execution_processes
    attempts = 0

    def flaky(session_id=None, *, status=None):
        nonlocal attempts
        assert_external_io_allowed()
        attempts += 1
        if attempts == 1:
            raise CdesktopError("GET /execution-processes failed: HTTP 500")
        return real(session_id, status=status)

    client.execution_processes = flaky
    monkeypatch.setattr(updates, "QUIET_SECONDS", 0)
    monkeypatch.setattr(updates, "DRAIN_POLL_SECONDS", 0)
    monkeypatch.setattr(updates, "DRAIN_WAIT_SECONDS", 1)

    current, report = updates._drain_and_wait(client, enforced=True)

    assert current["idle"] is True
    assert attempts == 2
    assert report["probe_errors"] == [
        "GET /execution-processes failed: HTTP 500"
    ]


def test_native_state_migration_imports_then_archives(monkeypatch, tmp_path) -> None:
    isolated_state(monkeypatch, tmp_path)
    state = service.state_dir()
    state.mkdir(parents=True)
    relationships = sqlite3.connect(state / "relationships.sqlite3")
    relationships.execute(
        "CREATE TABLE parent_edges (child_session_id TEXT, parent_session_id TEXT)"
    )
    relationships.execute("INSERT INTO parent_edges VALUES ('child', 'parent')")
    relationships.commit()
    relationships.close()
    delivery = sqlite3.connect(state / "delivery.sqlite3")
    delivery.execute(
        "CREATE TABLE deliveries (session_id TEXT, prompt TEXT, idempotency_key TEXT, "
        "status TEXT, created_at REAL)"
    )
    delivery.execute(
        "INSERT INTO deliveries VALUES "
        "('child', 'continue', 'delivery-1', 'pending', 1)"
    )
    delivery.commit()
    delivery.close()
    client = FakeClient()

    result = updates.migrate_native_state(client)

    assert client.parents == [("child", "parent")]
    assert client.sent == [("child", "continue", None, "legacy:delivery-1")]
    assert result["parent_relationships"] == 1
    assert result["pending_commands"] == 1
    assert len(result["archives"]) == 2
    assert not (state / "relationships.sqlite3").exists()
    assert not (state / "delivery.sqlite3").exists()


def test_prune_preserves_active_pending_and_one_recent_spare(
    monkeypatch, tmp_path: Path
) -> None:
    isolated_state(monkeypatch, tmp_path)
    root = updates.root_dir()
    root.mkdir(parents=True)
    releases = {
        name: root / name
        for name in (
            "cdesktop-active",
            "cdesktop-pending",
            "cdesktop-extra-new",
            "cdesktop-extra-old",
        )
    }
    for index, release in enumerate(releases.values()):
        executable = release / "node_modules" / ".bin" / "cdesktop"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture", encoding="utf-8")
        timestamp = time.time() + index
        os.utime(release, (timestamp, timestamp))

    backup_root = root / "backups"
    backup_root.mkdir()
    legacy_backup = backup_root / "cdesktop-old.plist"
    legacy_backup.write_bytes(b"obsolete")

    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "active": {
                "executable": str(
                    releases["cdesktop-active"] / "node_modules" / ".bin" / "cdesktop"
                )
            },
            "pending": {
                "executable": str(
                    releases["cdesktop-pending"] / "node_modules" / ".bin" / "cdesktop"
                )
            },
        },
    )

    result = updates.prune(keep=1)

    assert releases["cdesktop-active"].exists()
    assert releases["cdesktop-pending"].exists()
    assert sum(path.exists() for name, path in releases.items() if "extra" in name) == 1
    assert len([path for path in result["removed"] if "extra" in path]) == 1
    assert not legacy_backup.exists()


def test_automatic_prune_reports_cleanup_error_without_failing(monkeypatch) -> None:
    def fail() -> None:
        raise OSError("read only")

    monkeypatch.setattr(updates, "prune", fail)

    result = updates._automatic_prune()

    assert result["error"] == "read only"


def test_activation_drains_first_then_reports_a_bounded_timeout(
    monkeypatch, tmp_path
) -> None:
    """A host that never goes idle must still be *asked* to drain.

    The deleted idle-first gate meant a loaded host never reached
    `set_update_drain` at all, so a staged update sat pending forever and
    cdesktop releases piled up. Drain-first inverts that: admission is
    refused immediately, the wait is bounded, and a host that still will not
    quiet down is rolled back with the state it actually observed rather
    than the state that was configured.
    """
    isolated_state(monkeypatch, tmp_path)
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "pending": {"version": "0.2.4-sightmesh.1", "executable": "/tmp/cdesktop"},
        },
    )
    monkeypatch.setattr(updates, "QUIET_SECONDS", 0)
    monkeypatch.setattr(updates, "DRAIN_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(updates, "DRAIN_POLL_SECONDS", 0.0)
    bootouts: list[str] = []
    monkeypatch.setattr(service, "_bootout", lambda label: bootouts.append(label))
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    bootstraps: list[str] = []
    monkeypatch.setattr(
        service, "_bootstrap", lambda label, _path: bootstraps.append(label)
    )
    monkeypatch.setattr(service, "bridge_plist_path", lambda: tmp_path / "bridge.plist")
    monkeypatch.setattr(
        service,
        "plist_path",
        lambda: pytest.fail("a timed-out drain must not rewrite the service plist"),
    )

    client = FakeClient(running=True)
    result = updates.activate_after_drain(client, port=4321)

    assert result["action"] == "drain-timed-out"
    assert updates.read_state()["status"] == "drain-timed-out"
    # Drain requested before the wait, then released on the way out.
    assert client.drain_calls == [updates.DRAIN_SECONDS, 0]
    assert result["drain"]["enforced"] is True
    assert result["drain"]["configured_wait_seconds"] == 0.0
    assert result["activity"]["idle"] is False
    # An activation that never swapped anything never touched the bridge, so
    # there is nothing to put back: a refused update costs zero downtime.
    assert bootouts == []
    assert bootstraps == []


def test_activation_restarts_owned_services_and_verifies_version(
    monkeypatch, tmp_path
) -> None:
    isolated_state(monkeypatch, tmp_path)
    target = tmp_path / "cdesktop.plist"
    bridge = tmp_path / "bridge.plist"
    target.write_bytes(b"old-definition")
    bridge.write_bytes(b"bridge-definition")
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "pending": {
                "version": "0.2.4-sightmesh.1",
                "executable": "/tmp/new-cdesktop",
                "sha256": "abc",
            },
        },
    )
    monkeypatch.setattr(updates, "QUIET_SECONDS", 0)
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge)
    monkeypatch.setattr(
        service,
        "definition",
        lambda _port, executable: {"ProgramArguments": [str(executable)]},
    )
    monkeypatch.setattr(service, "_bootout", lambda _label: None)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(service, "wait_until_healthy", lambda _port: None)
    monkeypatch.setattr(service, "_loaded", lambda _label: True)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    bootstraps = []
    monkeypatch.setattr(
        service, "_bootstrap", lambda label, path: bootstraps.append((label, path))
    )
    monkeypatch.setattr(updates, "CdesktopClient", lambda _url: FakeClient())

    client = FakeClient()
    result = updates.activate_after_drain(client, port=4321)

    assert result["action"] == "activated"
    assert result["pending"] is None
    assert updates.read_state()["status"] == "active"
    assert [label for label, _path in bootstraps] == [
        service.LABEL,
        service.BRIDGE_LABEL,
    ]
    assert b"/tmp/new-cdesktop" in target.read_bytes()
    assert client.drain_calls == [updates.DRAIN_SECONDS]


def test_activation_allows_exact_legacy_bootstrap_without_drain(
    monkeypatch, tmp_path
) -> None:
    isolated_state(monkeypatch, tmp_path)
    target = tmp_path / "cdesktop.plist"
    bridge = tmp_path / "bridge.plist"
    target.write_bytes(b"old-definition")
    bridge.write_bytes(b"bridge-definition")
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "pending": {
                "version": "0.2.3-sightmesh.2",
                "executable": "/tmp/new-cdesktop",
            },
        },
    )
    monkeypatch.setattr(updates, "QUIET_SECONDS", 0)
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge)
    monkeypatch.setattr(
        service,
        "definition",
        lambda _port, executable: {"ProgramArguments": [str(executable)]},
    )
    monkeypatch.setattr(service, "_bootout", lambda _label: None)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(service, "wait_until_healthy", lambda _port: None)
    monkeypatch.setattr(service, "_loaded", lambda _label: True)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    monkeypatch.setattr(service, "_bootstrap", lambda _label, _path: None)
    monkeypatch.setattr(
        updates,
        "CdesktopClient",
        lambda _url: FakeClient(version="0.2.3-sightmesh.2"),
    )
    client = FakeClient(
        version="0.2.3-sightmesh.1",
        drain_supported=False,
    )

    result = updates.activate_after_drain(client, port=4321)

    assert result["action"] == "activated"
    assert result["active"]["version"] == "0.2.3-sightmesh.2"


def test_activation_failure_stops_without_rollback_or_retry(
    monkeypatch, tmp_path
) -> None:
    isolated_state(monkeypatch, tmp_path)
    target = tmp_path / "cdesktop.plist"
    bridge = tmp_path / "bridge.plist"
    target.write_bytes(b"old-definition")
    bridge.write_bytes(b"bridge-definition")
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "pending": {
                "version": "expected-version",
                "executable": "/tmp/new-cdesktop",
            },
        },
    )
    monkeypatch.setattr(updates, "QUIET_SECONDS", 0)
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge)
    monkeypatch.setattr(
        service,
        "definition",
        lambda _port, executable: {"ProgramArguments": [str(executable)]},
    )
    monkeypatch.setattr(service, "_bootout", lambda _label: None)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(service, "wait_until_healthy", lambda _port: None)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    monkeypatch.setattr(service, "_loaded", lambda _label: False)
    bootstraps = []
    monkeypatch.setattr(
        service, "_bootstrap", lambda label, path: bootstraps.append((label, path))
    )
    monkeypatch.setattr(
        updates,
        "CdesktopClient",
        lambda _url: FakeClient(version="wrong-version"),
    )

    with pytest.raises(RuntimeError, match="will not be retried automatically"):
        updates.activate_after_drain(FakeClient(), port=4321)

    state = updates.read_state()
    assert state["status"] == "failed"
    assert state["pending"] is None
    assert state["failed_package"]["version"] == "expected-version"
    assert b"/tmp/new-cdesktop" in target.read_bytes()
    assert not (updates.root_dir() / "backups").exists()
    assert [label for label, _path in bootstraps] == [
        service.LABEL,
        service.BRIDGE_LABEL,
    ]


def test_activation_refuses_unknown_backend_without_drain(
    monkeypatch, tmp_path
) -> None:
    isolated_state(monkeypatch, tmp_path)
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "pending": {"version": "next", "executable": "/tmp/cdesktop"},
        },
    )
    monkeypatch.setattr(service, "_bootout", lambda _label: None)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(service, "_bootstrap", lambda _label, _path: None)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)

    with pytest.raises(RuntimeError, match="HTTP 404"):
        updates.activate_after_drain(
            FakeClient(version="unknown", drain_supported=False),
            port=4321,
        )


def test_activation_converges_once_the_drain_empties_the_fleet(
    monkeypatch, tmp_path
) -> None:
    """The point of draining first: work that was running when activation
    started reaches terminal *during* the bounded wait, because no new work
    is admitted behind it. The old idle-first gate never got this far on a
    loaded host and left the staged update pending forever.
    """
    isolated_state(monkeypatch, tmp_path)
    target = tmp_path / "cdesktop.plist"
    bridge = tmp_path / "bridge.plist"
    target.write_bytes(b"old-definition")
    bridge.write_bytes(b"bridge-definition")
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "pending": {
                "version": "0.2.4-sightmesh.1",
                "executable": "/tmp/new-cdesktop",
                "sha256": "abc",
            },
        },
    )
    monkeypatch.setattr(updates, "QUIET_SECONDS", 0)
    monkeypatch.setattr(updates, "DRAIN_WAIT_SECONDS", 60.0)
    monkeypatch.setattr(updates, "DRAIN_POLL_SECONDS", 0.0)
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge)
    monkeypatch.setattr(
        service,
        "definition",
        lambda _port, executable: {"ProgramArguments": [str(executable)]},
    )
    monkeypatch.setattr(service, "_bootout", lambda _label: None)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(service, "wait_until_healthy", lambda _port: None)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    monkeypatch.setattr(service, "_bootstrap", lambda _label, _path: None)
    monkeypatch.setattr(updates, "CdesktopClient", lambda _url: FakeClient())

    client = FakeClient(running=True)
    polls = {"count": 0}
    real_activity = updates.activity

    def draining_activity(observed_client):
        polls["count"] += 1
        if polls["count"] >= 2:
            observed_client.running = False
        return real_activity(observed_client)

    monkeypatch.setattr(updates, "activity", draining_activity)

    result = updates.activate_after_drain(client, port=4321)

    assert result["action"] == "activated"
    assert client.drain_calls == [updates.DRAIN_SECONDS]
    assert result["drain"]["enforced"] is True
    assert result["drain"]["waited_seconds"] >= 0
    assert polls["count"] >= 2


def _staged_activation(monkeypatch, tmp_path, *, clock: FakeClock) -> list[tuple[str, str]]:
    """Wire one staged update whose activation runs on a virtual clock.

    Returns the ordered events the activation performs, so a test can assert
    *when* the bridge went down rather than only that it came back.
    """
    isolated_state(monkeypatch, tmp_path)
    target = tmp_path / "cdesktop.plist"
    bridge = tmp_path / "bridge.plist"
    target.write_bytes(b"old-definition")
    bridge.write_bytes(b"bridge-definition")
    updates._write_json_atomic(
        updates.state_path(),
        {
            "schema_version": 1,
            "status": "staged",
            "pending": {
                "version": "0.2.4-sightmesh.1",
                "executable": "/tmp/new-cdesktop",
                "sha256": "abc",
            },
        },
    )
    monkeypatch.setattr(updates, "time", clock)
    monkeypatch.setattr(service, "plist_path", lambda: target)
    monkeypatch.setattr(service, "bridge_plist_path", lambda: bridge)
    monkeypatch.setattr(
        service,
        "definition",
        lambda _port, executable: {"ProgramArguments": [str(executable)]},
    )
    monkeypatch.setattr(service, "wait_until_healthy", lambda _port: None)
    monkeypatch.setattr(service, "is_healthy", lambda _port: True)
    monkeypatch.setattr(service, "_wait_until_unloaded", lambda _label: None)
    monkeypatch.setattr(updates, "CdesktopClient", lambda _url: FakeClient())
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service, "_bootout", lambda label: events.append(("bootout", label))
    )
    monkeypatch.setattr(
        service, "_bootstrap", lambda label, _path: events.append(("bootstrap", label))
    )
    observed_activity = updates.activity
    monkeypatch.setattr(
        updates,
        "activity",
        lambda client: (events.append(("poll", "activity")), observed_activity(client))[1],
    )
    return events


def test_activation_re_arms_the_drain_inside_the_executor_ttl(
    monkeypatch, tmp_path
) -> None:
    """A busy host converges only while admission stays refused.

    cdesktop caps one drain request at 30 seconds, but the wait budget is 15
    minutes. The shipped code asked once for 15 seconds and then polled: the
    refusal lapsed three polls in, the host admitted fresh turns behind it,
    and activation timed out on exactly the loaded hosts the drain exists
    for. Re-arming inside the TTL is what makes the wait mean what it says.
    """
    clock = FakeClock()
    _staged_activation(monkeypatch, tmp_path, clock=clock)
    client = FakeClient(running=True, turn_seconds=20.0, clock=clock)

    result = updates.activate_after_drain(client, port=4321)

    assert result["action"] == "activated"
    assert result["drain"]["rearms"] >= 1
    assert result["drain"]["lapsed"] is None
    # Every request is one the executor accepts, and there is more than one.
    assert set(client.drain_calls) == {updates.DRAIN_SECONDS}
    assert len(client.drain_calls) >= 2
    assert result["drain"]["waited_seconds"] < updates.DRAIN_WAIT_SECONDS


def test_activation_keeps_the_bridge_up_until_the_fleet_has_drained(
    monkeypatch, tmp_path
) -> None:
    """Shipped order was: boot the bridge out, then wait up to 15 minutes.

    The fleet lost its message path for the whole wait - and on a host that
    never quieted down, for nothing at all. The bridge is only in the way of
    the swap, so it goes down after the fleet converges and comes straight
    back up.
    """
    clock = FakeClock()
    events = _staged_activation(monkeypatch, tmp_path, clock=clock)
    client = FakeClient(running=True, turn_seconds=20.0, clock=clock)

    assert updates.activate_after_drain(client, port=4321)["action"] == "activated"

    bootout = events.index(("bootout", service.BRIDGE_LABEL))
    polls = [index for index, event in enumerate(events) if event[0] == "poll"]
    assert polls and max(polls) < bootout
    assert events[bootout + 1 :] == [
        ("bootstrap", service.LABEL),
        ("bootstrap", service.BRIDGE_LABEL),
    ]


def test_activation_stops_waiting_once_the_drain_can_no_longer_be_renewed(
    monkeypatch, tmp_path
) -> None:
    """Waiting on a refusal that is not in force is hoping, not draining.

    When a renewal fails the wait ends immediately and reports why, instead
    of spending the rest of a 15-minute budget while the host admits new
    work behind it.
    """
    clock = FakeClock()
    events = _staged_activation(monkeypatch, tmp_path, clock=clock)
    client = FakeClient(running=True, turn_seconds=20.0, clock=clock)
    accepted = client.set_update_drain

    def stops_honouring_the_drain(seconds):
        if seconds and client.drain_calls:
            raise CdesktopError("cdesktop is not responding")
        return accepted(seconds)

    monkeypatch.setattr(client, "set_update_drain", stops_honouring_the_drain)

    result = updates.activate_after_drain(client, port=4321)

    assert result["action"] == "drain-timed-out"
    assert "stopped honouring the drain" in result["drain"]["lapsed"]
    assert result["drain"]["waited_seconds"] < updates.DRAIN_WAIT_SECONDS
    assert ("bootout", service.BRIDGE_LABEL) not in events


def test_the_re_arm_cadence_fits_inside_what_the_executor_accepts() -> None:
    """Both bounds are the executor's, not ours: a request above its cap is
    rejected outright, and a cadence at or above the TTL leaves a window a
    host can launch new work through.
    """
    assert 0 < updates.DRAIN_SECONDS <= FakeClient.DRAIN_CAP_SECONDS
    assert 0 < updates.DRAIN_REARM_SECONDS < updates.DRAIN_SECONDS
