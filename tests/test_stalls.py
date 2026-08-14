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
    client = FakeClient([_process()], {"process-1": {"entries": []}})
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
        {"process-1": {"entries": [{"tool_name": command, "status": "running"}]}},
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
    client = FakeClient([_process()], {"process-1": {"entries": [{"content": "compile"}]}})
    detector = StallDetector(threshold=timedelta(minutes=2), now=lambda: now)
    session = {"id": "child"}

    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=1)
    client.snapshots["process-1"] = {"entries": [{"content": "compile failed"}]}
    detector.reconcile(client, session)
    detector.now = lambda: now + timedelta(minutes=3)
    detector.reconcile(client, session)

    assert client.stopped == ["process-1"]
