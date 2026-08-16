from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cdesktop import (
    CdesktopClient,
    CdesktopError,
    CdesktopInterruptedError,
    CdesktopPendingError,
    CdesktopRejectedError,
)
from .stall_settings import threshold_minutes

LOGGER = logging.getLogger("sightmesh.stalls")
MAX_CONFIRMATION_POLLS = 3


def threshold_from_environment() -> timedelta:
    """Return the bounded, operator-configurable no-event threshold."""
    return timedelta(minutes=threshold_minutes())


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _latest_event_time(value: object) -> datetime | None:
    latest: datetime | None = None
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"timestamp", "created_at", "updated_at", "time"}:
                candidate = _parse_time(item)
                if candidate and (latest is None or candidate > latest):
                    latest = candidate
            nested = _latest_event_time(item)
            if nested and (latest is None or nested > latest):
                latest = nested
    elif isinstance(value, list):
        for item in value:
            candidate = _latest_event_time(item)
            if candidate and (latest is None or candidate > latest):
                latest = candidate
    return latest


def _has_active_child(value: object) -> bool:
    """Recognize live command/tool records without maintaining a command allowlist."""
    if isinstance(value, dict):
        state = str(value.get("status") or value.get("state") or "").lower()
        is_process_like = any(
            key in value
            for key in (
                "command",
                "tool",
                "tool_name",
                "process",
                "pid",
                "child_process",
            )
        )
        if is_process_like and state in {
            "running",
            "active",
            "in_progress",
            "executing",
        }:
            return True
        return any(_has_active_child(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_active_child(item) for item in value)
    return False


def is_active_suite_work(snapshot: dict[str, Any]) -> bool:
    """Return true when a live child/tool keeps a suite turn active."""
    return _has_active_child(snapshot)


def _event_signature(snapshot: dict[str, Any]) -> str:
    return json.dumps(
        snapshot.get("entries", []), sort_keys=True, separators=(",", ":")
    )


@dataclass
class _ObservedProcess:
    signature: str
    last_event_at: datetime


class RecoveryIntentStore:
    """Persist the outcome class of a non-idempotent stop request per process."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.intents = self._read()

    def begin(self, process_id: str) -> tuple[str, bool]:
        state = self.intents.get(process_id)
        if state:
            return self.state(process_id), False
        self.intents[process_id] = "intent:1:0"
        self._write()
        return "intent", True

    def set(self, process_id: str, state: str) -> None:
        self.intents[process_id] = f"{state}:{self.attempt(process_id)}:0"
        self._write()

    def state(self, process_id: str) -> str:
        return self.intents[process_id].split(":", 1)[0]

    def attempt(self, process_id: str) -> int:
        parts = self.intents.get(process_id, "").split(":")
        try:
            return max(1, int(parts[1]))
        except (IndexError, ValueError):
            return 1

    def fresh_attempt(self, process_id: str) -> int:
        attempt = self.attempt(process_id) + 1
        self.intents[process_id] = f"intent:{attempt}:0"
        self._write()
        return attempt

    def confirmation_poll(self, process_id: str) -> int:
        state = self.state(process_id)
        parts = self.intents[process_id].split(":")
        try:
            count = int(parts[2]) + 1
        except (IndexError, ValueError):
            count = 1
        self.intents[process_id] = f"{state}:{self.attempt(process_id)}:{count}"
        self._write()
        return count

    def discard(self, process_id: str) -> None:
        if process_id in self.intents:
            self.intents.pop(process_id)
            self._write()

    def process_ids(self) -> set[str]:
        return set(self.intents)

    def _read(self) -> dict[str, str]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return (
            {
                key: value
                for key, value in data.items()
                if isinstance(key, str)
                and isinstance(value, str)
                and value.split(":", 1)[0]
                in {
                    "intent",
                    "stopping",
                    "accepted",
                    "uncertain",
                    "interrupted",
                    "handoff",
                    "notified",
                    "escalated",
                }
            }
            if isinstance(data, dict)
            else {}
        )

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, temporary = tempfile.mkstemp(
            prefix=".stall-recovery.", dir=self.path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.intents, stream, sort_keys=True)
                stream.write("\n")
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


class StallDetector:
    """Detect one truly idle execution and hand it to native recovery exactly once."""

    def __init__(
        self,
        *,
        threshold: timedelta | None = None,
        now: Callable[[], datetime] | None = None,
        recovery_store: RecoveryIntentStore | None = None,
    ) -> None:
        self.threshold = threshold or threshold_from_environment()
        self.now = now or (lambda: datetime.now(UTC))
        self.recovery_store = recovery_store or RecoveryIntentStore()
        self.observed: dict[str, _ObservedProcess] = {}
        self.notified: set[str] = set()

    def reconcile(self, client: CdesktopClient, session: dict[str, Any]) -> None:
        parent_session_id = session.get("parent_session_id")
        if not isinstance(parent_session_id, str) or not parent_session_id:
            return
        session_id = str(session["id"])
        processes = client.execution_processes(session_id)
        running_ids = {
            str(process["id"])
            for process in processes
            if process.get("status") == "running"
            and process.get("run_reason") != "devserver"
        }
        active_ids = {str(process.get("id")) for process in processes}
        for process_id in set(self.observed) - running_ids:
            self.observed.pop(process_id, None)
        by_id = {str(process.get("id")): process for process in processes}
        for process_id in self.recovery_store.process_ids() & active_ids:
            self._reconcile_recovery(
                client, session, parent_session_id, process_id, by_id[process_id]
            )
        for process_id in self.recovery_store.process_ids() - active_ids:
            state = self.recovery_store.state(process_id)
            if state == "interrupted":
                self._notify_parent(
                    client, session, parent_session_id, process_id, interrupted=True
                )
            elif state == "notified":
                self.recovery_store.discard(process_id)

        for process in processes:
            process_id = str(process.get("id"))
            if (
                process_id not in running_ids
                or process_id in self.recovery_store.process_ids()
            ):
                continue
            self._reconcile_process(client, session, parent_session_id, process)

    def _reconcile_process(
        self,
        client: CdesktopClient,
        session: dict[str, Any],
        parent_session_id: str,
        process: dict[str, Any],
    ) -> None:
        process_id = str(process["id"])
        try:
            snapshot = client.normalized_snapshot(process_id)
        except CdesktopError as exc:
            LOGGER.warning(
                "Cannot inspect execution %s for stalls: %s", process_id, exc
            )
            return
        now = self.now()
        signature = _event_signature(snapshot)
        recorded_event = _latest_event_time(snapshot)
        observed = self.observed.get(process_id)
        if observed is None:
            # The first read is a warm baseline, never stale evidence. cdesktop
            # leaves a running stream partial until it emits Finished, so using
            # process start time here would kill a cold active stream at once.
            self.observed[process_id] = _ObservedProcess(
                signature=signature,
                last_event_at=recorded_event or now,
            )
            return
        if signature != observed.signature or _has_active_child(snapshot):
            observed.signature = signature
            observed.last_event_at = recorded_event or now
            return
        if recorded_event and recorded_event > observed.last_event_at:
            observed.last_event_at = recorded_event
            return
        if now - observed.last_event_at < self.threshold:
            return

        self.recovery_store.begin(process_id)
        self._reconcile_recovery(
            client, session, parent_session_id, process_id, process
        )

    def _reconcile_recovery(
        self,
        client: CdesktopClient,
        session: dict[str, Any],
        parent_session_id: str,
        process_id: str,
        process: dict[str, Any],
    ) -> None:
        state = self.recovery_store.state(process_id)
        if state == "interrupted":
            self._notify_parent(
                client, session, parent_session_id, process_id, interrupted=True
            )
            return
        try:
            current = client.execution_process(process_id)
        except CdesktopError as exc:
            LOGGER.warning("Cannot reconcile stalled execution %s: %s", process_id, exc)
            return
        state = self.recovery_store.state(process_id)
        if current.get("status") != "running":
            if state != "notified":
                self.recovery_store.set(process_id, "handoff")
                self._notify_parent(client, session, parent_session_id, process_id)
            return
        if state == "notified":
            return
        if state in {"accepted", "uncertain"}:
            # A successful POST, or one whose transport outcome is unknown, is
            # non-idempotent.  Keep reading the authoritative process state;
            # never turn normal shutdown latency into a second stop request.
            polls = self.recovery_store.confirmation_poll(process_id)
            if polls >= MAX_CONFIRMATION_POLLS:
                self.recovery_store.set(process_id, "escalated")
                LOGGER.error(
                    "Stalled execution %s did not settle after %s confirmations; "
                    "leaving standard failed-child recovery for operator escalation",
                    process_id,
                    polls,
                )
            return
        if state == "escalated":
            return
        # A crash can leave ``stopping`` before or after cdesktop accepts the
        # POST. Reissuing the same process-scoped key is the cross-service
        # invariant: cdesktop performs the destructive stop once and returns
        # that original outcome on a retry.
        self.recovery_store.set(process_id, "stopping")
        try:
            client.stop_execution(
                process_id,
                dedupe_key=(
                    f"stall:{process_id}:stop:{self.recovery_store.attempt(process_id)}"
                ),
            )
        except CdesktopInterruptedError as exc:
            # HTTP 424 is a durable unknown-causality result.  It may follow
            # either side of the side effect, so never issue another stop.
            self.recovery_store.set(process_id, "interrupted")
            LOGGER.warning("Stop interrupted for execution %s: %s", process_id, exc)
            self._notify_parent(
                client, session, parent_session_id, process_id, interrupted=True
            )
            return
        except CdesktopPendingError as exc:
            # HTTP 425 means this exact keyed request is still in flight.
            # Replay that key on the next reconciliation, never a new one.
            self.recovery_store.set(process_id, "stopping")
            LOGGER.warning("Stop pending for execution %s: %s", process_id, exc)
            return
        except CdesktopRejectedError as exc:
            # A durable rejection proves no stop side effect for this key.
            # Rotate identity before the next recovery attempt.
            if exc.status not in {None, 409}:
                self.recovery_store.set(process_id, "uncertain")
                LOGGER.warning(
                    "Stop outcome for execution %s is not definitive: %s",
                    process_id,
                    exc,
                )
                return
            self.recovery_store.fresh_attempt(process_id)
            LOGGER.warning(
                "Stop rejected for execution %s; it remains retryable: %s",
                process_id,
                exc,
            )
            return
        except CdesktopError as exc:
            self.recovery_store.set(process_id, "uncertain")
            LOGGER.warning(
                "Stop outcome for execution %s is ambiguous: %s", process_id, exc
            )
            # A transport failure may have followed a successful side effect.
            # One authoritative read can establish recovery; otherwise retain
            # uncertainty and wait rather than retrying the destructive call.
            try:
                confirmed = client.execution_process(process_id)
            except CdesktopError as confirm_exc:
                LOGGER.warning(
                    "Cannot confirm stalled execution %s: %s", process_id, confirm_exc
                )
                return
            if confirmed.get("status") != "running":
                self.recovery_store.set(process_id, "handoff")
                self._notify_parent(client, session, parent_session_id, process_id)
            else:
                self.recovery_store.confirmation_poll(process_id)
            return
        self.recovery_store.set(process_id, "accepted")
        # Confirm promptly when possible, but an unavailable read leaves the
        # accepted state durable for the next reconciliation rather than
        # making another destructive request.
        try:
            confirmed = client.execution_process(process_id)
        except CdesktopError as exc:
            LOGGER.warning("Cannot confirm stalled execution %s: %s", process_id, exc)
            return
        if confirmed.get("status") != "running":
            self.recovery_store.set(process_id, "handoff")
            self._notify_parent(client, session, parent_session_id, process_id)

    def _notify_parent(
        self,
        client: CdesktopClient,
        session: dict[str, Any],
        parent_session_id: str,
        process_id: str,
        *,
        interrupted: bool = False,
    ) -> None:
        if process_id in self.notified:
            return
        dedupe_key = f"stall:{process_id}:parent"
        try:
            client.send(
                parent_session_id,
                (
                    "STALL: child execution entered interrupted recovery; reconcile its "
                    "durable parent follow-up."
                    if interrupted
                    else "STALL: child execution produced no session events past the configured "
                    "threshold and was handed to native killed-child recovery."
                ),
                str(session["id"]),
                dedupe_key=dedupe_key,
            )
        except CdesktopError as exc:
            LOGGER.warning(
                "Cannot notify parent for stalled execution %s: %s", process_id, exc
            )
            return
        self.notified.add(process_id)
        self.recovery_store.set(process_id, "notified")
        LOGGER.warning(
            "Marked session %s stalled through execution %s and notified parent %s",
            session["id"],
            process_id,
            parent_session_id,
        )
