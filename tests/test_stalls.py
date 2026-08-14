from datetime import UTC, datetime, timedelta

import pytest

from sightmesh.cdesktop import CdesktopError, CdesktopRejectedError
from sightmesh.stalls import RecoveryIntentStore, StallDetector


class FakeClient:
    def __init__(self, processes, snapshots):
        self.processes = processes
        self.snapshots = snapshots
        self.stopped = []
        self.sent = []

    def execution_processes(self, _session_id):
        return self.processes

    def normalized_snapshot(self, process_id):
        return self.snapshots[process_id]

    def execution_process(self, process_id):
        return next(process for process in self.processes if process["id"] == process_id)

    def stop_execution(self, process_id):
        self.stopped.append(process_id)
        self.execution_process(process_id)["status"] = "killed"

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _process():
    return {
        "id": "process-1",
        "status": "running",
        "run_reason": "coding_agent",
        "started_at": "2026-08-14T00:00:00Z",
    }


def test_true_idle_stall_stops_once_and_notifies_parent():
    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = FakeClient([_process()], {"process-1": {"complete": True, "entries": []}})
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=2)
    detector.reconcile(client, session)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent == [
        (
            (
                "parent",
                (
                    "STALL: child execution produced no session events past the configured "
                    "threshold and was handed to native killed-child recovery."
                ),
                "child",
            ),
            {"dedupe_key": "stall:process-1:parent"},
        )
    ]


@pytest.mark.parametrize("command", ["bun test", "postgres", "project build"])
def test_active_child_process_suppresses_stall(command):
    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = FakeClient(
        [_process()],
        {
            "process-1": {
                "complete": False,
                "entries": [{"tool_name": command, "status": "running"}],
            }
        },
    )
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(hours=1)
    detector.reconcile(client, session)

    assert client.stopped == []
    assert client.sent == []


def test_new_event_resets_idle_threshold():
    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = FakeClient(
        [_process()],
        {"process-1": {"complete": True, "entries": [{"content": "compile"}]}},
    )
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=1)
    client.snapshots["process-1"] = {
        "complete": True,
        "entries": [{"content": "compile failed"}],
    }
    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=3)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]


def test_root_session_is_never_eligible_for_stall_recovery():
    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = FakeClient([_process()], {"process-1": {"complete": True, "entries": []}})
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)

    detector.reconcile(client, {"id": "lead"})
    detector.now = lambda: now + timedelta(hours=1)
    detector.reconcile(client, {"id": "lead"})

    assert client.stopped == []
    assert client.sent == []


def test_huge_threshold_cannot_overflow_detector_construction(monkeypatch):
    monkeypatch.setenv("SIGHTMESH_STALL_THRESHOLD_MINUTES", "999999999999999999999")

    detector = StallDetector()

    assert detector.threshold == timedelta(minutes=30)


def test_cold_partial_snapshot_cannot_immediately_trigger_recovery():
    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = FakeClient([_process()], {"process-1": {"complete": False, "entries": []}})
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=1)
    detector.reconcile(client, session)

    assert client.stopped == []
    assert client.sent == []


def test_running_partial_snapshot_without_new_events_eventually_recovers_child():
    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = FakeClient([_process()], {"process-1": {"complete": False, "entries": []}})
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=2)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent[0][0][0] == "parent"


def test_lost_stop_response_never_repeats_stop_and_retries_parent_wake():
    class LostStopClient(FakeClient):
        def __init__(self):
            super().__init__([_process()], {"process-1": {"entries": []}})
            self.stop_responses = [CdesktopError("Cannot reach cdesktop after stop")]
            self.send_responses = [CdesktopError("Cannot reach cdesktop parent")]

        def stop_execution(self, process_id):
            self.stopped.append(process_id)
            self.execution_process(process_id)["status"] = "killed"
            if self.stop_responses:
                raise self.stop_responses.pop()

        def send(self, *args, **kwargs):
            if self.send_responses:
                raise self.send_responses.pop()
            super().send(*args, **kwargs)

    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = LostStopClient()
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=2)
    detector.reconcile(client, session)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent == [
        (
            (
                "parent",
                (
                    "STALL: child execution produced no session events past the configured "
                    "threshold and was handed to native killed-child recovery."
                ),
                "child",
            ),
            {"dedupe_key": "stall:process-1:parent"},
        )
    ]


