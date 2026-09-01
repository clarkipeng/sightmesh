from __future__ import annotations

import os
import selectors
import signal
import subprocess
from pathlib import Path
from typing import BinaryIO, Sequence

DEFAULT_MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_LOG_ENV = "SIGHTMESH_MAX_SERVICE_LOG_BYTES"


def max_log_bytes() -> int:
    try:
        value = int(os.environ.get(MAX_LOG_ENV, DEFAULT_MAX_LOG_BYTES))
    except ValueError:
        return DEFAULT_MAX_LOG_BYTES
    return value if value > 0 else DEFAULT_MAX_LOG_BYTES


class BoundedLog:
    """Append service output while retaining at most two bounded generations."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.backup = path.with_name(f"{path.name}.1")
        self.limit = limit
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._trim_file(self.backup)
        self._trim_existing()
        self.stream: BinaryIO = self.path.open("ab", buffering=0)
        self.path.chmod(0o600)

    def _trim_existing(self) -> None:
        if not self.path.exists() or self.path.stat().st_size <= self.limit:
            return
        with self.path.open("rb") as source:
            source.seek(-self.limit, os.SEEK_END)
            tail = source.read(self.limit)
        temporary = self.backup.with_name(f".{self.backup.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as target:
                target.write(tail)
            temporary.chmod(0o600)
            os.replace(temporary, self.backup)
            self.path.unlink()
        finally:
            temporary.unlink(missing_ok=True)

    def _trim_file(self, path: Path) -> None:
        if not path.exists() or path.stat().st_size <= self.limit:
            return
        with path.open("rb") as source:
            source.seek(-self.limit, os.SEEK_END)
            tail = source.read(self.limit)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as target:
                target.write(tail)
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def write(self, payload: bytes) -> None:
        if not payload:
            return
        if len(payload) > self.limit:
            payload = payload[-self.limit :]
        if self.stream.tell() + len(payload) > self.limit:
            self.stream.close()
            self.backup.unlink(missing_ok=True)
            if self.path.exists():
                os.replace(self.path, self.backup)
            self.stream = self.path.open("ab", buffering=0)
            self.path.chmod(0o600)
        self.stream.write(payload)

    def close(self) -> None:
        self.stream.close()


def run(
    command: Sequence[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    limit: int | None = None,
) -> int:
    """Run one managed child and bound both persistent output streams."""
    if not command:
        raise ValueError("Managed service command must not be empty")
    bound = limit or max_log_bytes()
    stdout = BoundedLog(stdout_path, bound)
    stderr = BoundedLog(stderr_path, bound)
    try:
        child = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception:
        stdout.close()
        stderr.close()
        raise

    def signal_child(signum: int) -> None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            signal_child(signum)

    previous = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    selector = selectors.DefaultSelector()
    assert child.stdout is not None
    assert child.stderr is not None
    selector.register(child.stdout, selectors.EVENT_READ, stdout)
    selector.register(child.stderr, selectors.EVENT_READ, stderr)
    try:
        while selector.get_map():
            for key, _mask in selector.select(timeout=0.5):
                payload = os.read(key.fileobj.fileno(), 64 * 1024)
                if payload:
                    key.data.write(payload)
                else:
                    selector.unregister(key.fileobj)
        return child.wait()
    finally:
        selector.close()
        if child.poll() is None:
            signal_child(signal.SIGTERM)
            child.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        stdout.close()
        stderr.close()
