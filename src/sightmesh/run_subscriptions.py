from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .cdesktop import CdesktopClient, CdesktopError
from .escalation import EscalationStore, EscalationStoreError, escalate

RECEIPT_NAME = "terminal-receipt.json"
TERMINAL_STATES = frozenset({"completed", "failed"})


class RunSubscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunSubscription:
    subscription_id: str
    run_id: str
    output_root: str
    return_session_id: str
    return_workspace_id: str | None
    dedupe_key: str
    state: str
    pid: int | None
    process_start: str | None
    created_at: float
    updated_at: float
    bound_at: float | None
    terminal_state: str | None
    exit_code: int | None
    finished_at: str | None
    receipt_path: str
    receipt_digest: str | None
    diagnostic: str | None
    lost_at: float | None
    lease_released_at: float | None
    notified_at: float | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "run_id": self.run_id,
            "output_root": self.output_root,
            "return_session_id": self.return_session_id,
            "return_workspace_id": self.return_workspace_id,
            "dedupe_key": self.dedupe_key,
            "state": self.state,
            "pid": self.pid,
            "process_start": self.process_start,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "bound_at": self.bound_at,
            "terminal_state": self.terminal_state,
            "exit_code": self.exit_code,
            "finished_at": self.finished_at,
            "receipt_path": self.receipt_path,
            "receipt_digest": self.receipt_digest,
            "diagnostic": self.diagnostic,
            "lost_at": self.lost_at,
            "lease_released_at": self.lease_released_at,
            "notified_at": self.notified_at,
        }


@dataclass(frozen=True)
class SubscribeResult:
    subscription: RunSubscription
    writer_capability: str

    def to_dict(self) -> dict[str, Any]:
        data = self.subscription.to_public_dict()
        data["writer_capability"] = self.writer_capability
        return data


@dataclass(frozen=True)
class Receipt:
    terminal_state: str
    exit_code: int | None
    finished_at: str
    digest: str


