from __future__ import annotations

from sightmesh import wakes
from sightmesh.cdesktop import (
    CdesktopInterruptedError,
    CdesktopPendingError,
)
from sightmesh.durable import (
    DurableCommand,
    DurableExecutionReconciler,
    NativeCommandQueue,
    supports_durable_recovery,
)
from sightmesh.effects import EffectJournal
from sightmesh.runtime_lock import RUNTIME_LOCK
from sightmesh.task_store import TaskStore


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


class KeyedStopClient(Client):
    """cdesktop's keyed-stop contract: one outcome per key."""

    def __init__(self, *args):
        super().__init__(*args)
        self.stop_outcomes = []
        self.keys = set()

    def stop_execution(self, process, *, dedupe_key=None):
        super().stop_execution(process, dedupe_key=dedupe_key)
        if dedupe_key not in self.keys:
            self.keys.add(dedupe_key)
            self.stop_outcomes.append((process, dedupe_key))


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
                "version": "cdesktop/" + RUNTIME_LOCK.cdesktop.compatibility.minimum
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
    assert client.stops == [("process-1", "durable:command-1:stop")]
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


def test_terminal_wake_refuses_self_delivery_once_across_restarts(tmp_path, caplog):
    client = Client({"id": "process-1", "status": "completed"}, {})
    queue = Queue([command("done")])
    from sightmesh.escalation import EscalationStore

    signal_store = EscalationStore(tmp_path / "signals.sqlite3")
    session = {"id": "child", "parent_session_id": "child"}

    for _ in range(2):
        DurableExecutionReconciler(
            client, queue, signal_store=signal_store
        ).reconcile_session(session)

    assert queue.notifications == []
    assert signal_store.pending() == []
    assert signal_store.has_terminal_dedupe_key("child-command:command-1:done")
    assert caplog.text.count("is its own parent") == 1


def test_parent_notification_is_attributed_to_child():
    class SendingClient:
        def __init__(self) -> None:
            self.sent = []

        def send(self, *args, **kwargs):
            self.sent.append((args, kwargs))

    client = SendingClient()
    NativeCommandQueue(client).notify_parent(
        "parent", "child", "CHILD_DELIVERY: child command-1 done", "delivery-key"
    )

    assert client.sent == [
        (
            ("parent", "CHILD_DELIVERY: child command-1 done", "child"),
            {"dedupe_key": "delivery-key", "intent": "continue"},
        )
    ]


def test_lifecycle_notification_completion_does_not_generate_another_wake():
    client = Client({"id": "process-1", "status": "completed"}, {})
    queue = Queue(
        [
            DurableCommand(
                f"command-{index}",
                "parent",
                "generated lifecycle notification",
                "done",
                key,
                "process-1",
            )
            for index, key in enumerate(
                (
                    "child-command:command-1:done",
                    "child-terminal:child:completed",
                    "signal-policy:child:terminal",
                ),
                start=2,
            )
        ]
    )

    DurableExecutionReconciler(client, queue).reconcile_session(
        {"id": "parent", "parent_session_id": "grandparent"}
    )

    assert queue.notifications == []


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


def test_keyed_stop_is_exactly_once_across_repeated_sweeps():
    """Why: cdesktop's keyed stop fence makes repeated sweep observations one stop."""
    client = KeyedStopClient({"id": "process-1", "status": "running"}, {})
    reconciler = DurableExecutionReconciler(client, Queue([command()]))

    reconciler.recover_stalled_process({}, client.process, command())
    reconciler.recover_stalled_process({}, client.process, command())

    assert client.stop_outcomes == [("process-1", "durable:command-1:stop")]


def test_keyed_stop_is_exactly_once_after_reconciler_restart():
    """Why: a restarted reconciler replays the command key, never a new stop."""
    client = KeyedStopClient({"id": "process-1", "status": "running"}, {})
    for _ in range(2):
        DurableExecutionReconciler(client, Queue([command()])).recover_stalled_process(
            {}, client.process, command()
        )

    assert client.stops == [
        ("process-1", "durable:command-1:stop"),
        ("process-1", "durable:command-1:stop"),
    ]
    assert client.stop_outcomes == [("process-1", "durable:command-1:stop")]


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
    assert client.stops == [("process-1", "durable:command-1:stop")]

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


def test_425_retries_the_same_command_key():
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
        ("process-1", "durable:command-1:stop"),
        ("process-1", "durable:command-1:stop"),
    ]


class KernelClient:
    """Only the send seam the wake outbox uses."""

    def __init__(self):
        self.sent = []

    def send(self, session_id, prompt, sender=None, *, dedupe_key=None, intent=None):
        self.sent.append((session_id, prompt, dedupe_key, intent))
        return {"queued": True}