def test_definitive_stop_failure_remains_retryable():
    class RetryStopClient(FakeClient):
        def __init__(self):
            super().__init__([_process()], {"process-1": {"entries": []}})
            self.stop_responses = [
                CdesktopRejectedError("POST /execution-processes/process-1/stop failed: HTTP 500")
            ]

        def stop_execution(self, process_id):
            self.stopped.append(process_id)
            if self.stop_responses:
                raise self.stop_responses.pop()
            self.execution_process(process_id)["status"] = "killed"

    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = RetryStopClient()
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=2)
    detector.reconcile(client, session)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1", "process-1"]
    assert client.sent[0][0][0] == "parent"


def test_restart_after_intent_before_stop_retries_from_authoritative_running_state():
    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = FakeClient([_process()], {"process-1": {"entries": []}})
    store = RecoveryIntentStore()
    store.begin("process-1")
    detector = StallDetector(
        threshold=timedelta(minutes=2), now=lambda: now, recovery_store=store
    )

    detector.reconcile(client, {"id": "child", "parent_session_id": "parent"})

    assert client.stopped == ["process-1"]
    assert client.sent[0][0][0] == "parent"


def test_definitive_server_rejection_retries_without_parent_wake():
    class RejectedStopClient(FakeClient):
        def __init__(self):
            super().__init__([_process()], {"process-1": {"entries": []}})
            self.stop_responses = [CdesktopRejectedError("server rejected stop")]

        def stop_execution(self, process_id):
            self.stopped.append(process_id)
            if self.stop_responses:
                raise self.stop_responses.pop()
            self.execution_process(process_id)["status"] = "killed"

    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = RejectedStopClient()
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=2)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent == []

    detector.reconcile(client, session)

    assert client.stopped == ["process-1", "process-1"]
    assert client.sent[0][0][0] == "parent"


def test_accepted_stop_waits_for_confirmation_without_a_duplicate_request(tmp_path):
    class DelayedConfirmationClient(FakeClient):
        def __init__(self):
            super().__init__([_process()], {"process-1": {"entries": []}})
            self.confirmation_reads = 0

        def execution_process(self, process_id):
            self.confirmation_reads += 1
            if self.confirmation_reads == 2:
                raise CdesktopError("confirmation read timed out")
            return super().execution_process(process_id)

        def stop_execution(self, process_id):
            self.stopped.append(process_id)

    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = DelayedConfirmationClient()
    store_path = tmp_path / "stall-recovery.json"
    detector = StallDetector(
        threshold=timedelta(minutes=2),
        now=lambda: now,
        recovery_store=RecoveryIntentStore(store_path),
    )
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=2)
    detector.reconcile(client, session)
    restarted = StallDetector(
        threshold=timedelta(minutes=2),
        now=lambda: now + timedelta(minutes=2),
        recovery_store=RecoveryIntentStore(store_path),
    )
    restarted.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent == []

    client.processes[0]["status"] = "killed"
    restarted.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent[0][0][0] == "parent"


def test_ambiguous_stop_with_delayed_running_transition_never_repeats_stop():
    class AmbiguousStopClient(FakeClient):
        def __init__(self):
            super().__init__([_process()], {"process-1": {"entries": []}})
            self.stop_responses = [CdesktopError("connection closed before response")]

        def stop_execution(self, process_id):
            self.stopped.append(process_id)
            if self.stop_responses:
                raise self.stop_responses.pop()
            self.execution_process(process_id)["status"] = "killed"

    now = datetime(2026, 8, 14, tzinfo=UTC)
    client = AmbiguousStopClient()
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child", "parent_session_id": "parent"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=2)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent == []

    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent == []

    client.processes[0]["status"] = "killed"
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
    assert client.sent[0][0][0] == "parent"
