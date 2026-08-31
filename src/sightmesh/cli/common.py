from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dataclasses
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
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
    fleet,
    leases,
    routing,
    service,
    succession,
    tasks,
    updates,
)
from ..bridge import run_bridge
from ..cdesktop import (
    CdesktopClient,
    CdesktopError,
    CdesktopInterruptedError,
    CdesktopPendingError,
    CdesktopRejectedError,
)
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


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, indent=2))


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
