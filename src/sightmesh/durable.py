"""The single in-service gap closer for durable cdesktop commands.

SightMesh does not persist execution state here.  cdesktop owns the command
rows; this module only translates observations of native processes into the
native command lifecycle and queues parent notifications through the same
follow-up path.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .cdesktop import (
    CdesktopClient,
    CdesktopError,
    CdesktopInterruptedError,
    CdesktopPendingError,
)
from .escalation import EscalationStore, escalate
from .runtime_lock import RUNTIME_LOCK
from .stalls import is_active_suite_work, threshold_from_environment
from .succession import COMMAND_TERMINAL_STATES, OwnershipStore, resolve_live_successor

LOGGER = logging.getLogger("sightmesh.durable")


def supports_durable_recovery(version: object) -> bool:
    """Return whether cdesktop exposes the process-scoped recovery API."""
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", str(version or ""))
    return bool(
        match
        and tuple(int(part) for part in match.groups())
        >= RUNTIME_LOCK.cdesktop.compatibility.durable_recovery_tuple
    )


@dataclass(frozen=True)
class DurableCommand:
    id: str
    session_id: str
    body: str
    state: str
    dedupe_key: str | None = None
    execution_process_id: str | None = None

    def delivery_state(self, process: dict[str, Any] | None) -> str:
        """Project cdesktop facts onto the delivery lifecycle without storing it."""
        if self.state == "pending":
            return "queued"
        if self.state == "claimed":
            if process is None:
                return "claimed"
            return "running" if process.get("status") == "running" else "observed"
        if self.state in {"done", "failed"}:
            return "terminal"
        if self.state == "cancelled":
            return "rejected"
        return "observed"


class NativeCommandQueue:
    """Thin adapter around cdesktop's already durable command machinery."""

    def __init__(self, client: CdesktopClient) -> None:
        self.client = client

    def commands(self, session_id: str) -> list[DurableCommand]:
        if not hasattr(self.client, "session_commands"):
            return []
        rows = self.client.session_commands(session_id)
        return [DurableCommand(**_command_fields(row, session_id)) for row in rows]

    def requeue(self, command: DurableCommand) -> None:
        if not command.execution_process_id:
            raise CdesktopError(f"Command {command.id} has no execution to requeue")
        self.client.requeue_execution_commands(
            command.session_id, command.execution_process_id
        )

    def interrupt(self, command: DurableCommand) -> None:
        # cdesktop has no per-command cancel endpoint yet. Prefer one when the
        # runtime (or a test double) offers it; otherwise stop the command's
        # execution, which is replay-safe, and rely on the reconciler never
        # requeueing or dispatching for a quarantined session.
        if hasattr(self.client, "interrupt_command"):
            self.client.interrupt_command(command.id)
            return
        if command.execution_process_id:
            self.client.stop_execution(
                command.execution_process_id,
                dedupe_key=f"quarantine:{command.id}",
            )

    def notify_parent(
        self, parent_session_id: str, child_session_id: str, message: str, key: str
    ) -> None:
        self.client.send(
            parent_session_id,
            message,
            None,
            dedupe_key=key,
            intent="continue",
        )


def _command_fields(row: dict[str, Any], session_id: str) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row.get("session_id") or session_id),
        "body": str(row.get("body") or row.get("prompt") or ""),
        "state": str(row.get("state") or row.get("status") or "queued"),
        "dedupe_key": row.get("dedupe_key"),
        "execution_process_id": row.get("execution_process_id"),
    }