def observe_process_start(pid: int) -> str | None:
    """Return the platform process-start token for a live PID, or None."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def _capability_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_output_root(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if raw.exists() and raw.is_symlink():
        raise RunSubscriptionError("Output root must not be a symlink")
    return raw.resolve(strict=False)


def _prepare_output_root(path: str | Path) -> tuple[str, bool]:
    root = _canonical_output_root(path)
    created = False
    if root.exists():
        if not root.is_dir():
            raise RunSubscriptionError("Output root must be a directory")
        if any(root.iterdir()):
            raise RunSubscriptionError("Output root must be empty before subscription")
    else:
        try:
            root.mkdir(parents=True, mode=0o700)
            created = True
        except FileExistsError:
            if not root.is_dir() or any(root.iterdir()):
                raise RunSubscriptionError(
                    "Output root must be an empty directory"
                ) from None
    root.chmod(0o700)
    return str(root), created


def _parse_rfc3339(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RunSubscriptionError("Receipt finished_at must be an RFC3339 string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunSubscriptionError("Receipt finished_at must be RFC3339") from exc
    return value


def _receipt_at(subscription: RunSubscription) -> Receipt | None:
    path = Path(subscription.receipt_path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunSubscriptionError(f"Cannot read terminal receipt: {exc}") from exc
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunSubscriptionError(f"Invalid terminal receipt: {exc}") from exc
    if not isinstance(data, dict):
        raise RunSubscriptionError("Terminal receipt must be a JSON object")
    if data.get("schema_version") != 1:
        raise RunSubscriptionError("Terminal receipt schema_version must be 1")
    if data.get("subscription_id") != subscription.subscription_id:
        raise RunSubscriptionError("Terminal receipt subscription_id mismatch")
    if data.get("run_id") != subscription.run_id:
        raise RunSubscriptionError("Terminal receipt run_id mismatch")
    terminal_state = data.get("terminal_state")
    if terminal_state not in TERMINAL_STATES:
        raise RunSubscriptionError("Terminal receipt terminal_state is invalid")
    exit_code = data.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        raise RunSubscriptionError("Terminal receipt exit_code must be an integer or null")
    return Receipt(
        terminal_state=str(terminal_state),
        exit_code=exit_code,
        finished_at=_parse_rfc3339(data.get("finished_at")),
        digest=hashlib.sha256(raw).hexdigest(),
    )


class RunSubscriptionStore:
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
        if not run_id.strip():
            raise RunSubscriptionError("run_id must not be empty")
        if not return_session_id.strip():
            raise RunSubscriptionError("return_session must not be empty")
        canonical_root = str(_canonical_output_root(output_root))
        created = False
        subscription_id = str(uuid.uuid4())
        capability = secrets.token_urlsafe(32)
        now = time.time()
        try:
            with self.escalations._connect() as conn:
                canonical_root, created = _prepare_output_root(canonical_root)
                conn.execute(
                    """
                    INSERT INTO run_subscriptions (
                        subscription_id, run_id, output_root, return_session_id,
                        return_workspace_id, writer_capability_digest, dedupe_key,
                        state, created_at, updated_at, receipt_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'subscribed', ?, ?, ?)
                    """,
                    (
                        subscription_id,
                        run_id,
                        canonical_root,
                        return_session_id,
                        return_workspace_id,
                        _capability_digest(capability),
                        f"run-wake:{subscription_id}",
                        now,
                        now,
                        str(Path(canonical_root) / RECEIPT_NAME),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if created and not self._root_is_claimed(canonical_root):
                Path(canonical_root).rmdir()
            raise RunSubscriptionError(
                "Run ID or output root is already subscribed"
            ) from exc
        except sqlite3.DatabaseError as exc:
            if created:
                Path(canonical_root).rmdir()
            raise EscalationStoreError(f"Cannot create run subscription: {exc}") from exc
        return SubscribeResult(self.get(subscription_id), capability)

    def _root_is_claimed(self, canonical_root: str) -> bool:
        """Protect a concurrent winner's directory from loser cleanup."""
        try:
            with self.escalations._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM run_subscriptions WHERE output_root = ?",
                    (canonical_root,),
                ).fetchone()
        except sqlite3.DatabaseError:
            return True
        return row is not None

    def bind(
        self,
        subscription_id: str,
        *,
        writer_capability: str,
        pid: int,
        process_start: str,
        observer: Callable[[int], str | None] | None = None,
    ) -> RunSubscription:
        if pid <= 0:
            raise RunSubscriptionError("pid must be positive")
        if not process_start.strip():
            raise RunSubscriptionError("process_start must not be empty")
        try:
            with self.escalations._connect() as conn:
                authorized = conn.execute(
                    "SELECT writer_capability_digest FROM run_subscriptions "
                    "WHERE subscription_id = ?",
                    (subscription_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot authorize run binding: {exc}") from exc
        if authorized is None:
            raise RunSubscriptionError(f"No run subscription: {subscription_id}")
        if authorized["writer_capability_digest"] != _capability_digest(
            writer_capability
        ):
            raise RunSubscriptionError("Writer capability mismatch")
        observer = observer or observe_process_start
        live_start = observer(pid)
        if live_start != process_start:
            raise RunSubscriptionError("Process fingerprint is not live at bind")
        now = time.time()
        try:
            with self.escalations._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM run_subscriptions WHERE subscription_id = ?",
                    (subscription_id,),
                ).fetchone()
                if row is None:
                    raise RunSubscriptionError(f"No run subscription: {subscription_id}")
                if row["writer_capability_digest"] != _capability_digest(writer_capability):
                    raise RunSubscriptionError("Writer capability mismatch")
                if row["state"] == "running":
                    if row["pid"] == pid and row["process_start"] == process_start:
                        return _subscription_from_row(row)
                    raise RunSubscriptionError("Run subscription is already bound")
                if row["state"] != "subscribed":
                    raise RunSubscriptionError(
                        f"Cannot bind run subscription in state {row['state']}"
                    )
                conn.execute(
                    """
                    UPDATE run_subscriptions
                    SET state = 'running', pid = ?, process_start = ?,
                        bound_at = ?, updated_at = ?
                    WHERE subscription_id = ?
                    """,
                    (pid, process_start, now, now, subscription_id),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot bind run subscription: {exc}") from exc
        return self.get(subscription_id)

    def get(self, subscription_id: str) -> RunSubscription:
        try:
            with self.escalations._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM run_subscriptions WHERE subscription_id = ?",
                    (subscription_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot read run subscription: {exc}") from exc
        if row is None:
            raise RunSubscriptionError(f"No run subscription: {subscription_id}")
        return _subscription_from_row(row)

    def find(self, value: str) -> RunSubscription:
        try:
            with self.escalations._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM run_subscriptions
                    WHERE subscription_id = ? OR run_id = ?
                    """,
                    (value, value),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot read run subscription: {exc}") from exc
        if row is None:
            raise RunSubscriptionError(f"No run subscription: {value}")
        return _subscription_from_row(row)

    def list_active(self) -> list[RunSubscription]:
        try:
            with self.escalations._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM run_subscriptions
                    WHERE state != 'notified'
                    ORDER BY created_at ASC
                    """
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot list run subscriptions: {exc}") from exc
        return [_subscription_from_row(row) for row in rows]

    def all(self) -> list[RunSubscription]:
        try:
            with self.escalations._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM run_subscriptions ORDER BY created_at ASC"
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot list run subscriptions: {exc}") from exc
        return [_subscription_from_row(row) for row in rows]

    def preserve_terminal(
        self, subscription: RunSubscription, receipt: Receipt
    ) -> RunSubscription:
        if subscription.receipt_digest:
            if subscription.receipt_digest == receipt.digest:
                return subscription
            return self._diagnose(
                subscription.subscription_id, "duplicate terminal receipt differs"
            )
        if subscription.state not in {"subscribed", "running"}:
            return subscription
        now = time.time()
        try:
            with self.escalations._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_subscriptions
                    SET state = 'terminal', terminal_state = ?, exit_code = ?,
                        finished_at = ?, receipt_digest = ?, diagnostic = NULL,
                        lease_released_at = ?, updated_at = ?
                    WHERE subscription_id = ? AND state IN ('subscribed', 'running')
                    """,
                    (
                        receipt.terminal_state,
                        receipt.exit_code,
                        receipt.finished_at,
                        receipt.digest,
                        now,
                        now,
                        subscription.subscription_id,
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot preserve terminal receipt: {exc}") from exc
        return self.get(subscription.subscription_id)

    def preserve_lost(self, subscription: RunSubscription, diagnostic: str) -> RunSubscription:
        if subscription.state not in {"subscribed", "running"}:
            return subscription
        now = time.time()
        try:
            with self.escalations._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_subscriptions
                    SET state = 'lost', terminal_state = NULL, exit_code = NULL,
                        finished_at = NULL, diagnostic = ?, lost_at = ?,
                        lease_released_at = ?, updated_at = ?
                    WHERE subscription_id = ? AND state IN ('subscribed', 'running')
                    """,
                    (diagnostic, now, now, now, subscription.subscription_id),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot preserve run loss: {exc}") from exc
        return self.get(subscription.subscription_id)

    def mark_notified(self, subscription_id: str) -> RunSubscription:
        now = time.time()
        try:
            with self.escalations._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_subscriptions
                    SET state = 'notified', notified_at = ?, updated_at = ?
                    WHERE subscription_id = ? AND state IN ('terminal', 'lost')
                    """,
                    (now, now, subscription_id),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot mark run notified: {exc}") from exc
        return self.get(subscription_id)

    def _diagnose(self, subscription_id: str, diagnostic: str) -> RunSubscription:
        try:
            with self.escalations._connect() as conn:
                conn.execute(
                    """
                    UPDATE run_subscriptions
                    SET diagnostic = ?, updated_at = ?
                    WHERE subscription_id = ?
                    """,
                    (diagnostic, time.time(), subscription_id),
                )
        except sqlite3.DatabaseError as exc:
            raise EscalationStoreError(f"Cannot update run diagnostic: {exc}") from exc
        return self.get(subscription_id)


class RunReconciler:
    def __init__(
        self,
        client: CdesktopClient,
        store: RunSubscriptionStore | None = None,
        *,
        observer: Callable[[int], str | None] | None = None,
    ) -> None:
        self.client = client
        self.store = store or RunSubscriptionStore()
        self.observer = observer or observe_process_start

    def reconcile(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for subscription in self.store.list_active():
            try:
                results.append(self.reconcile_one(subscription))
            except (OSError, RuntimeError) as exc:
                results.append(
                    {
                        "subscription_id": subscription.subscription_id,
                        "state": subscription.state,
                        "error": str(exc),
                    }
                )
        return results

    def reconcile_one(self, subscription: RunSubscription | str) -> dict[str, Any]:
        record = (
            self.store.find(subscription)
            if isinstance(subscription, str)
            else self.store.get(subscription.subscription_id)
        )
        try:
            receipt = _receipt_at(record)
        except RunSubscriptionError as exc:
            receipt_error = str(exc)
            receipt = None
        else:
            receipt_error = None

        if receipt is not None:
            record = self.store.preserve_terminal(record, receipt)
        elif receipt_error:
            live = self._fingerprint_live(record)
            if record.pid is not None and live:
                record = self.store._diagnose(record.subscription_id, receipt_error)
                return {"subscription_id": record.subscription_id, "state": record.state}
            record = self.store.preserve_lost(record, receipt_error)
        elif record.state == "running" and not self._fingerprint_live(record):
            # The runner may publish its receipt immediately before exit. Re-read
            # after the negative liveness observation so terminal evidence wins.
            receipt = _receipt_at(record)
            if receipt is not None:
                record = self.store.preserve_terminal(record, receipt)
            else:
                record = self.store.preserve_lost(
                    record, "process fingerprint disappeared"
                )

        if record.state in {"terminal", "lost"}:
            delivered = self._deliver(record)
            if delivered:
                record = self.store.mark_notified(record.subscription_id)
        return {"subscription_id": record.subscription_id, "state": record.state}

    def _fingerprint_live(self, subscription: RunSubscription) -> bool:
        if subscription.pid is None or subscription.process_start is None:
            return True
        return self.observer(subscription.pid) == subscription.process_start

    def _deliver(self, subscription: RunSubscription) -> bool:
        outcome = (
            f"terminal/{subscription.terminal_state}"
            if subscription.state == "terminal"
            else "lost/unknown"
        )
        message = (
            f"STATUS: run {subscription.run_id} {outcome}; "
            f"output_root={subscription.output_root}; receipt={subscription.receipt_path}"
        )
        try:
            escalate(
                self.client,
                child_session_id=f"run:{subscription.subscription_id}",
                child_workspace_id=subscription.return_workspace_id,
                parent_session_id=subscription.return_session_id,
                message=message,
                store=self.store.escalations,
                dedupe_key=subscription.dedupe_key,
            )
        except (CdesktopError, OSError):
            return False
        return True


def _subscription_from_row(row: sqlite3.Row) -> RunSubscription:
    return RunSubscription(
        subscription_id=row["subscription_id"],
        run_id=row["run_id"],
        output_root=row["output_root"],
        return_session_id=row["return_session_id"],
        return_workspace_id=row["return_workspace_id"],
        dedupe_key=row["dedupe_key"],
        state=row["state"],
        pid=row["pid"],
        process_start=row["process_start"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        bound_at=row["bound_at"],
        terminal_state=row["terminal_state"],
        exit_code=row["exit_code"],
        finished_at=row["finished_at"],
        receipt_path=row["receipt_path"],
        receipt_digest=row["receipt_digest"],
        diagnostic=row["diagnostic"],
        lost_at=row["lost_at"],
        lease_released_at=row["lease_released_at"],
        notified_at=row["notified_at"],
    )
