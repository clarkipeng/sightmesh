"""Validated access to SightMesh's pinned native runtime contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOCK_RESOURCE = "runtime-lock.json"
SUPPORTED_SCHEMA_VERSION = 1
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RuntimeLockError(ValueError):
    """The runtime lock cannot be trusted or understood."""


@dataclass(frozen=True)
class PackageAsset:
    url: str
    sha256: str


@dataclass(frozen=True)
class Compatibility:
    minimum: str
    durable_recovery: str

    @property
    def minimum_tuple(self) -> tuple[int, int, int]:
        return _version_tuple(self.minimum)

    @property
    def durable_recovery_tuple(self) -> tuple[int, int, int]:
        return _version_tuple(self.durable_recovery)


@dataclass(frozen=True)
class CdesktopRuntime:
    repository: str
    version: str
    tag: str
    package: PackageAsset
    compatibility: Compatibility


@dataclass(frozen=True)
class RuntimeLock:
    schema_version: int
    cdesktop: CdesktopRuntime


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if not match:
        raise RuntimeLockError(f"Invalid semantic version in runtime lock: {value!r}")
    return tuple(int(part) for part in match.groups())


def _exact_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise RuntimeLockError(f"Malformed {label} in runtime lock")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeLockError(f"Invalid {label} in runtime lock")
    return value


def _validate(data: object) -> RuntimeLock:
    root = _exact_keys(data, {"schema_version", "cdesktop"}, "root")
    if root["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        raise RuntimeLockError(
            f"Unsupported runtime lock schema: {root['schema_version']!r}"
        )
    cdesktop = _exact_keys(
        root["cdesktop"],
        {"repository", "version", "tag", "package", "compatibility"},
        "cdesktop record",
    )
    package = _exact_keys(cdesktop["package"], {"url", "sha256"}, "package")
    compatibility = _exact_keys(
        cdesktop["compatibility"],
        {"minimum", "durable_recovery"},
        "compatibility",
    )
    repository = _text(cdesktop["repository"], "repository")
    version = _text(cdesktop["version"], "version")
    tag = _text(cdesktop["tag"], "tag")
    url = _text(package["url"], "package URL")
    sha256 = _text(package["sha256"], "package SHA-256")
    minimum = _text(compatibility["minimum"], "minimum compatibility version")
    durable = _text(
        compatibility["durable_recovery"], "durable recovery compatibility version"
    )
    if not _REPOSITORY.fullmatch(repository):
        raise RuntimeLockError(f"Invalid repository identity in runtime lock: {repository!r}")
    current = _version_tuple(version)
    minimum_tuple = _version_tuple(minimum)
    durable_tuple = _version_tuple(durable)
    if current < minimum_tuple or durable_tuple < minimum_tuple:
        raise RuntimeLockError("Incoherent compatibility versions in runtime lock")
    if not tag.startswith(f"v{version}"):
        raise RuntimeLockError("Runtime tag does not identify the pinned version")
    parsed = urlparse(url)
    expected_prefix = f"https://github.com/{repository}/releases/download/{tag}/"
    if (
        parsed.scheme != "https"
        or not url.startswith(expected_prefix)
        or any(character.isspace() for character in url)
    ):
        raise RuntimeLockError("Package URL does not match the pinned repository and tag")
    if not _SHA256.fullmatch(sha256):
        raise RuntimeLockError("Invalid package SHA-256 in runtime lock")
    return RuntimeLock(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        cdesktop=CdesktopRuntime(
            repository=repository,
            version=version,
            tag=tag,
            package=PackageAsset(url=url, sha256=sha256),
            compatibility=Compatibility(minimum=minimum, durable_recovery=durable),
        ),
    )


def load_runtime_lock(path: str | Path | None = None) -> RuntimeLock:
    """Load and strictly validate the repository or installed-wheel lock."""
    try:
        if path is None:
            raw = resources.files("sightmesh").joinpath(LOCK_RESOURCE).read_text(
                encoding="utf-8"
            )
        else:
            raw = Path(path).read_text(encoding="utf-8")
        return _validate(json.loads(raw))
    except RuntimeLockError:
        raise
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeLockError(f"Could not load runtime lock: {exc}") from exc


def verify_file_sha256(path: str | Path, expected: str) -> str:
    """Return the artifact digest, failing closed when it differs."""
    if not _SHA256.fullmatch(expected):
        raise RuntimeLockError("Invalid expected SHA-256")
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as artifact:
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeLockError(f"Could not read runtime artifact: {exc}") from exc
    actual = digest.hexdigest()
    if actual != expected:
        raise RuntimeLockError(
            f"Runtime artifact checksum mismatch: got {actual}, expected {expected}"
        )
    return actual


RUNTIME_LOCK = load_runtime_lock()
