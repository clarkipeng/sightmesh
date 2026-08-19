from __future__ import annotations

from sightmesh.cdesktop import (
    CdesktopInterruptedError,
    CdesktopPendingError,
    CdesktopRejectedError,
)
from sightmesh.durable import (
    DurableCommand,
    DurableExecutionReconciler,
    supports_durable_recovery,
)
from sightmesh.runtime_lock import RUNTIME_LOCK


class Queue:
    def __init__(self, rows):
        self.rows = rows
        self.interrupted = []
        self.requeued = []
        self.notifications = []
        self.recoveries = []

    def commands(self, _session):
        return list(self.rows)

    def interrupt(self, command):
        self.interrupted.append(command.id)

    def requeue(self, command):
        self.requeued.append((command.id, command.dedupe_key))

    def recovery(self, command, *, attempt, state):
        self.recoveries.append((command.id, attempt, state))

    def notify_parent(self, parent, child, message, key):
        self.notifications.append((parent, child, message, key))


class Client:
    def __init__(self, process, snapshot, *, online=True):
        self.process = process
        self.snapshot = snapshot
        self.online = online
        self.dispatches = []
        self.stops = []

    def execution_processes(self, _session):
        return [self.process]

    def normalized_snapshot(self, _process):
        return self.snapshot

    def probe_connectivity(self):
        return self.online

    def dispatch_queued(self, session):
        self.dispatches.append(session)

    def stop_execution(self, process, *, dedupe_key=None):
        self.stops.append((process, dedupe_key))


def command(state="claimed"):
    from sightmesh.durable import DurableCommand

    return DurableCommand(
        "command-1", "session-1", "work", state, "same-key", "process-1"
    )


def test_durable_recovery_version_boundary() -> None:
    minimum = RUNTIME_LOCK.cdesktop.compatibility.durable_recovery
    major, minor, patch = RUNTIME_LOCK.cdesktop.compatibility.durable_recovery_tuple
    previous = f"{major}.{minor}.{patch - 1}"
    assert not supports_durable_recovery(f"cdesktop/{previous}")
    assert supports_durable_recovery(f"cdesktop/{minimum}")


def test_025_gate_fails_closed_once_without_recovery_calls(caplog) -> None:
    class LegacyClient:
        def __init__(self) -> None:
            self.info_calls = 0

        def info(self):
            self.info_calls += 1
            return {
                "version": "cdesktop/"
                + RUNTIME_LOCK.cdesktop.compatibility.minimum
            }

        def execution_processes(self, _session):
            raise AssertionError("unsupported recovery API was called")

    client = LegacyClient()
    reconciler = DurableExecutionReconciler(client, Queue([command()]))

    reconciler.reconcile_sessions([{"id": "session-1"}])
    reconciler.reconcile_sessions([{"id": "session-1"}])

    assert client.info_calls == 1
    assert caplog.text.count("Durable recovery is disabled") == 1


def test_restart_after_claim_interrupts_and_requeues_same_command():
    client = Client({"id": "process-1", "status": "killed"}, {})
    queue = Queue([command()])
    reconciler = DurableExecutionReconciler(client, queue)

    reconciler.reconcile_session({"id": "session-1"})
    reconciler.reconcile_session({"id": "session-1"})

    assert queue.interrupted == []
    assert queue.requeued == [("command-1", "same-key")]


def test_stream_death_requeues_and_offline_gate_backoffs():
    client = Client(
        {"id": "process-1", "status": "running"},
        {"stream_alive": False},
        online=False,
    )
    queue = Queue([command()])
    reconciler = DurableExecutionReconciler(client, queue)

    reconciler.reconcile_session({"id": "session-1"})
    assert client.stops == [("process-1", "durable:process-1:stop:1")]
    assert queue.requeued == []
    assert client.dispatches == []


def test_delivery_lifecycle_is_derived_only_from_native_records():
    pending = command("pending")
    claimed = command("claimed")
    done = command("done")
    cancelled = command("cancelled")

    assert pending.delivery_state(None) == "queued"
    assert claimed.delivery_state(None) == "claimed"
    assert claimed.delivery_state({"status": "running"}) == "running"
    assert claimed.delivery_state({"status": "killed"}) == "observed"
    assert done.delivery_state(None) == "terminal"
    assert cancelled.delivery_state(None) == "rejected"


def test_restart_terminal_wake_uses_native_dedupe_and_does_not_loop():
    class NativeDedupeQueue(Queue):
        def __init__(self, rows):
            super().__init__(rows)
            self.keys = set()

        def notify_parent(self, parent, child, message, key):
            if key not in self.keys:
                super().notify_parent(parent, child, message, key)
                self.keys.add(key)

    client = Client({"id": "process-1", "status": "completed"}, {})
    queue = NativeDedupeQueue([command("done")])
    session = {"id": "child", "parent_session_id": "parent"}

    DurableExecutionReconciler(client, queue).reconcile_session(session)
    DurableExecutionReconciler(client, queue).reconcile_session(session)
    DurableExecutionReconciler(client, queue).reconcile_session({"id": "parent"})

    assert queue.notifications == [
        (
            "parent",
            "child",
            "CHILD_DELIVERY: child command-1 done",
            "child-command:command-1:done",
        )
    ]


