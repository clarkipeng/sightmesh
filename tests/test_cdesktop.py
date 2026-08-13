from pathlib import Path

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


def test_send_marks_sender_when_provided() -> None:
    client = FakeClient()
    client.send("target", "hello", "sender")
    assert client.calls == [
        (
            "POST",
            "/sessions/target/follow-up",
            {"prompt": "hello"},
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