class SuiteLiveness:
    """Read-only suite-aware observation used by the durable reconciler."""

    def __init__(
        self,
        *,
        threshold: timedelta | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.threshold = (
            threshold if threshold is not None else threshold_from_environment()
        )
        self.now = now or (lambda: datetime.now(UTC))
        self._last: dict[str, tuple[str, datetime]] = {}

    def stale(self, process_id: str, snapshot: dict[str, Any]) -> bool:
        if is_active_suite_work(snapshot):
            self._last.pop(process_id, None)
            return False
        signature = json.dumps(snapshot.get("entries", []), sort_keys=True)
        now = self.now()
        previous = self._last.get(process_id)
        if previous is None or previous[0] != signature:
            self._last[process_id] = (signature, now)
            return False
        return now - previous[1] >= self.threshold


def _context_pressure(snapshot: dict[str, Any]) -> float | None:
    for wrapped in snapshot.get("entries", []):
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        entry = content.get("entry_type") if isinstance(content, dict) else None
        if isinstance(entry, dict) and entry.get("type") == "token_usage_info":
            used, window = entry.get("total_tokens"), entry.get("model_context_window")
            if isinstance(used, (int, float)) and isinstance(window, (int, float)) and window:
                return float(used) / float(window)
    return None


def _idle_seconds(processes: Iterable[dict[str, Any]], now: float) -> float | None:
    timestamps: list[float] = []
    for process in processes:
        for name in ("updated_at", "started_at", "created_at"):
            value = process.get(name)
            if isinstance(value, (int, float)):
                timestamps.append(float(value))
                break
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                timestamps.append(
                    parsed.replace(tzinfo=UTC).timestamp()
                    if parsed.tzinfo is None
                    else parsed.timestamp()
                )
                break
    return now - max(timestamps) if timestamps else None


class DurableExecutionReconciler:
    """Reconcile durable intent and live processes; safe to run repeatedly."""

    def __init__(
        self,
        client: CdesktopClient,
        queue: NativeCommandQueue | None = None,
        *,
        probe: Callable[[], bool] | None = None,
        liveness: SuiteLiveness | None = None,
        ownership: OwnershipStore | None = None,
        signal_store: EscalationStore | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.client = client
        self.queue = queue or NativeCommandQueue(client)
        self.probe = probe or getattr(client, "probe_connectivity", lambda: True)
        self.ownership = ownership or OwnershipStore()
        self.signal_store = signal_store or EscalationStore()
        self.clock = clock or time.time
        self._stopped: set[str] = set()
        self._requeued: set[str] = set()
        self._cancelled: set[str] = set()
        self._notified: set[str] = set()
        self.liveness = liveness or SuiteLiveness()
        self._offline_until = 0.0
        self._backoff = 1.0
        self._durable_supported: bool | None = None

    def _supports_durable_recovery(self) -> bool:
        if self._durable_supported is not None:
            return self._durable_supported
        if not hasattr(self.client, "info"):
            # Protocol test doubles and older embedders predate version discovery.
            self._durable_supported = True
            return True
        info = self.client.info()
        version = info.get("version") if isinstance(info, dict) else None
        self._durable_supported = supports_durable_recovery(version)
        if not self._durable_supported:
            minimum = RUNTIME_LOCK.cdesktop.compatibility.durable_recovery
            LOGGER.warning(
                "Durable recovery is disabled: cdesktop %s or newer is required "
                "(found %s). Normal bridging remains available.",
                minimum,
                version or "unknown version",
            )
        return self._durable_supported

    def reconcile_sessions(self, sessions: Iterable[dict[str, Any]]) -> None:
        """Reconcile all sessions in one writer, tolerating partial reads."""
        if not self._supports_durable_recovery():
            return
        for session in sessions:
            try:
                self.reconcile_session(session)
            except CdesktopError as exc:
                LOGGER.warning(
                    "Cannot reconcile durable session %s: %s", session.get("id"), exc
                )

    def reconcile_session(self, session: dict[str, Any]) -> None:
        session_id = str(session["id"])
        commands = self.queue.commands(session_id)
        if self.ownership.is_quarantined(session_id):
            # An explicit retired/superseded ownership transition is the only
            # quarantine trigger.  Queued delivery must never auto-resume the
            # session into a shared worktree: cancel, never requeue, never
            # dispatch.  Ordinary completed or failed turns take the normal
            # path below and stay resumable.
            self._cancel_quarantined(commands)
            return
        processes = self.client.execution_processes(session_id)
        by_process = {str(item.get("id")): item for item in processes}
        command_by_process = {
            str(command.execution_process_id): command
            for command in commands
            if command.state == "claimed" and command.execution_process_id
        }
        self._wake_parent_for_terminal_commands(session, commands)
        self._reconcile_signal_policy(session, commands, processes)
        for command in commands:
            if command.state != "claimed":
                continue
            process = by_process.get(str(command.execution_process_id))
            if process is None:
                # Absence is not terminal evidence. A partial read must never
                # release a claim while its execution may still be running.
                continue
            if process and process.get("status") == "running":
                snapshot = self.client.normalized_snapshot(str(process["id"]))
                if not snapshot.get("stream_alive", True):
                    self.recover_stalled_process(session, process, command)
                elif is_active_suite_work(snapshot):
                    continue
                elif self.liveness.stale(str(process["id"]), snapshot):
                    self.recover_stalled_process(session, process, command)
                continue
            self.reconcile_child_terminal(
                session, status=str(process.get("status") or "terminal")
            )
            self._interrupt_and_requeue(command)

        # A running child can be observed before the command list is visible;
        # native cdesktop normally supplies the row, but this keeps observation
        # and recovery in this same writer during that narrow read race.
        for process_id, process in by_process.items():
            command = command_by_process.get(process_id)
            if command or process.get("status") != "running":
                continue
            snapshot = self.client.normalized_snapshot(process_id)
            if is_active_suite_work(snapshot):
                continue
            if self.liveness.stale(process_id, snapshot):
                synthetic = DurableCommand(
                    process_id,
                    session_id,
                    "",
                    "claimed",
                    execution_process_id=process_id,
                )
                self.recover_stalled_process(session, process, synthetic)
                if process.get("status") != "running":
                    self.reconcile_child_terminal(session, status="interrupted")

        # The native dispatcher remains the only claimant.  The gate prevents
        # a reconnect storm when cdesktop is reachable but the model is not.
        if hasattr(self.client, "dispatch_queued") and self._online():
            self.client.dispatch_queued(session_id)

    def _reconcile_signal_policy(
        self,
        session: dict[str, Any],
        commands: Iterable[DurableCommand],
        processes: Iterable[dict[str, Any]],
    ) -> None:
        """Turn opt-in observable conditions into one durable parent follow-up."""
        policy = self.signal_store.signal_policy(str(session["id"]))
        if not policy.conditions:
            return
        process_rows = list(processes)
        command_rows = list(commands)
        terminal = any(
            process.get("status") not in {None, "running"} for process in process_rows
        )
        pressure = max(
            (
                value
                for process in process_rows
                for value in [
                    _context_pressure(self.client.normalized_snapshot(str(process["id"])))
                ]
                if value is not None
            ),
            default=None,
        )
        active = any(
            command.delivery_state(None) not in {"terminal", "rejected"}
            for command in command_rows
        )
        idle_seconds = _idle_seconds(process_rows, self.clock()) if active else None
        for condition in policy.conditions:
            triggered = condition == "terminal" and terminal
            if condition.startswith("context-pressure:") and pressure is not None:
                triggered = pressure >= float(condition.split(":", 1)[1])
            if condition.startswith("idle:") and idle_seconds is not None:
                triggered = idle_seconds > int(condition.split(":", 1)[1])
            if not triggered:
                continue
            session_id = str(session["id"])
            key = f"signal-policy:{session_id}:{condition}"
            if self.signal_store.has_dedupe_key(key):
                continue
            escalate(
                self.client,
                child_session_id=session_id,
                child_workspace_id=str(session.get("workspace_id") or "") or None,
                parent_session_id=(
                    str(session["parent_session_id"])
                    if session.get("parent_session_id")
                    else None
                ),
                message=f"STATUS: signal policy triggered: {condition}",
                store=self.signal_store,
                dedupe_key=key,
            )

    def _online(self) -> bool:
        now = time.monotonic()
        if now < self._offline_until:
            return False
        if self.probe():
            self._backoff = 1.0
            self._offline_until = 0.0
            return True
        self._offline_until = now + self._backoff
        self._backoff = min(self._backoff * 2.0, 30.0)
        return False

    def _cancel_quarantined(self, commands: Iterable[DurableCommand]) -> None:
        for command in commands:
            if command.state in COMMAND_TERMINAL_STATES:
                continue
            if command.id in self._cancelled:
                continue
            self.queue.interrupt(command)
            self._cancelled.add(command.id)

    def reconcile_child_terminal(
        self,
        child_session: dict[str, Any],
        *,
        status: str,
    ) -> None:
        parent = child_session.get("parent_session_id")
        if not parent:
            return
        # The key stays bound to the child and status, not the destination, so
        # a redirected notification is still one logical command.
        key = f"child-terminal:{child_session['id']}:{status}"
        self._notify_live_parent(
            parent_session_id=str(parent),
            child_session_id=str(child_session["id"]),
            message=f"CHILD_TERMINAL: {child_session['id']} {status}",
            key=key,
        )

    def _wake_parent_for_terminal_commands(
        self, child_session: dict[str, Any], commands: Iterable[DurableCommand]
    ) -> None:
        """Wake once per native terminal transition; cdesktop owns the dedupe fence."""
        parent = child_session.get("parent_session_id")
        if not parent:
            return
        child_id = str(child_session["id"])
        for command in commands:
            state = command.delivery_state(None)
            if state not in {"terminal", "rejected"}:
                continue
            key = f"child-command:{command.id}:{command.state}"
            self._notify_live_parent(
                parent_session_id=str(parent),
                child_session_id=child_id,
                message=f"CHILD_DELIVERY: {child_id} {command.id} {command.state}",
                key=key,
            )

    def _notify_live_parent(
        self,
        *,
        parent_session_id: str,
        child_session_id: str,
        message: str,
        key: str,
    ) -> None:
        """Deliver once to a live successor, or leave the wake parked for retry."""
        if key in self._notified:
            return
        destination = resolve_live_successor(self.ownership, parent_session_id)
        if destination is None:
            LOGGER.warning(
                "Parking child notification for quarantined parent %s with no live "
                "successor",
                parent_session_id,
            )
            return
        self.queue.notify_parent(destination, child_session_id, message, key)
        self._notified.add(key)

    def _interrupt_and_requeue(self, command: DurableCommand) -> None:
        # Lifecycle writes are idempotent in cdesktop; duplicate ticks cannot
        # manufacture a second command because requeue retains dedupe_key.
        if command.id in self._requeued:
            return
        self.queue.requeue(command)
        self._requeued.add(command.id)

    def recover_stalled_process(
        self,
        session: dict[str, Any],
        process: dict[str, Any],
        command: DurableCommand | None,
    ) -> None:
        if command is None or str(process["id"]) in self._stopped:
            return
        key = f"durable:{command.id}:stop"
        try:
            self.client.stop_execution(str(process["id"]), dedupe_key=key)
        except CdesktopInterruptedError:
            # HTTP 424 is not terminal evidence: the keyed stop may or may not
            # have run. Do not release the claim or wake its parent until the
            # native process row proves a terminal status. On restart the same
            # key replays this cdesktop-owned outcome without a second stop.
            self._stopped.add(str(process["id"]))
            return
        except CdesktopPendingError:
            return
        else:
            self._stopped.add(str(process["id"]))
