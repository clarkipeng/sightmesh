from __future__ import annotations

from sightmesh.cdesktop import CdesktopInterruptedError, CdesktopPendingError
from sightmesh.durable import DurableExecutionReconciler


class Queue:
    def __init__(self, rows):
        self.rows = rows
        self.interrupted = []
        self.requeued = []
        self.notifications = []

    def commands(self, _session):
        return list(self.rows)

    def interrupt(self, command):
        self.interrupted.append(command.id)

    def requeue(self, command):
        self.requeued.append((command.id, command.dedupe_key))

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


def test_restart_after_claim_interrupts_and_requeues_same_command():
    client = Client({"id": "process-1", "status": "killed"}, {})
    queue = Queue([command()])
    reconciler = DurableExecutionReconciler(client, queue)

    reconciler.reconcile_session({"id": "session-1"})
    reconciler.reconcile_session({"id": "session-1"})

    assert queue.interrupted == ["command-1"]
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
    assert queue.requeued == [("command-1", "same-key")]
    assert client.dispatches == []


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


def test_424_stop_interrupts_without_repeated_stop():
    class Interrupted(Client):
        def stop_execution(self, process, *, dedupe_key=None):
            super().stop_execution(process, dedupe_key=dedupe_key)
            raise CdesktopInterruptedError("unknown", status=424)

    client = Interrupted({"id": "process-1", "status": "running"}, {})
    queue = Queue([command()])
    reconciler = DurableExecutionReconciler(client, queue)
    reconciler.recover_stalled_process({"id": "child"}, client.process, command())
    reconciler.recover_stalled_process({"id": "child"}, client.process, command())

    assert len(client.stops) == 1
    assert queue.requeued == [("command-1", "same-key")]


def test_425_retries_same_key_and_409_is_retryable():
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
    reconciler = DurableExecutionReconciler(client, Queue([]))
    reconciler.recover_stalled_process({}, client.process, command())
    reconciler.recover_stalled_process({}, client.process, command())
    assert client.stops == [
        ("process-1", "durable:command-1:stop"),
        ("process-1", "durable:command-1:stop"),
    ]
