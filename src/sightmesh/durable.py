"""The single in-service gap closer for durable cdesktop commands.

SightMesh does not persist execution state here.  cdesktop owns the command
rows; this module only translates observations of native processes into the
native command lifecycle and queues parent notifications through the same
follow-up path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .cdesktop import (
    CdesktopClient,
    CdesktopError,
    CdesktopInterruptedError,
    CdesktopPendingError,
    CdesktopRejectedError,
)
from .stalls import is_active_suite_work

LOGGER = logging.getLogger("sightmesh.durable")


@dataclass(frozen=True)
class DurableCommand:
    id: str
    session_id: str
    body: str
    state: str
    dedupe_key: str | None = None
    execution_process_id: str | None = None


class NativeCommandQueue:
    """Thin adapter around cdesktop's already durable command machinery."""

    def __init__(self, client: CdesktopClient) -> None:
        self.client = client

    def commands(self, session_id: str) -> list[DurableCommand]:
        rows = self.client.session_commands(session_id)
        return [DurableCommand(**_command_fields(row, session_id)) for row in rows]

    def interrupt(self, command: DurableCommand) -> None:
        self.client.interrupt_command(command.id)

    def requeue(self, command: DurableCommand) -> None:
        self.client.requeue_command(command.id, dedupe_key=command.dedupe_key)

    def done(self, command: DurableCommand) -> None:
        self.client.complete_command(command.id)

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


class DurableExecutionReconciler:
    """Reconcile durable intent and live processes; safe to run repeatedly."""

    def __init__(
        self,
        client: CdesktopClient,
        queue: NativeCommandQueue | None = None,
        *,
        probe: Callable[[], bool] | None = None,
    ) -> None:
        self.client = client
        self.queue = queue or NativeCommandQueue(client)
        self.probe = probe or getattr(client, "probe_connectivity", lambda: True)
        self._stopped: set[str] = set()
        self._requeued: set[str] = set()
        self._legacy_idle: dict[str, int] = {}
        self._offline_until = 0.0
        self._backoff = 1.0

    def reconcile_sessions(self, sessions: Iterable[dict[str, Any]]) -> None:
        """Reconcile all sessions in one writer, tolerating partial reads."""
        for session in sessions:
            try:
                self.reconcile_session(session)
            except CdesktopError as exc:
                LOGGER.warning(
                    "Cannot reconcile durable session %s: %s", session.get("id"), exc
                )

    def reconcile_session(self, session: dict[str, Any]) -> None:
        session_id = str(session["id"])
        if isinstance(self.queue, NativeCommandQueue) and not hasattr(
            self.client, "session_commands"
        ):
            self._reconcile_legacy_liveness(session)
            return
        commands = self.queue.commands(session_id)
        processes = self.client.execution_processes(session_id)
        by_process = {str(item.get("id")): item for item in processes}
        for command in commands:
            if command.state != "claimed":
                continue
            process = by_process.get(str(command.execution_process_id))
            if process and process.get("status") == "running":
                snapshot = self.client.normalized_snapshot(str(process["id"]))
                if is_active_suite_work(snapshot):
                    continue
                if not snapshot.get("stream_alive", True):
                    self._interrupt_and_requeue(command)
                continue
            self._interrupt_and_requeue(command)

        # The native dispatcher remains the only claimant.  The gate prevents
        # a reconnect storm when cdesktop is reachable but the model is not.
        if self._online():
            self.client.dispatch_queued(session_id)

    def _reconcile_legacy_liveness(self, session: dict[str, Any]) -> None:
        """Compatibility read path for pre-command-list cdesktop builds."""
        parent = session.get("parent_session_id")
        if not parent:
            return
        for process in self.client.execution_processes(str(session["id"])):
            if process.get("status") != "running":
                continue
            process_id = str(process["id"])
            snapshot = self.client.normalized_snapshot(process_id)
            if is_active_suite_work(snapshot):
                self._legacy_idle.pop(process_id, None)
                continue
            self._legacy_idle[process_id] = self._legacy_idle.get(process_id, 0) + 1
            if self._legacy_idle[process_id] < 2 or process_id in self._stopped:
                continue
            command = DurableCommand(process_id, str(session["id"]), "", "claimed")
            self.recover_stalled_process(session, process, command)
            if process.get("status") != "running":
                self.queue.notify_parent(
                    str(parent),
                    str(session["id"]),
                    f"CHILD_TERMINAL: {session['id']} interrupted",
                    f"child-terminal:{session['id']}:interrupted",
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

    def reconcile_child_terminal(
        self,
        child_session: dict[str, Any],
        *,
        status: str,
    ) -> None:
        parent = child_session.get("parent_session_id")
        if not parent:
            return
        key = f"child-terminal:{child_session['id']}:{status}"
        self.queue.notify_parent(
            str(parent),
            str(child_session["id"]),
            f"CHILD_TERMINAL: {child_session['id']} {status}",
            key,
        )

    def _interrupt_and_requeue(self, command: DurableCommand) -> None:
        # Lifecycle writes are idempotent in cdesktop; duplicate ticks cannot
        # manufacture a second command because requeue retains dedupe_key.
        if command.id in self._requeued:
            return
        self.queue.interrupt(command)
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
            self._stopped.add(str(process["id"]))
            self._interrupt_and_requeue(command)
        except CdesktopPendingError:
            return
        except CdesktopRejectedError as exc:
            if exc.status == 409:
                return
            raise
        else:
            self._stopped.add(str(process["id"]))
