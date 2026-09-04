"""Conductor-safe durable parent escalation.

When SightMesh is launched by cdesktop, a spawned child session always has a
live cdesktop parent to receive escalations (``sightmesh parent --message``).
When the launcher is external (for example Conductor invoking the SightMesh
CLI directly), no such parent may exist. This module makes that case correct
by construction: every escalation is either delivered to a confirmed live,
non-archived parent session, or durably parked in a decision inbox so it is
never silently dropped. Parked escalations are never auto-delivered into a
session that has since been archived -- that delivery path stays closed
forever for that record; an operator resolves it explicitly instead.

Delivered escalations carry classified intent: routine STATUS/completion
callbacks queue with ``intent=continue`` and a durable acknowledgment record,
preserving the recipient's active turn. Only explicit BLOCKED/DECISION
escalations replace it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cdesktop import CdesktopClient, CdesktopError
from .service import state_dir

CDESKTOP_SESSION_ENV = "CDESKTOP_SESSION_ID"
CONDUCTOR_ENV_HINTS = ("CONDUCTOR_WORKSPACE_NAME", "CONDUCTOR_ROOT_PATH")

PARK_REASONS = frozenset({"no_parent", "parent_archived", "parent_unreachable"})
INTERRUPT_TAGS = frozenset({"BLOCKED", "DECISION"})
ESCALATION_KINDS = frozenset({"routine", "interrupt"})
DELIVERY_INTENTS = frozenset({"continue", "replace"})
WAL_ADOPTION_TIMEOUT_SECONDS = 30.0
ESCALATION_SCHEMA_VERSION = 1
SIGNAL_CONDITION_RE = re.compile(
    r"^(?:terminal|context-pressure:(0(?:\.\d+)?|1(?:\.0+)?)|idle:(\d+))$"
)


class EscalationStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignalPolicy:
    """The opt-in, per-session conditions the durable sweep may signal."""

    session_id: str
    conditions: tuple[str, ...]
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "signal_on": list(self.conditions),
            "updated_at": self.updated_at,
        }


def parse_signal_conditions(value: str) -> tuple[str, ...]:
    """Validate and canonicalize the deliberately small v1 condition language."""
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError("--signal-on requires one or more comma-separated conditions")
    conditions: set[str] = set()
    for part in parts:
        match = SIGNAL_CONDITION_RE.fullmatch(part)
        if not match:
            raise ValueError(
                "Unknown signal condition %r; use terminal, context-pressure:<0..1>, "
                "or idle:<seconds>" % part
            )
        if part.startswith("context-pressure:"):
            condition = f"context-pressure:{float(match.group(1)):g}"
        elif part.startswith("idle:"):
            condition = f"idle:{int(match.group(2))}"
        else:
            condition = part
        conditions.add(condition)
    return tuple(sorted(conditions))


def escalation_db_path() -> Path:
    return state_dir() / "escalations.sqlite3"


@dataclass(frozen=True)
class LauncherIdentity:
    launcher: str  # "cdesktop" | "external"
    detail: str | None  # best-effort hint, e.g. "conductor" or "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"launcher": self.launcher, "detail": self.detail}


def detect_launcher(env: dict[str, str] | None = None) -> LauncherIdentity:
    """Identify what invoked this SightMesh process, using the same
    CDESKTOP_SESSION_ID invariant the rest of the CLI already treats as
    proof of a live cdesktop parent context."""
    source = env if env is not None else os.environ
    if source.get(CDESKTOP_SESSION_ENV):
        return LauncherIdentity(launcher="cdesktop", detail=None)
    if any(source.get(name) for name in CONDUCTOR_ENV_HINTS):
        return LauncherIdentity(launcher="external", detail="conductor")
    return LauncherIdentity(launcher="external", detail="unknown")


@dataclass(frozen=True)
class EscalationClass:
    kind: str  # "routine" | "interrupt"
    intent: str  # "continue" | "replace"


def classify_escalation(message: str) -> EscalationClass:
    """Routine STATUS/completion callbacks must preserve the recipient's
    active turn; only messages that open with an explicit BLOCKED or
    DECISION tag may interrupt and replace it."""
    match = re.match(r"\s*([A-Za-z]+)", message)
    tag = match.group(1).upper() if match else ""
    if tag in INTERRUPT_TAGS:
        return EscalationClass(kind="interrupt", intent="replace")
    return EscalationClass(kind="routine", intent="continue")


@dataclass(frozen=True)
class DeliveryAck:
    ack_id: str
    dedupe_key: str
    child_session_id: str
    parent_session_id: str
    kind: str
    intent: str
    message: str
    delivered_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ack_id": self.ack_id,
            "dedupe_key": self.dedupe_key,
            "child_session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "kind": self.kind,
            "intent": self.intent,
            "message": self.message,
            "delivered_at": self.delivered_at,
        }


@dataclass(frozen=True)
class ParkedEscalation:
    escalation_id: str
    child_session_id: str
    child_workspace_id: str | None
    recorded_parent_session_id: str | None
    reason: str
    message: str
    dedupe_key: str
    status: str
    created_at: float
    resolved_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "escalation_id": self.escalation_id,
            "child_session_id": self.child_session_id,
            "child_workspace_id": self.child_workspace_id,
            "recorded_parent_session_id": self.recorded_parent_session_id,
            "reason": self.reason,
            "message": self.message,
            "dedupe_key": self.dedupe_key,
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True)
class OrderExpectation:
    order_id: str
    sender_session_id: str | None
    recipient_session_id: str
    body: str
    body_digest: str
    created_at: float
    satisfied_at: float | None


_CREDENTIAL_KEY_PARTS = (
    "authorization",
    "bearer",
    "cookie",
    "auth",
    "token",
    "key",
    "apikey",
    "secret",
    "password",
    "credential",
)

_CREDENTIAL_VALUE_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+(?:[A-Za-z0-9._~+/=-]+)"), r"\1 [REDACTED]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:sk-ant-|sk-|ghp_|gho_|github_pat_|xox[abp]-|AKIA|pypi-|npm-)[A-Za-z0-9_-]+"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
        "[REDACTED]",
    ),
)
_URL_USERINFO_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@")
_SAFE_ROUTING_VALUE_RE = re.compile(
    r"^(?:[0-9a-f]{7,64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+)$",
    re.IGNORECASE,
)
_SAFE_ROUTING_KEY_PARTS = ("dedupe", "session", "sha", "commit", "path", "uuid")


def _is_credential_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _CREDENTIAL_KEY_PARTS)


def _is_routing_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _SAFE_ROUTING_KEY_PARTS)


def _has_high_entropy(value: str) -> bool:
    """Identify opaque secrets without treating ordinary routing ids as secrets."""
    if len(value) <= 24 or _SAFE_ROUTING_VALUE_RE.fullmatch(value):
        return False
    frequencies = {character: value.count(character) for character in set(value)}
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in frequencies.values()
    )
    return entropy >= 4.0


def _redact_string(value: str, key: object | None = None) -> str:
    """Redact credential shapes whether or not callers gave them a revealing key."""
    if key is not None and _is_credential_key(key):
        if not _is_routing_key(key) and _has_high_entropy(value):
            return "[REDACTED]"
    redacted = _URL_USERINFO_RE.sub(r"\1***@", value)
    for pattern, replacement in _CREDENTIAL_VALUE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_structured_credentials(payload: Any, key: object | None = None) -> Any:
    if isinstance(payload, dict):
        return {
            child_key: "[REDACTED]"
            if _is_credential_key(child_key) and not _is_routing_key(child_key)
            else _redact_structured_credentials(value, child_key)
            for child_key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_structured_credentials(value, key) for value in payload]
    if isinstance(payload, str):
        return _redact_string(payload, key)
    return payload


def redact_credentials(payload: Any) -> Any:
    """Redact credential-bearing fields before a payload becomes durable.

    JSON is decoded and walked so formatting cannot hide a nested credential;
    free-form prose keeps the header and inline-value redaction used by logs.
    """
    if isinstance(payload, (dict, list)):
        return _redact_structured_credentials(payload)
    if not isinstance(payload, str):
        return payload
    try:
        structured = json.loads(payload)
    except json.JSONDecodeError:
        structured = None
    else:
        if isinstance(structured, (dict, list)):
            return json.dumps(
                _redact_structured_credentials(structured),
                ensure_ascii=False,
                separators=(",", ":"),
            )

    # Header values can contain a scheme and spaces (``Bearer secret``), so
    # redact an entire credential header before handling inline key/value forms.
    without_headers = re.sub(
        r"(?im)\b(authorization|cookie)\b\s*:\s*[^\r\n]*",
        r"\1: [REDACTED]",
        _redact_string(payload),
    )
    return re.sub(
        r"(?im)\b(token|secret|password|credential|api[_-]?key)\b\s*([:=])\s*"
        r"(?:(?:bearer|basic)\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n]+)"
        r"|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n]+)",
        r"\1\2 [REDACTED]",
        without_headers,
    )


class EscalationStore:
    """Durable, restart-proof home for launcher identity and parked escalations."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        initialization_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self.path = path or escalation_db_path()
        # Tests use this at the table/index boundary to prove that every DDL
        # phase remains inside the one initialization transaction. It is not a
        # runtime callback or a second schema path.
        self._initialization_hook = initialization_hook
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one connection, commit or roll back, and always close it.

        Closing matters beyond hygiene: SQLite refuses a journal-mode switch
        while any other connection is open, so a leaked connection would keep
        :func:`_adopt_wal` failing for the life of the process.
        """
        conn = self._open()
        try:
            # Statement errors from the body stay raw so each caller keeps
            # raising its own specific EscalationStoreError message.
            with conn:
                yield conn
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        """Connect with the store's fixed pragmas and owner-only file modes."""
        try:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        except sqlite3.Error as exc:
            raise EscalationStoreError(
                f"Cannot open escalation store {self.path}: {exc}"
            ) from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")
            for path in (
                self.path,
                self.path.with_name(f"{self.path.name}-wal"),
                self.path.with_name(f"{self.path.name}-shm"),
            ):
                try:
                    os.chmod(path, 0o600)
                except FileNotFoundError:
                    # SQLite may remove a sidecar after it was enumerated.
                    pass
        except sqlite3.Error as exc:
            conn.close()
            raise EscalationStoreError(
                f"Cannot open escalation store {self.path}: {exc}"
            ) from exc
        return conn

    def _adopt_wal(self, conn: sqlite3.Connection) -> None:
        """Put the database in WAL mode, tolerating a concurrent first open.

        Journal mode is a persistent property of the file, so this only has to
        succeed once - every later opener reads ``wal`` back and does nothing.
        The switch needs an exclusive lock and SQLite deliberately does *not*
        run the busy handler for it, so ``PRAGMA busy_timeout`` cannot cover
        the window where two processes create the store at the same moment.
        Retrying here is what keeps that race off the caller: without it a
        concurrent open fails with a spurious "database is locked".
        """
        deadline = time.monotonic() + WAL_ADOPTION_TIMEOUT_SECONDS
        delay = 0.005
        while True:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            try:
                row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is not None and str(row[0]).lower() == "wal":
                return
            if time.monotonic() >= deadline:
                raise EscalationStoreError(
                    f"Cannot enable WAL on escalation store {self.path}: "
                    "another connection held it locked for "
                    f"{WAL_ADOPTION_TIMEOUT_SECONDS:g}s"
                )
            time.sleep(delay)
            delay = min(delay * 2, 0.25)

    def _initialize(self) -> None:
        try:
            with self._connect() as conn:
                self._adopt_wal(conn)
                # Schema inspection, migration, and every dependent index share
                # one write transaction. A peer opening a fresh database waits
                # here, then observes the completed schema rather than a table
                # created by an initializer that has not reached its indexes.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS launcher_identities (
                        session_id TEXT PRIMARY KEY,
                        workspace_id TEXT,
                        launcher TEXT NOT NULL CHECK (launcher IN ('cdesktop', 'external')),
                        detail TEXT,
                        recorded_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS escalations (
                        escalation_id TEXT PRIMARY KEY,
                        child_session_id TEXT NOT NULL,
                        child_workspace_id TEXT,
                        recorded_parent_session_id TEXT,
                        reason TEXT NOT NULL
                            CHECK (reason IN ('no_parent', 'parent_archived', 'parent_unreachable')),
                        message TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK (status IN ('parked', 'resolved'))
                            DEFAULT 'parked',
                        created_at REAL NOT NULL,
                        resolved_at REAL
                    )
                    """
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_escalations_dedupe "
                    "ON escalations(dedupe_key)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS acknowledgments (
                        ack_id TEXT PRIMARY KEY,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        child_session_id TEXT NOT NULL,
                        parent_session_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('routine', 'interrupt')),
                        intent TEXT NOT NULL CHECK (intent IN ('continue', 'replace')),
                        message TEXT NOT NULL,
                        delivered_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS order_expectations (
                        order_id TEXT PRIMARY KEY,
                        sender_session_id TEXT,
                        recipient_session_id TEXT NOT NULL,
                        body TEXT NOT NULL,
                        body_digest TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        satisfied_at REAL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_order_expectations_recipient "
                    "ON order_expectations(recipient_session_id, satisfied_at, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_escalations_status "
                    "ON escalations(status, created_at DESC)"
                )
                self._migrate_external_run_schema(conn)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_policies (
                        session_id TEXT PRIMARY KEY,
                        conditions_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS terminal_ownerships (
                        session_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL CHECK (state IN ('retired', 'superseded')),
                        reason TEXT NOT NULL,
                        retired_at TEXT NOT NULL,
                        logical_key TEXT,
                        successor_session_id TEXT
                    )
                    """
                )
                conn.execute(f"PRAGMA user_version = {ESCALATION_SCHEMA_VERSION}")
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot initialize escalation store {self.path}: {exc}"
            ) from exc

    def _external_run_schema(self, conn: sqlite3.Connection) -> None:
        """Converge the external-run tables and their dependent indexes."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_run_leases (
                subscription_id TEXT PRIMARY KEY,
                output_root TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('active', 'released')),
                version INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                released_at REAL
            )
            """
        )
        if self._initialization_hook is not None:
            self._initialization_hook(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_run_subscriptions (
                subscription_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                output_root TEXT NOT NULL,
                return_session_id TEXT NOT NULL,
                return_workspace_id TEXT,
                writer_capability_digest TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (state IN
                    ('subscribed', 'running', 'terminal', 'lost', 'delivering', 'notified')),
                version INTEGER NOT NULL DEFAULT 0,
                pid INTEGER,
                process_fingerprint TEXT,
                receipt_path TEXT NOT NULL,
                receipt_digest TEXT,
                outcome TEXT,
                diagnostic TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                terminal_at REAL,
                notified_at REAL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_run_leases_live_root "
            "ON external_run_leases(output_root) WHERE state = 'active'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_run_subscriptions_pending "
            "ON external_run_subscriptions(state, updated_at)"
        )

    def _migrate_external_run_schema(self, conn: sqlite3.Connection) -> None:
        """Replace the original lifetime-root tables without losing evidence."""
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('external_run_leases', 'external_run_subscriptions')"
        ).fetchall()
        existing = {str(row['name']): str(row['sql'] or '') for row in rows}
        expected = {"external_run_leases", "external_run_subscriptions"}
        legacy = (
            'output_root TEXT PRIMARY KEY' in existing.get('external_run_leases', '')
            or 'output_root TEXT NOT NULL UNIQUE' in existing.get('external_run_subscriptions', '')
        )
        if not legacy or set(existing) != expected:
            # Absent and interrupted current schemas use the same convergent
            # path. In particular, an initializer can never leave a follower
            # with the subscription index targeting a missing table.
            self._external_run_schema(conn)
            return
        conn.execute('ALTER TABLE external_run_leases RENAME TO external_run_leases_old')
        conn.execute(
            'ALTER TABLE external_run_subscriptions RENAME TO '
            'external_run_subscriptions_old'
        )
        self._external_run_schema(conn)
        conn.execute(
            "INSERT INTO external_run_leases "
            "(subscription_id, output_root, state, version, created_at, released_at) "
            "SELECT subscription_id, output_root, state, version, created_at, released_at "
            "FROM external_run_leases_old"
        )
        conn.execute(
            "INSERT INTO external_run_subscriptions "
            "SELECT * FROM external_run_subscriptions_old"
        )
        conn.execute('DROP TABLE external_run_leases_old')
        conn.execute('DROP TABLE external_run_subscriptions_old')

    def record_launcher(
        self,
        *,
        session_id: str,
        workspace_id: str | None,
        identity: LauncherIdentity,
    ) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO launcher_identities
                        (session_id, workspace_id, launcher, detail, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        workspace_id = excluded.workspace_id,
                        launcher = excluded.launcher,
                        detail = excluded.detail,
                        recorded_at = excluded.recorded_at
                    """,
                    (
                        session_id,
                        workspace_id,
                        identity.launcher,
                        identity.detail,
                        time.time(),
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot record launcher identity for {session_id}: {exc}"
            ) from exc

    def get_launcher(self, session_id: str) -> LauncherIdentity | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT launcher, detail FROM launcher_identities WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot read launcher identity for {session_id}: {exc}"
            ) from exc
        if row is None:
            return None
        return LauncherIdentity(launcher=row["launcher"], detail=row["detail"])

    def signal_policy(self, session_id: str) -> SignalPolicy:
        """Return the empty default when this session has no policy row."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT conditions_json, updated_at FROM signal_policies WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot read signal policy: {exc}") from exc
        if row is None:
            return SignalPolicy(session_id, (), 0.0)
        try:
            conditions = tuple(str(item) for item in json.loads(row["conditions_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise EscalationStoreError("Signal policy record is invalid") from exc
        return SignalPolicy(session_id, conditions, float(row["updated_at"]))

    def set_signal_policy(
        self, session_id: str, conditions: tuple[str, ...]
    ) -> SignalPolicy:
        """Replace a session policy atomically after validating every condition."""
        canonical = parse_signal_conditions(",".join(conditions))
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO signal_policies (session_id, conditions_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET conditions_json = excluded.conditions_json,
                    updated_at = excluded.updated_at""",
                    (session_id, json.dumps(canonical), now),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot set signal policy: {exc}") from exc
        return SignalPolicy(session_id, canonical, now)

    def clear_signal_policy(self, session_id: str) -> bool:
        try:
            with self._connect() as conn:
                return bool(
                    conn.execute(
                        "DELETE FROM signal_policies WHERE session_id = ?",
                        (session_id,),
                    ).rowcount
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot clear signal policy: {exc}") from exc

    def has_dedupe_key(self, dedupe_key: str) -> bool:
        """Whether a policy signal was already delivered or parked durably."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM acknowledgments WHERE dedupe_key = ? "
                    "UNION SELECT 1 FROM escalations WHERE dedupe_key = ? LIMIT 1",
                    (dedupe_key, dedupe_key),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot read delivery dedupe state: {exc}"
            ) from exc
        return row is not None

    def has_terminal_dedupe_key(self, dedupe_key: str) -> bool:
        """Whether delivery for this logical signal is conclusively finished."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM acknowledgments WHERE dedupe_key = ? "
                    "UNION SELECT 1 FROM escalations "
                    "WHERE dedupe_key = ? AND status = 'resolved' LIMIT 1",
                    (dedupe_key, dedupe_key),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot read terminal delivery dedupe state: {exc}"
            ) from exc
        return row is not None

    def park(
        self,
        *,
        child_session_id: str,
        child_workspace_id: str | None,
        recorded_parent_session_id: str | None,
        reason: str,
        message: str,
        dedupe_key: str,
    ) -> ParkedEscalation:
        if reason not in PARK_REASONS:
            raise ValueError(f"Unknown park reason: {reason}")
        message = redact_credentials(message)
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO escalations (
                        escalation_id, child_session_id, child_workspace_id,
                        recorded_parent_session_id, reason, message, dedupe_key,
                        status, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'parked', ?, NULL)
                    """,
                    (
                        str(uuid.uuid4()),
                        child_session_id,
                        child_workspace_id,
                        recorded_parent_session_id,
                        reason,
                        message,
                        dedupe_key,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM escalations WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot park escalation: {exc}") from exc
        if row is None:
            raise EscalationStoreError(
                f"Escalation is missing after park: {dedupe_key}"
            )
        return _from_row(row)

    def resolve(self, escalation_id: str) -> ParkedEscalation:
        completed_at = time.time()
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE escalations
                    SET status = 'resolved', resolved_at = ?
                    WHERE escalation_id = ? AND status = 'parked'
                    """,
                    (completed_at, escalation_id),
                )
                if cursor.rowcount != 1:
                    raise EscalationStoreError(
                        f"Escalation is missing or already resolved: {escalation_id}"
                    )
                row = conn.execute(
                    "SELECT * FROM escalations WHERE escalation_id = ?",
                    (escalation_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot resolve escalation: {exc}") from exc
        if row is None:
            raise EscalationStoreError(f"Escalation is missing: {escalation_id}")
        return _from_row(row)

    def resolve_dedupe_key(self, dedupe_key: str) -> ParkedEscalation | None:
        """Close a parked record once its wake was delivered after all.

        Returns the record under the key, or None when the key was never
        parked - so a caller that had a live destination all along can call
        this unconditionally.
        """
        completed_at = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE escalations
                    SET status = 'resolved', resolved_at = ?
                    WHERE dedupe_key = ? AND status = 'parked'
                    """,
                    (completed_at, dedupe_key),
                )
                row = conn.execute(
                    "SELECT * FROM escalations WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot resolve escalation: {exc}") from exc
        return _from_row(row) if row is not None else None

    def acknowledge(
        self,
        *,
        child_session_id: str,
        parent_session_id: str,
        kind: str,
        intent: str,
        message: str,
        dedupe_key: str,
    ) -> DeliveryAck:
        if kind not in ESCALATION_KINDS:
            raise ValueError(f"Unknown escalation kind: {kind}")
        if intent not in DELIVERY_INTENTS:
            raise ValueError(f"Unknown delivery intent: {intent}")
        message = redact_credentials(message)
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO acknowledgments (
                        ack_id, dedupe_key, child_session_id, parent_session_id,
                        kind, intent, message, delivered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        dedupe_key,
                        child_session_id,
                        parent_session_id,
                        kind,
                        intent,
                        message,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM acknowledgments WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot record acknowledgment: {exc}") from exc
        if row is None:
            raise EscalationStoreError(
                f"Acknowledgment is missing after record: {dedupe_key}"
            )
        return _ack_from_row(row)

    def acknowledgments(self, *, limit: int = 100) -> list[DeliveryAck]:
        if limit < 1 or limit > 1000:
            raise ValueError("Acknowledgment limit must be between 1 and 1000")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM acknowledgments
                    ORDER BY delivered_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot read acknowledgments: {exc}") from exc
        return [_ack_from_row(row) for row in rows]

    def expect_order(
        self,
        *,
        order_id: str | None,
        sender_session_id: str | None,
        recipient_session_id: str,
        body: str,
    ) -> OrderExpectation:
        """Record the one durable promise made by an ordered follow-up."""
        safe_body = redact_credentials(body)
        key = order_id or f"order:{uuid.uuid4()}"
        now = time.time()
        digest = hashlib.sha256(safe_body.encode("utf-8")).hexdigest()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO order_expectations (
                        order_id, sender_session_id, recipient_session_id, body,
                        body_digest, created_at, satisfied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        key,
                        sender_session_id,
                        recipient_session_id,
                        safe_body,
                        digest,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM order_expectations WHERE order_id = ?", (key,)
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot record order expectation: {exc}"
            ) from exc
        if row is None:
            raise EscalationStoreError(
                f"Order expectation is missing after record: {key}"
            )
        return _order_from_row(row)

    def satisfy_orders(
        self, recipient_session_id: str, *, order_id: str | None = None
    ) -> int:
        """Any later outbound report closes the recipient's outstanding orders."""
        now = time.time()
        where = "recipient_session_id = ? AND satisfied_at IS NULL"
        values: list[object] = [now, recipient_session_id]
        if order_id is not None:
            where += " AND order_id = ?"
            values.append(order_id)
        try:
            with self._connect() as conn:
                return conn.execute(
                    f"UPDATE order_expectations SET satisfied_at = ? WHERE {where}",
                    values,
                ).rowcount
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot satisfy order expectation: {exc}"
            ) from exc

    def _order_filter(
        self,
        *,
        recipient_session_id: str | None,
        unmet_only: bool,
        recipient_session_ids: Collection[str] | None,
    ) -> tuple[str, list[object]]:
        where: list[str] = []
        values: list[object] = []
        if recipient_session_id is not None:
            where.append("recipient_session_id = ?")
            values.append(recipient_session_id)
        if recipient_session_ids is not None:
            recipients = sorted({str(item) for item in recipient_session_ids})
            if not recipients:
                return " WHERE 0", []
            placeholders = ", ".join("?" for _ in recipients)
            where.append(f"recipient_session_id IN ({placeholders})")
            values.extend(recipients)
        if unmet_only:
            where.append("satisfied_at IS NULL")
        return (" WHERE " + " AND ".join(where)) if where else "", values

    def orders(
        self,
        *,
        recipient_session_id: str | None = None,
        unmet_only: bool = False,
        recipient_session_ids: Collection[str] | None = None,
        limit: int | None = None,
    ) -> list[OrderExpectation]:
        """Read order expectations, oldest first.

        ``limit`` keeps the *newest* rows and still returns them oldest
        first: an unbounded read of this table emitted every unmet order the
        host had ever recorded (33k rows in one measured case), and the old
        ones are stale noise while the recent ones are the live question.
        """
        if limit is not None and limit < 1:
            raise ValueError("Order expectation limit must be positive")
        clause, values = self._order_filter(
            recipient_session_id=recipient_session_id,
            unmet_only=unmet_only,
            recipient_session_ids=recipient_session_ids,
        )
        query = f"SELECT * FROM order_expectations{clause} ORDER BY created_at"
        if limit is None:
            query += " ASC"
        else:
            query += " DESC LIMIT ?"
            values = [*values, limit]
        try:
            with self._connect() as conn:
                rows = conn.execute(query, values).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot read order expectations: {exc}") from exc
        if limit is not None:
            rows = list(reversed(rows))
        return [_order_from_row(row) for row in rows]

    def unacked_counts(
        self, *, recipient_session_ids: Collection[str] | None = None
    ) -> dict[str, int]:
        """Total parked escalations and unmet orders.

        A bounded read needs these to say how much it suppressed, instead of
        showing a truncated list as if it were the whole queue.
        """
        clause, values = self._order_filter(
            recipient_session_id=None,
            unmet_only=True,
            recipient_session_ids=recipient_session_ids,
        )
        try:
            with self._connect() as conn:
                parked = conn.execute(
                    "SELECT COUNT(*) FROM escalations WHERE status = 'parked'"
                ).fetchone()[0]
                unmet = conn.execute(
                    f"SELECT COUNT(*) FROM order_expectations{clause}", values
                ).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(
                f"Cannot count unacknowledged deliveries: {exc}"
            ) from exc
        return {"parked_escalation": int(parked), "unmet_order": int(unmet)}

    def pending(self, *, limit: int = 100) -> list[ParkedEscalation]:
        if limit < 1 or limit > 1000:
            raise ValueError("Escalation pending limit must be between 1 and 1000")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM escalations
                    WHERE status = 'parked'
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot read parked escalations: {exc}") from exc
        return [_from_row(row) for row in reversed(rows)]


def _ack_from_row(row: sqlite3.Row) -> DeliveryAck:
    return DeliveryAck(
        ack_id=row["ack_id"],
        dedupe_key=row["dedupe_key"],
        child_session_id=row["child_session_id"],
        parent_session_id=row["parent_session_id"],
        kind=row["kind"],
        intent=row["intent"],
        message=row["message"],
        delivered_at=row["delivered_at"],
    )


def _from_row(row: sqlite3.Row) -> ParkedEscalation:
    return ParkedEscalation(
        escalation_id=row["escalation_id"],
        child_session_id=row["child_session_id"],
        child_workspace_id=row["child_workspace_id"],
        recorded_parent_session_id=row["recorded_parent_session_id"],
        reason=row["reason"],
        message=row["message"],
        dedupe_key=row["dedupe_key"],
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def _order_from_row(row: sqlite3.Row) -> OrderExpectation:
    return OrderExpectation(
        order_id=row["order_id"],
        sender_session_id=row["sender_session_id"],
        recipient_session_id=row["recipient_session_id"],
        body=row["body"],
        body_digest=row["body_digest"],
        created_at=row["created_at"],
        satisfied_at=row["satisfied_at"],
    )


def default_dedupe_key(child_session_id: str, message: str) -> str:
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return f"parent-escalation:{child_session_id}:{digest}"


def escalate(
    client: CdesktopClient,
    *,
    child_session_id: str,
    child_workspace_id: str | None,
    parent_session_id: str | None,
    message: str,
    store: EscalationStore | None = None,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """Deliver to a confirmed live, non-archived parent, or durably park.

    There is no third outcome: an escalation is always either delivered or
    recorded in the decision inbox. A parked record's recorded parent is
    never re-targeted automatically once that parent's workspace is
    archived -- superseded or completed sessions must never be auto-resumed
    by queued delivery.

    Delivery intent follows :func:`classify_escalation`: routine
    STATUS/completion callbacks queue with ``intent=continue`` and a durable
    acknowledgment, preserving the recipient's active turn; explicit
    BLOCKED/DECISION escalations replace it.
    """
    store = store or EscalationStore()
    key = dedupe_key or default_dedupe_key(child_session_id, message)
    classification = classify_escalation(message)
    reason: str
    if parent_session_id == child_session_id:
        reason = "parent_unreachable"
    elif parent_session_id:
        try:
            parent = client.session(parent_session_id)
            workspace = client.workspace(str(parent["workspace_id"]))
        except CdesktopError:
            reason = "parent_unreachable"
        else:
            if workspace.get("archived"):
                reason = "parent_archived"
            else:
                follow_up = client.send(
                    parent_session_id,
                    message,
                    child_session_id,
                    dedupe_key=key,
                    intent=classification.intent,
                )
                ack = store.acknowledge(
                    child_session_id=child_session_id,
                    parent_session_id=parent_session_id,
                    kind=classification.kind,
                    intent=classification.intent,
                    message=message,
                    dedupe_key=key,
                )
                return {
                    "delivered": True,
                    "kind": classification.kind,
                    "intent": classification.intent,
                    "parent_session_id": parent_session_id,
                    "parent_workspace_id": str(workspace["id"]),
                    "acknowledgment": ack.to_dict(),
                    "follow_up": follow_up,
                }
    else:
        reason = "no_parent"
    parked = store.park(
        child_session_id=child_session_id,
        child_workspace_id=child_workspace_id,
        recorded_parent_session_id=parent_session_id,
        reason=reason,
        message=message,
        dedupe_key=key,
    )
    return {"delivered": False, "reason": reason, "parked": parked.to_dict()}
