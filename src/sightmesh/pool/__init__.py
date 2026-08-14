"""Ordered credential pool across accounts the operator owns.

A pool holds Claude and Codex accounts per provider in priority order. A launch
takes the first account that still has quota; an exhausted account is cooled
until the provider's own reported reset and the next owned account takes over.
Each account is used with its own normal credentials.
"""

from __future__ import annotations

from .core import (
    DEFAULT_COOLDOWN,
    PROVIDERS,
    PoolError,
    accounts_for,
    ambient_claude_identity,
    check_duplicate,
    clear_cooldown,
    codex_identity,
    cooling_until,
    default_pool_root,
    env_for,
    find,
    fingerprint,
    fmt_delta,
    identity_key,
    identity_label,
    load_pool,
    load_state,
    normalize_token,
    parse_duration,
    parse_iso,
    probe,
    quota_cached,
    reorder,
    save_pool,
    save_state,
    select,
    shape,
    snapshot,
    token_path,
    validate_claude_token,
    write_token,
)

__all__ = [
    "DEFAULT_COOLDOWN",
    "PROVIDERS",
    "PoolError",
    "accounts_for",
    "ambient_claude_identity",
    "check_duplicate",
    "clear_cooldown",
    "codex_identity",
    "cooling_until",
    "default_pool_root",
    "env_for",
    "find",
    "fingerprint",
    "fmt_delta",
    "identity_key",
    "identity_label",
    "load_pool",
    "load_state",
    "normalize_token",
    "parse_duration",
    "parse_iso",
    "probe",
    "quota_cached",
    "reorder",
    "save_pool",
    "save_state",
    "select",
    "shape",
    "snapshot",
    "token_path",
    "validate_claude_token",
    "write_token",
]
