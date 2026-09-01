from sightmesh.service_process import BoundedLog


def test_bounded_log_keeps_two_fixed_generations(tmp_path) -> None:
    path = tmp_path / "service.log"
    log = BoundedLog(path, 8)

    log.write(b"12345678")
    log.write(b"abcdefgh")
    log.write(b"ABCDEFGH")
    log.close()

    assert path.read_bytes() == b"ABCDEFGH"
    assert path.with_name("service.log.1").read_bytes() == b"abcdefgh"


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
