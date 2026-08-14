from datetime import UTC, datetime, timedelta

import pytest

from sightmesh.stalls import StallDetector


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

    def stop_execution(self, process_id):
        self.stopped.append(process_id)

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
