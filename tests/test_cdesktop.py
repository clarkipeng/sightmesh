from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sightmesh import cdesktop
from sightmesh.cdesktop import CdesktopClient, _apply_approval_patches


class FakeClient(CdesktopClient):
    def __init__(self) -> None:
        self.base_url = "http://127.0.0.1:1"
        self.calls = []

    def repos(self):
        return [{"id": "existing", "path": "/tmp/repo"}]

    def request(self, method, path, payload=None, query=None, headers=None):
        self.calls.append((method, path, payload, query, headers))
        return {"id": "created"}


def test_register_repo_reuses_exact_path() -> None:
    client = FakeClient()
    repo = client.register_repo(Path("/tmp/repo"))
    assert repo["id"] == "existing"
    assert client.calls == []


def test_set_parent_rejects_self_link_before_request() -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="cannot be its own parent"):
        client.set_parent("session-a", "session-a")

    assert client.calls == []


def test_managed_launch_uses_the_task_epoch_route() -> None:
    client = FakeClient()
    launch = {"kind": "workspace", "request": {"prompt": "audit"}}

    client.managed_launch("task-a", 2, launch)
    client.managed_effect("task-a", 2)

    assert client.calls == [
        ("PUT", "/managed-tasks/task-a/epochs/2", launch, None, None),
        ("GET", "/managed-tasks/task-a/epochs/2", None, None, None),
    ]


def test_success_false_is_a_typed_server_rejection(monkeypatch) -> None:
    class Response:
        status = 200

        def read(self):
            return b'{"success": false, "message": "stop rejected"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cdesktop, "urlopen", lambda *_args, **_kwargs: Response())
    client = CdesktopClient("http://127.0.0.1:1")

    with pytest.raises(cdesktop.CdesktopRejectedError, match="stop rejected"):
        client.stop_execution("process-a")


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (409, cdesktop.CdesktopRejectedError),
        (424, cdesktop.CdesktopInterruptedError),
        (425, cdesktop.CdesktopPendingError),
    ],
)
def test_stop_operation_http_outcomes_are_typed(
    monkeypatch, status, error_type
) -> None:
    error = HTTPError(
        "http://127.0.0.1:1/api/execution-processes/process-a/stop",
        status,
        "stop outcome",
        None,
        BytesIO(b"stop outcome"),
    )
    monkeypatch.setattr(
        cdesktop, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )
    client = CdesktopClient("http://127.0.0.1:1")

    with pytest.raises(error_type) as raised:
        client.stop_execution("process-a", dedupe_key="stall:process-a:stop:1")

    assert raised.value.status == status