def _kernel_cohort(tmp_path):
    store = TaskStore(tmp_path / "state.sqlite3")
    ((parent, _),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "manager", "children": 2}],
        max_attempts=3,
    )
    store.activate(
        parent.task_id, workspace_id="ws-manager", session_id="session-manager"
    )
    ((child, _),) = store.reserve_all(
        scope="operator",
        parent_task_id=parent.task_id,
        specs=[{"key": "child", "children": 0}],
        max_attempts=3,
    )
    store.activate(child.task_id, workspace_id="ws-child", session_id="session-child")
    return store, parent, child


def test_the_reconciler_delivers_a_wake_left_pending_by_a_dead_pump(tmp_path):
    """Delivery is best effort at the call site; this pass is what makes it
    at-least-once, so a wake committed by a process that then died must be
    delivered on the next tick without any human noticing."""
    store, _parent, child = _kernel_cohort(tmp_path)
    client = KernelClient()
    # finish_with_wake commits the child transition, the parent's event-seq
    # bump, and the pending wake row in one transaction; the process then dies
    # before it can pump, leaving the wake for this pass to deliver.
    wakes.finish_with_wake(store, child.task_id, "completed", "done")

    result = DurableExecutionReconciler(
        client, Queue([]), task_store=store
    ).reconcile_kernel()

    assert result["wakes_delivered"] == 1
    assert client.sent[0][0] == "session-manager"
    assert client.sent[0][3] == "continue"


def test_the_reconciler_re_arms_a_wake_resolved_while_undeliverable(tmp_path):
    """A wake resolved because its parent was briefly undeliverable must not
    poison re-arm (G1): a resolve never advances ``last_woken_seq``, so the
    watermark still trails the child event and the next reconciler pass arms a
    fresh wake once the parent is reachable, then delivers it exactly once."""
    store = TaskStore(tmp_path / "state.sqlite3")
    # A parent that has not been activated yet has no holder session, so its
    # first wake resolves as undeliverable rather than being sent anywhere.
    ((parent, _),) = store.reserve_all(
        scope="operator",
        parent_task_id=None,
        specs=[{"key": "manager", "children": 2}],
        max_attempts=3,
    )
    ((child, _),) = store.reserve_all(
        scope="operator",
        parent_task_id=parent.task_id,
        specs=[{"key": "child", "children": 0}],
        max_attempts=3,
    )
    store.activate(child.task_id, workspace_id="ws-child", session_id="session-child")
    client = KernelClient()
    wakes.finish_with_wake(store, child.task_id, "completed", "done")

    resolved_pass = DurableExecutionReconciler(
        client, Queue([]), task_store=store
    ).reconcile_kernel()
    assert resolved_pass["wakes_delivered"] == 0
    assert client.sent == []
    with store.connect() as conn:
        assert conn.execute(
            "SELECT state FROM task_wakes"
        ).fetchone()["state"] == "resolved"

    # The parent comes online; a fresh reconciler pass arms a new wake for the
    # unchanged, still-satisfied cohort and delivers it.
    store.activate(
        parent.task_id, workspace_id="ws-manager", session_id="session-manager"
    )
    revived = DurableExecutionReconciler(
        client, Queue([]), task_store=store
    ).reconcile_kernel()

    assert revived["wakes_inserted"] == 1
    assert revived["wakes_delivered"] == 1
    with store.connect() as conn:
        row = conn.execute(
            "SELECT dedupe_key FROM task_wakes WHERE state = 'delivered'"
        ).fetchone()
    assert row["dedupe_key"] == wakes.dedupe_key(
        parent.task_id, "all_children_terminal"
    )


def test_the_reconciler_does_not_manufacture_a_second_wake(tmp_path):
    """It runs every tick over every parent; repairing an already repaired
    cohort must be a no-op (S28) or a manager gets re-woken forever: once the
    watermark caught up to the child event, an unchanged cohort arms nothing."""
    store, _parent, child = _kernel_cohort(tmp_path)
    client = KernelClient()
    wakes.finish_with_wake(store, child.task_id, "completed", "done")
    reconciler = DurableExecutionReconciler(client, Queue([]), task_store=store)

    reconciler.reconcile_kernel()
    second = reconciler.reconcile_kernel()

    assert second == {
        "wakes_inserted": 0,
        "wakes_delivered": 0,
        "effects_expired": 0,
        # The v1.1 liveness pass rides the same tick; a repaired cohort must
        # stay a no-op for it too, not just for the cohort predicates.
        "liveness_findings": 0,
    }
    assert len(client.sent) == 1


def test_the_reconciler_retires_a_reservation_whose_owner_died(tmp_path):
    """A lease that expired with no native session behind it is a task that
    will never run; leaving it 'reserved' hides that from every status view."""
    store, _parent, _child = _kernel_cohort(tmp_path)
    journal = EffectJournal(store)
    journal.reserve("orphan", 1, "hash", "owner-a", ttl=-1.0)

    result = DurableExecutionReconciler(
        KernelClient(), Queue([]), task_store=store
    ).reconcile_kernel()

    assert result["effects_expired"] == 1
    assert journal.get("orphan", 1).outcome == "lost:reservation-expired"
