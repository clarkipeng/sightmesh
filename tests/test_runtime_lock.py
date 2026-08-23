from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from sightmesh.runtime_lock import (
    LOCK_RESOURCE,
    RUNTIME_LOCK,
    RuntimeLockError,
    load_runtime_lock,
    verify_file_sha256,
)

ROOT = Path(__file__).parents[1]


def _write_lock(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "runtime-lock.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_runtime_lock_schema_is_typed_and_coherent() -> None:
    runtime = RUNTIME_LOCK.cdesktop
    assert RUNTIME_LOCK.schema_version == 1
    assert runtime.repository.count("/") == 1
    assert runtime.tag.startswith(f"v{runtime.version}")
    assert runtime.package.url.startswith(
        f"https://github.com/{runtime.repository}/releases/download/{runtime.tag}/"
    )
    assert len(runtime.package.sha256) == 64
    assert runtime.compatibility.minimum_tuple <= tuple(
        int(part) for part in runtime.version.split(".")
    )
    assert (
        runtime.compatibility.minimum_tuple
        <= runtime.compatibility.durable_recovery_tuple
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(schema_version=2),
        lambda data: data["cdesktop"].update(unexpected=True),
        lambda data: data["cdesktop"]["package"].update(sha256="not-a-digest"),
        lambda data: data["cdesktop"].update(version="next"),
        lambda data: data["cdesktop"]["package"].update(url="http://example.test/a"),
    ],
)
def test_runtime_lock_rejects_malformed_or_unsupported_data(
    tmp_path: Path, mutation
) -> None:
    data = {"schema_version": 1, "cdesktop": asdict(RUNTIME_LOCK.cdesktop)}
    mutation(data)
    with pytest.raises(RuntimeLockError):
        load_runtime_lock(_write_lock(tmp_path, data))


def test_runtime_artifact_checksum_succeeds_and_fails_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "cdesktop.tgz"
    artifact.write_bytes(b"known runtime")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert verify_file_sha256(artifact, digest) == digest
    with pytest.raises(RuntimeLockError, match="checksum mismatch"):
        verify_file_sha256(artifact, "0" * 64)


def test_built_wheel_contains_loadable_runtime_lock(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        member = next(
            name for name in archive.namelist() if name.endswith(f"/{LOCK_RESOURCE}")
        )
        data = json.loads(archive.read(member))
    assert data["cdesktop"]["package"]["sha256"] == RUNTIME_LOCK.cdesktop.package.sha256


def test_pinned_asset_identity_is_not_duplicated_outside_runtime_lock() -> None:
    runtime = RUNTIME_LOCK.cdesktop
    needles = (runtime.package.url, runtime.package.sha256, runtime.tag)
    paths = [
        *ROOT.joinpath("src").rglob("*.py"),
        *ROOT.joinpath("scripts").glob("*"),
        ROOT / "README.md",
        *ROOT.joinpath("docs").rglob("*.md"),
    ]
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(needle in text for needle in needles), path
