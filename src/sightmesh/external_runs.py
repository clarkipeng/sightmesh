"""Durable wake subscriptions for runner-owned external processes.

The runner launches and owns a process, writes an append-only terminal receipt,
and may bind the process fingerprint.  This module holds only an output-root
lease and a return address.  It consequently cannot launch, stop, retry, or
interpret the external run by accident.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .cdesktop import CdesktopClient, CdesktopError
from .escalation import EscalationStore, EscalationStoreError, escalate

RECEIPT_NAME = "terminal-receipt.json"
TERMINAL_OUTCOMES = frozenset({"completed", "failed"})
OPEN_STATES = frozenset({"subscribed", "running"})


class ExternalRunError(RuntimeError):
    pass


class StaleExternalRunTransition(ExternalRunError):
    """A guarded write observed a newer durable subscription row."""


@dataclass(frozen=True)
class ExternalRun:
    subscription_id: str
    run_id: str
    output_root: str
    return_session_id: str
    return_workspace_id: str | None
    dedupe_key: str
    state: str
    version: int
    pid: int | None
    process_fingerprint: str | None
    receipt_path: str
    receipt_digest: str | None
    outcome: str | None
    diagnostic: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class SubscribeResult:
    run: ExternalRun
    writer_capability: str

    def to_dict(self) -> dict[str, object]:
        value = self.run.to_dict()
        value["writer_capability"] = self.writer_capability
        return value


@dataclass(frozen=True)
class Receipt:
    outcome: str
    digest: str


def observe_process_fingerprint(pid: int) -> str | None:
    """Read a portable start-time fingerprint, never an exit-status guess."""
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip() if completed.returncode == 0 else ""
    return value or None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _root(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.exists() and path.is_symlink():
        raise ExternalRunError("output root must not be a symlink")
    return path.resolve(strict=False)


def _receipt(run: ExternalRun) -> Receipt | None:
    try:
        raw = Path(run.receipt_path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalRunError(f"invalid terminal receipt: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ExternalRunError("terminal receipt schema_version must be 1")
    if (
        value.get("subscription_id") != run.subscription_id
        or value.get("run_id") != run.run_id
    ):
        raise ExternalRunError("terminal receipt identifies a different run")
    outcome = value.get("terminal_state")
    if outcome not in TERMINAL_OUTCOMES:
        raise ExternalRunError("terminal receipt outcome is invalid")
    finished = value.get("finished_at")
    try:
        datetime.fromisoformat(str(finished))
    except ValueError as exc:
        raise ExternalRunError("terminal receipt finished_at must be RFC3339") from exc
    return Receipt(str(outcome), hashlib.sha256(raw).hexdigest())


class ExternalRunStore:
    """Version-fenced rows and output leases in the escalation database."""

    def __init__(self, path: Path | None = None) -> None:
        self.escalations = EscalationStore(path)
        self.path = self.escalations.path

    def subscribe(
        self,
        *,
        run_id: str,
        output_root: str | Path,
        return_session_id: str,
        return_workspace_id: str | None = None,
    ) -> SubscribeResult:
        if not run_id or not return_session_id:
            raise ExternalRunError("run_id and return_session must not be empty")
        root = _root(output_root)
        subscription_id, capability, now = (
            str(uuid.uuid4()),
            secrets.token_urlsafe(32),
            time.time(),
        )
        created = False
        try:
            with self.escalations._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if root.exists():
                    if not root.is_dir() or any(root.iterdir()):
                        raise ExternalRunError("output root must be an empty directory")
                else:
                    root.mkdir(parents=True, mode=0o700)
                    created = True
                root.chmod(0o700)
                conn.execute(
                    "INSERT INTO external_run_leases (subscription_id, output_root, state, created_at) VALUES (?, ?, 'active', ?)",
                    (subscription_id, str(root), now),
                )
                conn.execute(
                    """INSERT INTO external_run_subscriptions
                    (subscription_id, run_id, output_root, return_session_id, return_workspace_id,
                     writer_capability_digest, dedupe_key, state, receipt_path, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'subscribed', ?, ?, ?)""",
                    (
                        subscription_id,
                        run_id,
                        str(root),
                        return_session_id,
                        return_workspace_id,
                        _digest(capability),
                        f"external-run:{subscription_id}",
                        str(root / RECEIPT_NAME),
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            if created:
                root.rmdir()
            raise ExternalRunError("run ID or output root is already leased") from exc
        except Exception:
            if created and root.exists():
                root.rmdir()
            raise
        return SubscribeResult(self.get(subscription_id), capability)

    def get(self, subscription_id: str) -> ExternalRun:
        with self.escalations._connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_run_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        if row is None:
            raise ExternalRunError(
                f"unknown external run subscription: {subscription_id}"
            )
        return _run(row)

    def find(self, value: str) -> ExternalRun:
        with self.escalations._connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_run_subscriptions WHERE subscription_id = ? OR run_id = ?",
                (value, value),
            ).fetchone()
        if row is None:
            raise ExternalRunError(f"unknown external run: {value}")
        return _run(row)

    def pending(self) -> list[ExternalRun]:
        with self.escalations._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM external_run_subscriptions WHERE state != 'notified' ORDER BY created_at"
            ).fetchall()
        return [_run(row) for row in rows]

    def bind(
        self,
        subscription_id: str,
        *,
        writer_capability: str,
        pid: int,
        process_fingerprint: str,
        expect_version: int,
        observer: Callable[[int], str | None] = observe_process_fingerprint,
    ) -> ExternalRun:
        if pid <= 0 or not process_fingerprint or observer(pid) != process_fingerprint:
            raise ExternalRunError("process fingerprint is not live at bind")
        run = self.get(subscription_id)
        if _digest(writer_capability) != self._writer_digest(subscription_id):
            raise ExternalRunError("writer capability mismatch")
        if (
            run.state == "running"
            and run.pid == pid
            and run.process_fingerprint == process_fingerprint
        ):
            return run
        return self._transition(
            run,
            OPEN_STATES - {"running"},
            "running",
            expect_version,
            "pid = ?, process_fingerprint = ?",
            (pid, process_fingerprint),
        )

    def preserve_terminal(self, run: ExternalRun, receipt: Receipt) -> ExternalRun:
        if run.receipt_digest:
            if run.receipt_digest != receipt.digest:
                return self._diagnose(run, "duplicate terminal receipt differs")
            return run
        return self._transition(
            run,
            OPEN_STATES,
            "terminal",
            run.version,
            "receipt_digest = ?, outcome = ?, diagnostic = NULL, terminal_at = ?",
            (receipt.digest, receipt.outcome, time.time()),
        )

    def preserve_lost(self, run: ExternalRun, diagnostic: str) -> ExternalRun:
        return self._transition(
            run,
            OPEN_STATES,
            "lost",
            run.version,
            "outcome = 'lost/unknown', diagnostic = ?, terminal_at = ?",
            (diagnostic, time.time()),
        )

    def begin_delivery(self, run: ExternalRun) -> ExternalRun:
        if run.state == "delivering":
            return run
        return self._transition(
            run, frozenset({"terminal", "lost"}), "delivering", run.version, "", ()
        )

    def mark_notified(self, run: ExternalRun) -> ExternalRun:
        with self.escalations._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            cursor = conn.execute(
                "UPDATE external_run_subscriptions SET state = 'notified', notified_at = ?, "
                "version = version + 1, updated_at = ? WHERE subscription_id = ? "
                "AND state = 'delivering' AND version = ?",
                (now, now, run.subscription_id, run.version),
            )
            if not cursor.rowcount:
                conn.execute("ROLLBACK")
                current = self.get(run.subscription_id)
                if current.state == "notified":
                    return current
                raise StaleExternalRunTransition(
                    f"stale notified transition for {run.subscription_id}; current={current.state}"
                )
            conn.execute(
                "UPDATE external_run_leases SET state = 'released', released_at = ?, version = version + 1 WHERE subscription_id = ? AND state = 'active'",
                (now, run.subscription_id),
            )
            conn.execute("COMMIT")
        return self.get(run.subscription_id)

    def _writer_digest(self, subscription_id: str) -> str:
        with self.escalations._connect() as conn:
            row = conn.execute(
                "SELECT writer_capability_digest FROM external_run_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()
        if row is None:
            raise ExternalRunError(
                f"unknown external run subscription: {subscription_id}"
            )
        return str(row[0])

    def _diagnose(self, run: ExternalRun, message: str) -> ExternalRun:
        with self.escalations._connect() as conn:
            conn.execute(
                "UPDATE external_run_subscriptions SET diagnostic = ?, updated_at = ? WHERE subscription_id = ? AND version = ?",
                (message, time.time(), run.subscription_id, run.version),
            )
        return self.get(run.subscription_id)

    def _transition(
        self,
        run: ExternalRun,
        states: frozenset[str],
        target: str,
        expected: int,
        assign: str,
        values: tuple[object, ...],
    ) -> ExternalRun:
        choices = ", ".join("?" for _ in states)
        assignments = f"state = ?, {assign}, " if assign else "state = ?, "
        with self.escalations._connect() as conn:
            cursor = conn.execute(
                f"UPDATE external_run_subscriptions SET {assignments}version = version + 1, updated_at = ? "
                f"WHERE subscription_id = ? AND state IN ({choices}) AND version = ?",
                (
                    target,
                    *values,
                    time.time(),
                    run.subscription_id,
                    *sorted(states),
                    expected,
                ),
            )
        if not cursor.rowcount:
            current = self.get(run.subscription_id)
            raise StaleExternalRunTransition(
                f"stale {target} transition for {run.subscription_id}; current={current.state}"
            )
        return self.get(run.subscription_id)


class ExternalRunReconciler:
    def __init__(
        self,
        client: CdesktopClient,
        store: ExternalRunStore | None = None,
        *,
        observer: Callable[[int], str | None] = observe_process_fingerprint,
    ) -> None:
        self.client, self.store, self.observer = (
            client,
            store or ExternalRunStore(),
            observer,
        )

    def reconcile(self) -> int:
        return sum(self.reconcile_one(run) for run in self.store.pending())

    def reconcile_one(self, value: ExternalRun | str) -> int:
        run = (
            self.store.find(value)
            if isinstance(value, str)
            else self.store.get(value.subscription_id)
        )
        try:
            receipt = _receipt(run)
        except ExternalRunError as exc:
            if run.state == "running" and not self._live(run):
                run = self.store.preserve_lost(
                    run, f"receipt unreadable after process disappearance: {exc}"
                )
            else:
                return 0
        else:
            if receipt is not None:
                run = self.store.preserve_terminal(run, receipt)
            elif run.state == "running" and not self._live(run):
                run = self.store.preserve_lost(
                    run, "process fingerprint disappeared without terminal receipt"
                )
        if run.state not in {"terminal", "lost", "delivering"}:
            return 0
        run = self.store.begin_delivery(run)
        message = f"STATUS: external run {run.run_id} {run.outcome}; output_root={run.output_root}; receipt={run.receipt_path}"
        try:
            result = escalate(
                self.client,
                child_session_id=f"external-run:{run.subscription_id}",
                child_workspace_id=run.return_workspace_id,
                parent_session_id=run.return_session_id,
                message=message,
                store=self.store.escalations,
                dedupe_key=run.dedupe_key,
            )
        except (CdesktopError, OSError, EscalationStoreError):
            return 0
        if result.get("delivered"):
            self.store.mark_notified(run)
            return 1
        return 0

    def _live(self, run: ExternalRun) -> bool:
        return bool(
            run.pid
            and run.process_fingerprint
            and self.observer(run.pid) == run.process_fingerprint
        )


def _run(row: sqlite3.Row) -> ExternalRun:
    return ExternalRun(**{name: row[name] for name in ExternalRun.__dataclass_fields__})