def test_suite_child_activity_prevents_recovery():
    client = Client(
        {"id": "process-1", "status": "running"},
        {"entries": [{"tool_name": "bun test", "status": "running"}]},
    )
    queue = Queue([command()])
    DurableExecutionReconciler(client, queue).reconcile_session({"id": "session-1"})

    assert queue.interrupted == []


def test_child_terminal_notification_is_a_durable_parent_command():
    client = Client({"id": "process-1", "status": "killed"}, {})
    queue = Queue([])
    reconciler = DurableExecutionReconciler(client, queue)

    reconciler.reconcile_child_terminal(
        {"id": "child", "parent_session_id": "parent"}, status="interrupted"
    )

    assert queue.notifications[0][0:2] == ("parent", "child")
    assert queue.notifications[0][3] == "child-terminal:child:interrupted"


def test_native_stale_child_stops_and_active_suite_suppresses():
    from datetime import timedelta

    from sightmesh.durable import SuiteLiveness

    client = Client({"id": "process-1", "status": "running"}, {"entries": []})
    queue = Queue([command()])
    reconciler = DurableExecutionReconciler(
        client, queue, liveness=SuiteLiveness(threshold=timedelta(0))
    )
    reconciler.reconcile_session({"id": "session-1", "parent_session_id": "parent"})
    reconciler.reconcile_session({"id": "session-1", "parent_session_id": "parent"})
    assert client.stops == [("process-1", "durable:process-1:stop:1")]

    active = Client(
        {"id": "process-1", "status": "running"},
        {"entries": [{"tool_name": "bun test", "status": "running"}]},
    )
    active_queue = Queue([command()])
    active_reconciler = DurableExecutionReconciler(
        active, active_queue, liveness=SuiteLiveness(threshold=timedelta(0))
    )
    active_reconciler.reconcile_session(
        {"id": "session-1", "parent_session_id": "parent"}
    )
    active_reconciler.reconcile_session(
        {"id": "session-1", "parent_session_id": "parent"}
    )
    assert active.stops == []


def test_424_waits_for_native_terminal_observation_before_requeue_and_wake():
    class Interrupted(Client):
        def stop_execution(self, process, *, dedupe_key=None):
            super().stop_execution(process, dedupe_key=dedupe_key)
            raise CdesktopInterruptedError("unknown", status=424)

    client = Interrupted({"id": "process-1", "status": "running"}, {})
    queue = Queue([command()])
    reconciler = DurableExecutionReconciler(client, queue)
    reconciler.recover_stalled_process(
        {"id": "child", "parent_session_id": "parent"}, client.process, command()
    )
    reconciler.recover_stalled_process(
        {"id": "child", "parent_session_id": "parent"}, client.process, command()
    )

    assert len(client.stops) == 1
    assert queue.requeued == []
    assert queue.notifications == []

    client.process["status"] = "killed"
    child = {"id": "child", "parent_session_id": "parent"}
    reconciler.reconcile_session(child)
    reconciler.reconcile_session(child)

    assert queue.requeued == [("command-1", "same-key")]
    assert queue.notifications == [
        (
            "parent",
            "child",
            "CHILD_TERMINAL: child killed",
            "child-terminal:child:killed",
        )
    ]


def test_425_retries_same_key_and_409_rotates_attempt():
    class Pending(Client):
        def __init__(self, *args):
            super().__init__(*args)
            self.first = True

        def stop_execution(self, process, *, dedupe_key=None):
            super().stop_execution(process, dedupe_key=dedupe_key)
            if self.first:
                self.first = False
                raise CdesktopPendingError("pending", status=425)

    client = Pending({"id": "process-1", "status": "running"}, {})
    queue = Queue([])
    reconciler = DurableExecutionReconciler(client, queue)
    reconciler.recover_stalled_process({}, client.process, command())
    reconciler.recover_stalled_process({}, client.process, command())
    assert client.stops == [
        ("process-1", "durable:process-1:stop:1"),
        ("process-1", "durable:process-1:stop:1"),
    ]

    class Rejected(Client):
        def stop_execution(self, process, *, dedupe_key=None):
            super().stop_execution(process, dedupe_key=dedupe_key)
            if len(self.stops) == 1:
                raise CdesktopRejectedError("fresh attempt required", status=409)

    rejected = Rejected({"id": "process-1", "status": "running"}, {})
    rejected_queue = Queue([])
    rejected_reconciler = DurableExecutionReconciler(rejected, rejected_queue)
    first = command()
    rejected_reconciler.recover_stalled_process({}, rejected.process, first)
    second = DurableCommand(
        first.id,
        first.session_id,
        first.body,
        first.state,
        first.dedupe_key,
        first.execution_process_id,
        recovery_attempt=2,
    )
    rejected_reconciler.recover_stalled_process({}, rejected.process, second)
    assert rejected.stops == [
        ("process-1", "durable:process-1:stop:1"),
        ("process-1", "durable:process-1:stop:2"),
    ]
    assert rejected_queue.recoveries == [
        ("command-1", 2, "retryable"),
        ("command-1", 2, "stop_accepted"),
    ]
