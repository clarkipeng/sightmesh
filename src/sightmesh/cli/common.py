from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dataclasses
import getpass
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import (
    __version__,
    approvals,
    conductor_migrate,
    escalation,
    execution_routing,
    leases,
    observability,
    routing,
    service,
    succession,
    updates,
)
fleet = importlib.import_module("sightmesh.fleet")
from ..bridge import run_bridge
from ..cdesktop import CdesktopClient, CdesktopError
from ..pool import core as pool_core
from ..pool.core import PoolError
from ..profiles import (
    Profile,
    ProfileError,
    ProfileStore,
    provider_summary,
    validate_provider,
)
from ..repowire import RepowireError
from ..repowire import reply as repowire_reply
from ..runtime_lock import RUNTIME_LOCK

CDESKTOP_FORK_MARKER = "sightmesh"
DEFAULT_OVERVIEW_HOURS = 24
COORDINATION_MARKER = "## Local agent coordination"
COORDINATION_CONTRACT = """## Local agent coordination

- Use `sightmesh peers` and `sightmesh peek @agent` for compact fleet awareness.
- Use `sightmesh steer @agent --message "..."` for immediate peer contact. It interrupts only that agent's active turn.
- Leads use `sightmesh inbox` and one `sightmesh respond --responses '...'` call for pending requests across the fleet.
- Contact your launcher with `sightmesh parent --message "STATUS: concise details"` when blocked, when a decision is needed, and when complete.
- Batch independent read-only tool calls and all currently known independent questions. Keep dependent or destructive actions sequential.
- Do not use hidden or native subagents.
"""


def _read_text(value: str | None, path: str | None, label: str) -> str:
    if bool(value) == bool(path):
        raise ValueError(f"Provide exactly one of --{label} or --{label}-file")
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    return value or ""


def _with_coordination_contract(prompt: str) -> str:
    if COORDINATION_MARKER in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{COORDINATION_CONTRACT.rstrip()}\n"


#: Keys whose *values* are credentials. The CLI has exactly one output path,
#: so rejecting these here makes a future leak fail loudly instead of
#: silently serializing a live capability into a transcript
#: (docs/kernel-contract.md, "Observability").
SECRET_KEYS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passphrase",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "capability",
        "private_key",
    }
)


def _reject_secret_keys(data: Any, path: str = "") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            where = f"{path}.{key}" if path else str(key)
            if str(key).casefold() in SECRET_KEYS:
                raise ValueError(
                    f"Refusing to emit credential-shaped field {where!r}. "
                    "Write the value to a private file and emit its path."
                )
            _reject_secret_keys(value, where)
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            _reject_secret_keys(value, f"{path}[{index}]")


#: What a pass-through payload's credential-shaped values are replaced with.
REDACTED = "[redacted]"


def _redact_secret_values(data: Any) -> Any:
    """Return a copy of a pass-through payload with credential values removed.

    External payloads are authored by agents and executors, not by this CLI:
    the approval inbox attaches cdesktop's raw tool action verbatim, so one
    nested ``Authorization`` header in one request would otherwise take down
    the whole inbox. Refusing to emit is the right answer for a dict the
    kernel built - that is a bug here - but for a payload we are only
    relaying, the credential is what must go, not the operator's view of the
    fleet.
    """
    if isinstance(data, dict):
        return {
            key: REDACTED
            if str(key).casefold() in SECRET_KEYS
            else _redact_secret_values(value)
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [_redact_secret_values(item) for item in data]
    return data


def _emit(data: Any, as_json: bool, *, external: bool = False) -> None:
    """Print one command result through the CLI's single output path.

    ``external=True`` marks a payload the kernel did not construct and is
    only relaying; its credential-shaped values are redacted. Kernel-built
    dicts keep the loud guard, because a credential in one of those is a
    defect in this repository and failing is how it gets found.
    """
    # The guard rejects on key name, so it is the kernel-payload rule; a
    # redacted pass-through payload has already lost every value it names.
    data = _redact_secret_values(data) if external else data
    if not external:
        _reject_secret_keys(data)
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, indent=2))


def _emit_action_result(data: Any, as_json: bool, *, fallback: Any) -> None:
    """Report a completed action's outcome without ever contradicting it.

    The action already happened when this runs. An approval that went
    through and then printed an error - because rendering its pass-through
    payload raised - tells the operator the exact opposite of the truth, so
    a rendering failure degrades to a warning plus the minimal kernel-owned
    summary and the command still succeeds.
    """
    try:
        _emit(data, as_json, external=True)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        print(
            f"warning: the action succeeded but its full result could not be "
            f"rendered: {exc}",
            file=sys.stderr,
        )
        _emit(fallback, as_json)


def emit_capability(directory: Path, name: str, value: str) -> Path:
    """Deliver a capability through a private file, never through stdout.

    The founder rule is absolute: never print a credential value. ``acquire``
    is the one command that must hand a caller a live token, so it writes it
    0600 next to the store that owns it and reports only the path.
    """
    root = Path(directory).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    path = root / f"{name}.token"
    handle, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=root)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(value)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def _repowire_status_ok(returncode: int, detail: str) -> bool:
    return (
        returncode == 0
        and "Daemon responding at" in detail
        and "Daemon error" not in detail
    )


def _is_sightmesh_cdesktop_version(detail: object) -> bool:
    normalized = str(detail or "").casefold()
    if CDESKTOP_FORK_MARKER not in normalized and not normalized.startswith(
        "cdesktop/"
    ):
        return False
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", normalized)
    if not match:
        return False
    version = tuple(int(part) for part in match.groups())
    return version >= RUNTIME_LOCK.cdesktop.compatibility.minimum_tuple


def _active_runtime_matches_lock(reported_version: object) -> bool:
    """Checksum-verified provenance for servers whose /info version is bare.

    The server never announces fork identity in its version string; the
    updater already proves provenance by verifying the runtime lock's
    SHA-256 at stage time. Trust that verified activation when the running
    server reports the exact locked version.
    """
    try:
        active = updates.read_state().get("active") or {}
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError):
        return False
    if active.get("sha256") != RUNTIME_LOCK.cdesktop.package.sha256:
        return False
    return str(reported_version or "") == RUNTIME_LOCK.cdesktop.version


__all__ = [name for name in globals() if not name.startswith("__")]