def test_failed_start_cleans_the_one_new_native_workspace() -> None:
    class Client(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.workspace_lists = iter(
                [
                    [{"id": "existing", "name": "other"}],
                    [
                        {"id": "existing", "name": "other"},
                        {"id": "partial", "name": "worker"},
                    ],
                ]
            )

        def workspaces(self):
            return next(self.workspace_lists)

        def request(self, method, path, payload=None, query=None, headers=None):
            self.calls.append((method, path, payload, query, headers))
            if path == "/workspaces/start":
                raise cdesktop.CdesktopError(
                    "POST /workspaces/start failed: HTTP 500: SpawnError"
                )
            return {"id": "created"}

    client = Client()
    with pytest.raises(
        cdesktop.CdesktopError, match="native cleanup deleted partial workspace partial"
    ):
        client.spawn_workspace(
            name="worker",
            repo_path=Path("/tmp/repo"),
            target_branch="main",
            executor="CODEX",
            prompt="start",
            use_worktree=True,
            permission_policy="SUPERVISED",
            model=None,
            reasoning=None,
            provider_id=None,
        )
    assert client.calls[-1] == (
        "DELETE",
        "/workspaces/partial",
        None,
        {"delete_remote": False, "delete_branches": False},
        None,
    )


def test_failed_start_never_guesses_between_partial_workspaces() -> None:
    class Client(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.workspace_lists = iter(
                [
                    [],
                    [
                        {"id": "partial-a", "name": "worker"},
                        {"id": "partial-b", "name": "worker"},
                    ],
                ]
            )

        def workspaces(self):
            return next(self.workspace_lists)

        def request(self, method, path, payload=None, query=None, headers=None):
            if path == "/workspaces/start":
                raise cdesktop.CdesktopError("SpawnError")
            return {"id": "created"}

    client = Client()
    with pytest.raises(cdesktop.CdesktopError, match="cleanup was ambiguous"):
        client.spawn_workspace(
            name="worker",
            repo_path=Path("/tmp/repo"),
            target_branch="main",
            executor="CODEX",
            prompt="start",
            use_worktree=True,
            permission_policy="SUPERVISED",
            model=None,
            reasoning=None,
            provider_id=None,
        )


def test_send_marks_sender_when_provided() -> None:
    client = FakeClient()
    client.send("target", "hello", "sender")
    assert client.calls == [
        (
            "POST",
            "/sessions/target/follow-up",
            {"prompt": "hello", "intent": "continue"},
            None,
            {"x-cdesktop-from-session": "sender"},
        )
    ]


def test_respond_to_approval_binds_execution_process() -> None:
    client = FakeClient()
    client.respond_to_approval(
        "approval-a", "process-a", approved=False, reason="Revise the plan"
    )
    assert client.calls == [
        (
            "POST",
            "/approvals/approval-a/respond",
            {
                "execution_process_id": "process-a",
                "status": {"status": "denied", "reason": "Revise the plan"},
            },
            None,
            None,
        )
    ]


def test_respond_to_question_binds_process_and_structured_answers() -> None:
    client = FakeClient()
    answers = [{"question": "Ship it?", "answer": ["Yes"]}]
    client.respond_to_question("approval-a", "process-a", answers)
    assert client.calls == [
        (
            "POST",
            "/approvals/approval-a/respond",
            {
                "execution_process_id": "process-a",
                "status": {"status": "answered", "answers": answers},
            },
            None,
            None,
        )
    ]


def test_apply_approval_patches_handles_snapshot_and_resolution() -> None:
    pending = _apply_approval_patches(
        {},
        [
            {
                "op": "replace",
                "path": "/pending",
                "value": {
                    "approval-a": {
                        "approval_id": "approval-a",
                        "tool_name": "ExitPlanMode",
                    }
                },
            }
        ],
    )
    assert list(pending) == ["approval-a"]
    assert (
        _apply_approval_patches(
            pending, [{"op": "remove", "path": "/pending/approval-a"}]
        )
        == {}
    )


def test_rename_workspace_preserves_other_mutable_fields() -> None:
    client = FakeClient()
    client.rename_workspace("workspace-a", "project/task")
    assert client.calls == [
        (
            "PUT",
            "/workspaces/workspace-a",
            {"archived": None, "pinned": None, "name": "project/task"},
            None,
            None,
        )
    ]


def test_wait_for_workspace_idle_returns_terminal_summary() -> None:
    client = FakeClient()
    summaries = iter(
        [
            [{"workspace_id": "workspace-a", "latest_process_status": "running"}],
            [{"workspace_id": "workspace-a", "latest_process_status": "killed"}],
        ]
    )
    client.workspace_summaries = lambda _archived=False: next(summaries)
    summary = client.wait_for_workspace_idle(
        "workspace-a", timeout_seconds=1, poll_seconds=0
    )
    assert summary["latest_process_status"] == "killed"


def test_execution_process_and_snapshot_routes_are_native_gets() -> None:
    client = FakeClient()
    client.request = lambda method, path, payload=None, query=None, headers=None: (
        [] if path == "/execution-processes" else {"entries": []}
    )
    assert client.execution_processes("session-a") == []
    assert client.normalized_snapshot("process-a") == {"entries": []}


def test_stop_execution_targets_one_process() -> None:
    client = FakeClient()
    client.stop_execution("process-a")
    assert client.calls == [
        (
            "POST",
            "/execution-processes/process-a/stop",
            {},
            None,
            None,
        )
    ]


def test_stop_execution_passes_process_scoped_dedupe_key() -> None:
    client = FakeClient()
    client.stop_execution("process-a", dedupe_key="stall:process-a:stop")
    assert client.calls == [
        (
            "POST",
            "/execution-processes/process-a/stop",
            {"dedupe_key": "stall:process-a:stop"},
            None,
            None,
        )
    ]


def test_native_command_requeue_and_dispatch_routes_are_process_scoped() -> None:
    client = FakeClient()

    client.requeue_execution_commands("session-a", "process-a")
    client.dispatch_queued("session-a")

    assert client.calls == [
        (
            "POST",
            "/sessions/session-a/commands/requeue",
            {"execution_process_id": "process-a"},
            None,
            None,
        ),
        ("POST", "/sessions/session-a/commands/dispatch", None, None, None),
    ]


def test_create_direct_workspace_record_and_attach_repo(tmp_path) -> None:
    client = FakeClient()
    client.repos = list

    client.create_workspace_record("migrated-work", use_worktree=False)
    client.add_workspace_repo("workspace", tmp_path, "main", "source")

    assert client.calls == [
        (
            "POST",
            "/workspaces",
            {"name": "migrated-work", "use_worktree": False},
            None,
            None,
        ),
        (
            "POST",
            "/repos",
            {"path": str(tmp_path.resolve()), "display_name": "source"},
            None,
            None,
        ),
        (
            "POST",
            "/workspaces/workspace/repos",
            {"repo_id": "created", "target_branch": "main"},
            None,
            None,
        ),
    ]


def test_configure_local_preserves_config_and_forces_privacy(tmp_path) -> None:
    client = FakeClient()
    client.info = lambda: {
        "config": {"theme": "DARK", "analytics_enabled": True, "relay_enabled": True}
    }
    client.configure_local(tmp_path)
    method, path, payload, _, _ = client.calls[0]
    assert (method, path) == ("PUT", "/config")
    assert payload["theme"] == "DARK"
    assert payload["analytics_enabled"] is False
    assert payload["relay_enabled"] is False
    assert payload["workspace_dir"] == str(tmp_path.resolve())


def test_dirty_repositories_reports_direct_checkout(monkeypatch, tmp_path) -> None:
    client = FakeClient()
    client.workspace = lambda _: {"use_worktree": False}
    client.workspace_repos = lambda _: [
        {"is_git": True, "path": str(tmp_path), "name": "repo"}
    ]

    class Result:
        returncode = 0
        stdout = "?? pending.txt\n"
        stderr = ""

    monkeypatch.setattr(
        "sightmesh.cdesktop.subprocess.run", lambda *args, **kwargs: Result()
    )
    assert client.dirty_repositories("workspace") == [
        {"path": str(tmp_path), "status": "?? pending.txt"}
    ]


def test_delete_workspace_encodes_rust_booleans_in_lowercase(monkeypatch) -> None:
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"success":true,"data":null}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(cdesktop, "urlopen", fake_urlopen)
    CdesktopClient("http://127.0.0.1:3210").delete_workspace("workspace-a")

    assert requests[0][0].full_url.endswith("delete_remote=false&delete_branches=false")


def test_missing_repository_is_reconciled_not_dirty(tmp_path) -> None:
    """A path that does not exist holds no uncommitted work.

    Live condition: cdesktop had already reclaimed these managed worktrees, so
    the directory was gone. Reporting that as dirty state blocked archive, and
    delete refuses anything not yet archived, leaving the workspace unremovable.
    """
    container = tmp_path / "reclaimed"
    client = FakeClient()
    client.workspace = lambda _: {
        "use_worktree": True,
        "container_ref": str(container),
        "archived": False,
    }
    client.workspace_repos = lambda _: [
        {"is_git": True, "path": "/unused", "name": "repo"}
    ]

    assert client.dirty_repositories("workspace") == []
    assert client.missing_repositories("workspace") == [
        {
            "path": str(container / "repo"),
            "status": "repository path is missing",
        }
    ]


def test_missing_container_ref_is_reconciled_not_dirty() -> None:
    """A managed worktree that was never materialised has nothing on disk."""
    client = FakeClient()
    client.workspace = lambda _: {
        "use_worktree": True,
        "container_ref": None,
        "archived": False,
    }
    client.workspace_repos = lambda _: [
        {"is_git": True, "path": "/unused", "name": "repo"}
    ]

    assert client.dirty_repositories("workspace") == []
    assert client.missing_repositories("workspace") == []


# --- follow-up delivery: transport failure is 'unknown', never silent loss ---
# Why: a coordinator lost 24h when `sightmesh message` timed out under host load
# and the directive never landed (issue #90). Every keyed send is idempotent on
# dedupe_key, so retrying is safe; what must never happen is a quiet failure.


def _flaky_client(monkeypatch, failures: int, queued_after: bool):
    client = cdesktop.CdesktopClient("http://127.0.0.1:1")
    calls: list[dict] = []

    def fake_request(method, path, payload=None, query=None, headers=None):
        if path.endswith("/follow-up"):
            calls.append(payload)
            if len(calls) <= failures:
                raise cdesktop.CdesktopTransportError("timeout")
            return {"id": "cmd-1", "dedupe_key": payload["dedupe_key"]}
        if path.endswith("/commands"):
            return [{"dedupe_key": "order:x"}] if queued_after else []
        raise AssertionError(path)

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr(cdesktop.time, "sleep", lambda *_: None)
    return client, calls


def test_send_retries_the_identical_keyed_post_after_transport_failure(monkeypatch):
    client, calls = _flaky_client(monkeypatch, failures=2, queued_after=False)
    result = client.send("s1", "do it", dedupe_key="order:x")
    assert result["delivery"] == "queued"
    assert len(calls) == 3
    assert {c["dedupe_key"] for c in calls} == {"order:x"}  # same key every retry


def test_send_reports_already_queued_when_the_row_landed_despite_timeouts(monkeypatch):
    client, _ = _flaky_client(monkeypatch, failures=99, queued_after=True)
    result = client.send("s1", "do it", dedupe_key="order:x")
    assert result["delivery"] == "already_queued"


def test_send_is_loud_when_delivery_cannot_be_confirmed(monkeypatch):
    client, _ = _flaky_client(monkeypatch, failures=99, queued_after=False)
    with pytest.raises(cdesktop.CdesktopDeliveryError, match="could NOT be confirmed"):
        client.send("s1", "do it", dedupe_key="order:x")


def test_unkeyed_send_does_not_retry(monkeypatch):
    """Without a dedupe_key a retry could duplicate the message; fail once, loudly."""
    client, calls = _flaky_client(monkeypatch, failures=99, queued_after=False)
    with pytest.raises(cdesktop.CdesktopDeliveryError):
        client.send("s1", "do it")
    assert len(calls) == 1
