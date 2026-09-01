import os
import sys

from sightmesh.service_process import BoundedLog, run


def test_bounded_log_keeps_two_fixed_generations(tmp_path) -> None:
    path = tmp_path / "service.log"
    log = BoundedLog(path, 8)

    log.write(b"12345678")
    log.write(b"abcdefgh")
    log.write(b"ABCDEFGH")
    log.close()

    assert path.read_bytes() == b"ABCDEFGH"
    assert path.with_name("service.log.1").read_bytes() == b"abcdefgh"


def test_bounded_log_caps_the_current_writer_file_descriptor(tmp_path) -> None:
    path = tmp_path / "service.log"
    backup = path.with_name("service.log.1")
    log = BoundedLog(path, 8)
    log.write(b"12345678")
    previous_fd = os.dup(log.stream.fileno())
    try:
        log.write(b"abcdefgh")

        # Rotation leaves the prior open description on the bounded backup
        # and moves the active writer to the newly opened bounded path.
        assert os.fstat(previous_fd).st_size <= 8
        assert os.fstat(previous_fd).st_ino == backup.stat().st_ino
        assert os.fstat(log.stream.fileno()).st_size <= 8
        assert os.fstat(log.stream.fileno()).st_ino == path.stat().st_ino
    finally:
        os.close(previous_fd)
        log.close()


def test_run_bounds_child_stdout_and_stderr_at_the_writer(tmp_path) -> None:
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    assert run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('o' * 64); sys.stderr.write('e' * 64)",
        ],
        stdout_path=stdout,
        stderr_path=stderr,
        limit=8,
    ) == 0

    assert stdout.stat().st_size <= 8
    assert stderr.stat().st_size <= 8


def test_bounded_log_trims_an_existing_runaway_file(tmp_path) -> None:
    path = tmp_path / "service.log"
    path.write_bytes(b"0123456789abcdef")
    path.with_name("service.log.1").write_bytes(b"ABCDEFGHIJKL")

    log = BoundedLog(path, 6)
    log.close()

    assert path.read_bytes() == b""
    assert path.with_name("service.log.1").read_bytes() == b"abcdef"


def test_bounded_log_trims_an_existing_backup(tmp_path) -> None:
    path = tmp_path / "service.log"
    path.write_bytes(b"new")
    backup = path.with_name("service.log.1")
    backup.write_bytes(b"0123456789")

    log = BoundedLog(path, 4)
    log.close()

    assert backup.read_bytes() == b"6789"
