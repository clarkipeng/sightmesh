import argparse
import json
import subprocess
from pathlib import Path

import pytest

from sightmesh import __version__, approvals, cli, escalation, succession
from sightmesh.cli import (
    _fleet_sessions,
    _is_sightmesh_cdesktop_version,
    _latest_process,
    _normalized_snapshot_with_retry,
    _pending_request_from_snapshot,
    _primary_session_id,
    _read_text,
    _repowire_status_ok,
    _resolve_session,
    _validate_reasoning,
    _with_coordination_contract,
    _workspace_repository_paths,
    parser,
)
from sightmesh.leases import LeaseStore
from sightmesh.profiles import Profile, ProfileStore
from sightmesh.runtime_lock import RUNTIME_LOCK


def test_read_text_requires_one_source(tmp_path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("from file", encoding="utf-8")
    assert _read_text(None, str(prompt), "prompt") == "from file"
    assert _read_text("inline", None, "prompt") == "inline"


def test_sightmesh_cdesktop_version_requires_safe_command_lifecycle() -> None:
    minimum = RUNTIME_LOCK.cdesktop.compatibility.minimum
    major, minor, patch = RUNTIME_LOCK.cdesktop.compatibility.minimum_tuple
    older = f"{major}.{minor}.{patch - 1}"
    assert _is_sightmesh_cdesktop_version(f"cdesktop/{minimum}-sightmesh.0")
    assert _is_sightmesh_cdesktop_version(f"cdesktop/{minimum} darwin-arm64")
    assert not _is_sightmesh_cdesktop_version(f"cdesktop {older}-sightmesh.1")
    assert not _is_sightmesh_cdesktop_version(f"cdesktop/{older} darwin-arm64")
    assert not _is_sightmesh_cdesktop_version(older)
    assert not _is_sightmesh_cdesktop_version(None)


def test_bootstrap_derives_and_verifies_the_locked_cdesktop_release() -> None:
    bootstrap = (
        Path(__file__).parents[1] / "scripts" / "bootstrap-local.sh"
    ).read_text(encoding="utf-8")
    assert "from sightmesh.runtime_lock import RUNTIME_LOCK" in bootstrap
    assert "verify_file_sha256" in bootstrap
    assert RUNTIME_LOCK.cdesktop.package.url not in bootstrap


def test_update_stage_defaults_to_verified_runtime_lock(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        cli.updates,
        "stage",
        lambda package, version, *, expected_sha256: calls.append(
            (package, version, expected_sha256)
        )
        or {"status": "staged"},
    )
    args = parser().parse_args(["update", "stage"])
    args.quiet = True

    assert cli.cmd_update(args) == 0
    runtime = RUNTIME_LOCK.cdesktop
    assert calls == [(runtime.package.url, runtime.version, runtime.package.sha256)]


def test_update_stage_override_requires_verification_or_local_mode(
    monkeypatch, tmp_path
) -> None:
    package = tmp_path / "cdesktop.tgz"
    package.write_bytes(b"development package")
    args = parser().parse_args(["update", "stage", "--package", str(package)])
    args.quiet = True
    with pytest.raises(ValueError, match="require --sha256"):
        cli.cmd_update(args)

    calls = []
    monkeypatch.setattr(
        cli.updates,
        "stage",
        lambda source, version, *, expected_sha256: calls.append(
            (source, version, expected_sha256)
        )
        or {"status": "staged"},
    )
    args.local_development = True
    cli.cmd_update(args)
    assert calls == [(str(package), RUNTIME_LOCK.cdesktop.version, None)]


def test_coordination_contract_is_compact_and_idempotent() -> None:
    prompt = _with_coordination_contract("Do the bounded review.")
    assert "sightmesh peers" in prompt
    assert "sightmesh steer @agent" in prompt
    assert "Batch independent read-only tool calls" in prompt
    assert _with_coordination_contract(prompt) == prompt


def test_fleet_selector_resolves_workspace_and_named_peer() -> None:
    class FleetClient:
        def workspace_summaries(self, archived=False):
            assert archived is False
            return [{"workspace_id": "workspace-a", "latest_process_status": "running"}]

        def workspaces(self):
            return [
                {
                    "id": "workspace-a",
                    "name": "alpha",
                    "branch": "feature-a",
                    "archived": False,
                }
            ]

        def sessions(self, _workspace_id):
            return [
                {"id": "lead-a", "name": None, "created_at": "1", "executor": "CODEX"},
                {
                    "id": "peer-a",
                    "name": "reviewer",
                    "created_at": "2",
                    "executor": "CLAUDE_CODE",
                },
            ]

    client = FleetClient()
    rows = _fleet_sessions(client)
    assert [row["selector"] for row in rows] == ["alpha", "reviewer"]
    assert _resolve_session(client, "@alpha")["session_id"] == "lead-a"
    assert _resolve_session(client, "reviewer")["session_id"] == "peer-a"


def test_normalized_snapshot_retries_cold_partial_result() -> None:
    class SnapshotClient:
        def __init__(self) -> None:
            self.responses = iter(
                [
                    {"entries": [], "patch_count": 1, "complete": False},
                    {
                        "entries": [
                            {"content": {"entry_type": {"type": "assistant_message"}}}
                        ],
                        "patch_count": 41,
                        "complete": True,
                    },
                ]
            )

        def normalized_snapshot(self, _execution_process_id):
            return next(self.responses)

    result = _normalized_snapshot_with_retry(SnapshotClient(), "process-a")
    assert result["complete"] is True
    assert result["patch_count"] == 41


def test_latest_process_selects_maximum_event_time_from_unsorted_rows() -> None:
    latest = _latest_process(
        [
            {"id": "newer", "completed_at": "2026-08-18T02:00:00Z"},
            {"id": "older", "completed_at": "2026-08-18T01:00:00Z"},
        ]
    )
    assert latest and latest["id"] == "newer"


def test_latest_process_breaks_equal_time_ties_by_process_id() -> None:
    latest = _latest_process(
        [
            {"id": "process-a", "updated_at": "2026-08-18T02:00:00Z"},
            {"id": "process-b", "updated_at": "2026-08-18T02:00:00Z"},
        ]
    )
    assert latest and latest["id"] == "process-b"


def test_latest_process_excludes_dropped_and_devserver_rows() -> None:
    latest = _latest_process(
        [
            {
                "id": "dropped",
                "updated_at": "2099-01-01T00:00:00Z",
                "dropped": True,
            },
            {
                "id": "devserver",
                "updated_at": "2099-01-01T00:00:00Z",
                "run_reason": "devserver",
            },
            {"id": "agent", "updated_at": "2026-08-18T00:00:00Z"},
        ]
    )
    assert latest and latest["id"] == "agent"


def test_latest_process_prefers_valid_time_and_deterministically_handles_missing() -> (
    None
):
    timed = _latest_process(
        [
            {"id": "missing-z"},
            {"id": "timed", "created_at": "2020-01-01T00:00:00Z"},
        ]
    )
    missing = _latest_process([{"id": "missing-a"}, {"id": "missing-z"}])
    assert timed and timed["id"] == "timed"
    assert missing and missing["id"] == "missing-z"


def test_peers_and_peek_share_time_based_latest_selection(monkeypatch, capsys) -> None:
    class UnsortedClient:
        def workspace_summaries(self, archived=False):
            assert archived is False
            return [{"workspace_id": "workspace-a", "latest_process_status": "done"}]

        def workspaces(self):
            return [{"id": "workspace-a", "name": "alpha", "archived": False}]

        def sessions(self, _workspace_id):
            return [{"id": "session-a", "created_at": "1", "executor": "CODEX"}]

        def execution_processes(self, _session_id):
            return [
                {
                    "id": "newer",
                    "status": "completed",
                    "completed_at": "2026-08-18T02:00:00Z",
                },
                {
                    "id": "older",
                    "status": "failed",
                    "completed_at": "2026-08-18T01:00:00Z",
                },
            ]

        def normalized_snapshot(self, process_id):
            assert process_id == "newer"
            return {"complete": True, "entries": []}

        def workspace(self, _workspace_id):
            return {"use_worktree": False}

        def workspace_repos(self, _workspace_id):
            return []

    client = UnsortedClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)

    assert (
        cli.cmd_peers(argparse.Namespace(url=None, include_archived=False, json=True))
        == 0
    )
    peers = json.loads(capsys.readouterr().out)
    assert peers[0]["execution_process_id"] == "newer"
    assert peers[0]["status"] == "completed"

    assert (
        cli.cmd_peek(
            argparse.Namespace(
                url=None,
                agent="@alpha",
                include_archived=False,
                tools=3,
                max_chars=600,
                json=True,
            )
        )
        == 0
    )
    peek = json.loads(capsys.readouterr().out)
    assert peek["execution_process_id"] == "newer"
    assert peek["status"] == "completed"


def test_workspace_repository_paths_expose_source_and_checkout() -> None:
    class WorkspaceClient:
        def workspace(self, _workspace_id):
            return {
                "use_worktree": True,
                "container_ref": "/managed/workspace-a",
            }

        def workspace_repos(self, _workspace_id):
            return [{"name": "catapult-games", "path": "/source/catapult-games"}]

    assert _workspace_repository_paths(WorkspaceClient(), "workspace-a") == [
        {
            "name": "catapult-games",
            "source": "/source/catapult-games",
            "checkout": "/managed/workspace-a/catapult-games",
        }
    ]


def test_parser_registers_compact_fleet_commands() -> None:
    assert parser().parse_args(["overview"]).func is cli.cmd_overview
    assert parser().parse_args(["peers"]).func is cli.cmd_peers
    assert parser().parse_args(["peek", "@reviewer"]).func is cli.cmd_peek
    assert parser().parse_args(["inbox"]).func is cli.cmd_inbox
    assert parser().parse_args(["respond", "--responses", "[]"]).func is cli.cmd_respond


def test_pending_request_is_derived_from_matching_approval() -> None:
    snapshot = {
        "entries": [
            {
                "content": {
                    "content": "2 questions",
                    "entry_type": {
                        "type": "tool_use",
                        "status": {
                            "status": "pending_approval",
                            "approval_id": "approval-a",
                        },
                        "action_type": {
                            "action": "ask_user_question",
                            "questions": [
                                {
                                    "question": "Ship it?",
                                    "header": "Release",
                                    "options": [],
                                    "multiSelect": False,
                                }
                            ],
                        },
                    },
                }
            }
        ]
    }
    request = _pending_request_from_snapshot(snapshot, "approval-a")
    assert request["summary"] == "2 questions"
    assert request["action"]["questions"][0]["question"] == "Ship it?"


def test_batch_response_answers_questions_and_approves_plan(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    class BatchClient:
        def __init__(self, _url=None) -> None:
            self.responses = []

        def pending_approvals(self):
            return [
                {
                    "approval_id": "question-a",
                    "execution_process_id": "process-question",
                    "tool_name": "AskUserQuestion",
                    "is_question": True,
                    "created_at": "1",
                    "timeout_at": "9",
                },
                {
                    "approval_id": "plan-a",
                    "execution_process_id": "process-plan",
                    "tool_name": "ExitPlanMode",
                    "is_question": False,
                    "created_at": "2",
                    "timeout_at": "9",
                },
            ]

        def execution_process(self, process_id):
            return {"session_id": f"session-{process_id}"}

        def session(self, _session_id):
            return {"workspace_id": "workspace-a", "executor": "CODEX"}

        def workspace(self, _workspace_id):
            return {"name": "catapult", "archived": False}

        def normalized_snapshot(self, process_id):
            approval_id = "question-a" if process_id == "process-question" else "plan-a"
            action = (
                {
                    "action": "ask_user_question",
                    "questions": [
                        {
                            "question": "Ship it?",
                            "header": "Release",
                            "options": [],
                            "multiSelect": False,
                        },
                        {
                            "question": "Checks?",
                            "header": "Validation",
                            "options": [],
                            "multiSelect": True,
                        },
                    ],
                }
                if approval_id == "question-a"
                else {"action": "plan_presentation", "plan": "Do the work"}
            )
            return {
                "complete": True,
                "patch_count": 1,
                "entries": [
                    {
                        "content": {
                            "content": "pending",
                            "entry_type": {
                                "type": "tool_use",
                                "status": {
                                    "status": "pending_approval",
                                    "approval_id": approval_id,
                                },
                                "action_type": action,
                            },
                        }
                    }
                ],
            }

        def respond_to_question(self, approval_id, process_id, answers):
            self.responses.append((approval_id, process_id, answers))
            return {"status": "answered"}

        def respond_to_approval(
            self, approval_id, process_id, *, approved, reason=None
        ):
            self.responses.append((approval_id, process_id, approved, reason))
            return {"status": "approved"}

    client = BatchClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    monkeypatch.setattr(approvals, "approval_db_path", lambda: tmp_path / "audit.db")
    payload = json.dumps(
        [
            {"approval_id": "question-a", "answers": ["Yes", ["Unit", "UI"]]},
            {"approval_id": "plan-a", "decision": "approve"},
        ]
    )
    args = parser().parse_args(["--json", "respond", "--responses", payload])

    assert args.func(args) == 0
    assert client.responses == [
        (
            "question-a",
            "process-question",
            [
                {"question": "Ship it?", "answer": ["Yes"]},
                {"question": "Checks?", "answer": ["Unit", "UI"]},
            ],
        ),
        ("plan-a", "process-plan", True, None),
    ]
    assert '"failed": 0' in capsys.readouterr().out


def test_namespace_import_is_available() -> None:
    assert argparse.Namespace is not None


def test_version_flag_uses_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_inline_text_flags_are_available_for_text_commands() -> None:
    parsed = [
        parser().parse_args(["message", "session", "--message", "hello"]),
        parser().parse_args(["steer", "session", "--message", "correct course"]),
        parser().parse_args(["prompt-idle", "session", "--message", "continue"]),
        parser().parse_args(
            [
                "failover",
                "workspace",
                "--profile",
                "backup",
                "--checkpoint",
                "clean checkpoint",
            ]
        ),
        parser().parse_args(
            ["teammate-spawn", "--name", "reviewer", "--prompt", "review this"]
        ),
        parser().parse_args(["close", "workspace", "--message", "reconcile"]),
        parser().parse_args(
            [
                "bridge-reply",
                "correlation",
                "--from-peer",
                "manager",
                "--message",
                "done",
            ]
        ),
        parser().parse_args(
            ["approval", "reject", "approval", "--reason", "revise plan"]
        ),
    ]
    assert len(parsed) == 8


def test_max_reasoning_is_supported_for_claude_and_codex() -> None:
    _validate_reasoning("CLAUDE_CODE", "max")
    _validate_reasoning("CODEX", "max")


def test_approval_command_approves_reviewed_plan(monkeypatch, tmp_path, capsys) -> None:
    class ApprovalClient:
        def __init__(self, _url=None) -> None:
            self.responses = []

        def pending_approvals(self):
            return [
                {
                    "approval_id": "approval-a",
                    "execution_process_id": "process-a",
                    "tool_name": "ExitPlanMode",
                    "is_question": False,
                    "created_at": "2026-08-12T00:00:00Z",
                }
            ]

        def execution_process(self, _process_id):
            return {"session_id": "worker-session"}

        def session(self, _session_id):
            return {
                "workspace_id": "worker-workspace",
                "executor": "CLAUDE_CODE",
                "name": "worker",
            }

        def workspace(self, _workspace_id):
            return {"name": "worker", "archived": False}

        def normalized_snapshot(self, _process_id):
            return {"entries": [], "patch_count": 0, "complete": True}

        def respond_to_approval(
            self, approval_id, execution_process_id, *, approved, reason=None
        ):
            self.responses.append((approval_id, execution_process_id, approved, reason))
            return {"status": "approved"}

    instances = []

    def client_factory(url=None):
        client = ApprovalClient(url)
        instances.append(client)
        return client

    monkeypatch.setattr(cli, "CdesktopClient", client_factory)
    monkeypatch.setattr(approvals, "approval_db_path", lambda: tmp_path / "audit.db")
    monkeypatch.delenv("CDESKTOP_SESSION_ID", raising=False)
    args = parser().parse_args(["--json", "approval", "approve", "approval-a"])

    assert args.func(args) == 0
    assert instances[0].responses == [("approval-a", "process-a", True, None)]
    assert '"status": "responded"' in capsys.readouterr().out


def test_primary_session_id_reads_execution_process() -> None:
    assert (
        _primary_session_id({"execution_process": {"session_id": "session-a"}})
        == "session-a"
    )


def test_repowire_status_requires_a_responding_daemon() -> None:
    assert _repowire_status_ok(0, "Daemon responding at http://127.0.0.1:8377")
    assert not _repowire_status_ok(0, "Daemon error at http://127.0.0.1:8377")
    assert not _repowire_status_ok(1, "Daemon responding at http://127.0.0.1:8377")


class FakeSpawnClient:
    def __init__(self, _url=None) -> None:
        self.stopped = []
        self.archived = []
        self.restored = []
        self.deleted = []
        self.dirty = []
        self.workspace_data = {
            "id": "workspace-a",
            "container_ref": None,
            "use_worktree": False,
        }
        self.last_spawn = None

    def spawn_workspace(self, **kwargs):
        self.last_spawn = kwargs
        return {
            "workspace": dict(self.workspace_data),
            "sessions": [{"id": "session-a"}],
        }

    def workspaces(self):
        return []

    def workspace(self, workspace_id):
        assert workspace_id == "workspace-a"
        return dict(self.workspace_data)

    def stop_workspace(self, workspace_id):
        self.stopped.append(workspace_id)

    def archive_workspace(self, workspace_id):
        self.archived.append(workspace_id)
        return {"id": workspace_id, "archived": True}

    def restore_workspace(self, workspace_id):
        self.restored.append(workspace_id)
        return {"id": workspace_id, "archived": False}

    def delete_workspace(self, workspace_id):
        self.deleted.append(workspace_id)

    def dirty_repositories(self, workspace_id):
        assert workspace_id == "workspace-a"
        return list(self.dirty)

    def sessions(self, _workspace_id):
        return [{"id": "session-a", "created_at": "2026-08-12T00:00:00Z"}]

    def workspace_repos(self, _workspace_id):
        return []

    def providers(self):
        return []

    def session_commands(self, _session_id):
        return []

    def interrupt_command(self, command_id):
        self.interrupted_commands = getattr(self, "interrupted_commands", [])
        self.interrupted_commands.append(command_id)

    def send(self, session_id, prompt, sender=None, *, dedupe_key=None, intent="continue"):
        self.sent = getattr(self, "sent", [])
        self.sent.append((session_id, prompt, dedupe_key, intent))
        return {"queued": True}


def test_prompt_idle_sends_only_when_not_running(monkeypatch, capsys) -> None:
    class IdleClient(FakeSpawnClient):
        def workspaces(self):
            return [
                {
                    "id": "workspace-a",
                    "name": "worker-a",
                    "branch": "feature-a",
                    "archived": False,
                }
            ]

        def workspace_summaries(self, archived=False):
            assert archived is False
            return [
                {
                    "workspace_id": "workspace-a",
                    "latest_process_status": "completed",
                    "has_pending_approval": False,
                }
            ]

        def send(self, session_id, prompt, sender_session=None):
            return {
                "session_id": session_id,
                "prompt": prompt,
                "sender": sender_session,
            }

        def execution_processes(self, session_id):
            assert session_id == "session-a"
            return [
                {
                    "id": "process-a",
                    "status": "completed",
                    "run_reason": "codingagent",
                    "dropped": False,
                }
            ]

        def pending_approvals(self):
            return []

    monkeypatch.setattr(cli, "CdesktopClient", IdleClient)
    args = argparse.Namespace(
        session_id="session-a",
        message="continue",
        message_file=None,
        sender_session="manager",
        url=None,
        json=True,
    )

    assert cli.cmd_prompt_idle(args) == 0
    assert '"verified_idle": true' in capsys.readouterr().out


def test_message_creates_a_durable_expectation_and_queues_continue(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    class MessageClient(FakeSpawnClient):
        def workspaces(self):
            return [
                {
                    "id": "workspace-a",
                    "name": "worker-a",
                    "branch": "feature-a",
                    "archived": False,
                }
            ]

        def workspace_summaries(self, archived=False):
            return [{"workspace_id": "workspace-a", "latest_process_status": "running"}]

        def send(
            self, session_id, prompt, sender_session=None, *, dedupe_key=None, intent="continue"
        ):
            return {"session_id": session_id, "prompt": prompt, "intent": intent}

    monkeypatch.setattr(cli, "CdesktopClient", MessageClient)
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: tmp_path / "orders.sqlite3")
    args = argparse.Namespace(
        session_id="session-a",
        message="finish the review",
        message_file=None,
        sender_session="manager",
        url=None,
        json=True,
    )

    assert cli.cmd_message(args) == 0
    assert '"intent": "continue"' in capsys.readouterr().out
    orders = escalation.EscalationStore(tmp_path / "orders.sqlite3").orders()
    assert len(orders) == 1
    assert orders[0].sender_session_id == "manager"
    assert orders[0].recipient_session_id == "session-a"


def test_message_no_expect_ack_creates_no_expectation(monkeypatch, tmp_path: Path) -> None:
    class MessageClient(FakeSpawnClient):
        def workspaces(self):
            return [{"id": "workspace-a", "name": "worker-a", "branch": "x", "archived": False}]

        def workspace_summaries(self, archived=False):
            return [{"workspace_id": "workspace-a", "latest_process_status": "running"}]

        def send(self, session_id, prompt, sender_session=None, *, dedupe_key=None, intent="continue"):
            return {"session_id": session_id}

    monkeypatch.setattr(cli, "CdesktopClient", MessageClient)
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: tmp_path / "orders.sqlite3")
    args = argparse.Namespace(
        session_id="session-a", message="finish", message_file=None,
        sender_session="manager", no_expect_ack=True, url=None, json=True,
    )

    assert cli.cmd_message(args) == 0
    assert escalation.EscalationStore(tmp_path / "orders.sqlite3").orders() == []


def test_prompt_idle_refuses_running_agent(monkeypatch) -> None:
    class RunningClient(FakeSpawnClient):
        def workspaces(self):
            return [
                {
                    "id": "workspace-a",
                    "name": "worker-a",
                    "branch": "feature-a",
                    "archived": False,
                }
            ]

        def workspace_summaries(self, archived=False):
            return [{"workspace_id": "workspace-a", "latest_process_status": "running"}]

        def execution_processes(self, _session_id):
            return [
                {
                    "id": "process-a",
                    "status": "running",
                    "run_reason": "codingagent",
                    "dropped": False,
                }
            ]

    monkeypatch.setattr(cli, "CdesktopClient", RunningClient)
    args = argparse.Namespace(
        session_id="session-a",
        message="continue",
        message_file=None,
        sender_session=None,
        url=None,
        json=True,
    )

    with pytest.raises(ValueError, match="running"):
        cli.cmd_prompt_idle(args)


def test_steer_uses_native_replace_intent(monkeypatch, capsys) -> None:
    instances = []

    class RunningClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            instances.append(self)

        def workspaces(self):
            return [
                {
                    "id": "workspace-a",
                    "name": "worker-a",
                    "branch": "feature-a",
                    "archived": False,
                }
            ]

        def workspace_summaries(self, archived=False):
            return [
                {
                    "workspace_id": "workspace-a",
                    "latest_process_status": "running",
                    "has_pending_approval": False,
                }
            ]

        def execution_processes(self, session_id):
            assert session_id == "session-a"
            return [
                {
                    "id": "process-a",
                    "status": "running",
                    "run_reason": "codingagent",
                    "dropped": False,
                }
            ]

        def send(self, session_id, prompt, sender_session=None, *, intent=None):
            return {"session_id": session_id, "prompt": prompt, "intent": intent}

    monkeypatch.setattr(cli, "CdesktopClient", RunningClient)
    args = argparse.Namespace(
        session_id="session-a",
        message="change direction",
        message_file=None,
        sender_session="manager",
        url=None,
        json=True,
    )
    assert cli.cmd_steer(args) == 0
    assert instances[0].stopped == []
    output = capsys.readouterr().out
    assert '"scope": "selected session only"' in output
    assert '"process-a"' in output
    assert '"session_id": "session-a"' in output
    assert '"intent": "replace"' in output


def test_failover_starts_visible_successor_on_approved_profile(
    monkeypatch, tmp_path, capsys
) -> None:
    profile_store = ProfileStore(tmp_path / "profiles.json")
    profile_store.set(
        Profile(
            name="claude-api",
            executor="CLAUDE_CODE",
            provider_id="provider-a",
            credential_kind="api",
            automatic_failover=True,
        )
    )

    class FailoverClient(FakeSpawnClient):
        spawned = None

        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data = {"id": "workspace-a", "archived": False}

        def providers(self):
            return [
                {
                    "id": "provider-a",
                    "enabled": True,
                    "kind": "Custom",
                    "perAgentEnabled": {"CLAUDE_CODE": True},
                }
            ]

        def spawn_teammate(self, **kwargs):
            type(self).spawned = kwargs
            return {"session": {"id": "session-b"}}

        def session_commands(self, session_id):
            assert session_id == "session-a"
            return [
                {
                    "id": "command-1",
                    "session_id": session_id,
                    "body": "finish the review",
                    "state": "pending",
                    "dedupe_key": "logical-1",
                }
            ]

    monkeypatch.setattr(cli, "CdesktopClient", FailoverClient)
    monkeypatch.setattr(cli, "ProfileStore", lambda: profile_store)
    args = argparse.Namespace(
        workspace_id="workspace-a",
        profile_name="claude-api",
        checkpoint="resume tests",
        checkpoint_file=None,
        name=None,
        unattended=True,
        new_worktree=False,
        archive_source=False,
        confirm_reconciled=False,
        no_bridge=False,
        lease_ttl_seconds=60,
        url=None,
        json=True,
    )

    assert cli.cmd_failover(args) == 0
    assert FailoverClient.spawned["caller_session"] == "session-a"
    assert FailoverClient.spawned["provider_id"] == "provider-a"
    assert FailoverClient.spawned["permission_policy"] == "BYPASS_PERMISSIONS"
    assert '"action": "visible-successor-started"' in capsys.readouterr().out

    # The superseded source is quarantined: its open command moved to the
    # successor as the same logical command and later delivery is rejected.
    ownership = succession.OwnershipStore()
    record = ownership.get("session-a")
    assert record is not None and record.successor_session_id == "session-b"
    with pytest.raises(succession.QuarantinedSessionError):
        ownership.assert_deliverable("session-a")


def test_spawn_direct_acquires_workspace_lease(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "CdesktopClient", FakeSpawnClient)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli.leases, "sync_active_workspaces", lambda _client: [])

    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=False,
        json=True,
    )

    assert cli.cmd_spawn(args) == 0

    output = json.loads(capsys.readouterr().out)
    assert "token" not in output["lease"]
    assert output["lease"]["workspace_id"] == "workspace-a"

    leases = LeaseStore(lease_dir).list()
    assert len(leases) == 1
    assert leases[0].repo_path == str(repo.resolve())
    assert leases[0].worktree_path is None
    assert leases[0].workspace_id == "workspace-a"
    assert leases[0].session_id == "session-a"


def test_lease_capability_boundary_redacts_inspection(tmp_path: Path, capsys) -> None:
    store = LeaseStore(tmp_path / "leases")
    lease = store.acquire("owner", tmp_path, ttl_seconds=60)
    args = argparse.Namespace(
        lease_dir=str(tmp_path / "leases"),
        lease_action="list",
        include_stale=True,
        json=True,
    )

    assert cli.cmd_lease(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert "token" not in output[0]
    assert lease.token not in json.dumps(output)


def test_status_redacts_lease_capability(monkeypatch, tmp_path: Path, capsys) -> None:
    lease_dir = tmp_path / "leases"
    lease = LeaseStore(lease_dir).acquire("owner", tmp_path, ttl_seconds=60)
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli.service, "status", lambda _port: {"running": True})
    monkeypatch.setattr(cli.routing, "enabled_workspaces", lambda: set())
    monkeypatch.setattr(cli.ProfileStore, "list", lambda _self: [])

    class StatusClient:
        def __init__(self, _url=None) -> None:
            pass

        def workspace_summaries(self, _archived=False):
            return []

        def workspaces(self):
            return []

        def providers(self):
            return []

    monkeypatch.setattr(cli, "CdesktopClient", StatusClient)
    args = argparse.Namespace(url=None, port=8377, include_archived=False, json=True)

    assert cli.cmd_status(args) == 0
    assert lease.token not in capsys.readouterr().out


def test_overview_groups_native_processes_and_projects_private_fields(
    monkeypatch, capsys
) -> None:
    class OverviewClient:
        def __init__(self, _url=None) -> None:
            self.snapshots = []

        def workspaces(self):
            return [{"id": "workspace-a", "branch": "feature", "archived": False}]

        def workspace_summaries(self, archived=False):
            assert archived is False
            return [{"workspace_id": "workspace-a"}]

        def sessions(self, _workspace_id):
            return [
                {"id": "session-running", "created_at": "1"},
                {"id": "session-done", "created_at": "2"},
                {"id": "session-failed", "created_at": "3"},
                {"id": "session-historical", "created_at": "4"},
            ]

        def execution_processes(self, session_id):
            return {
                "session-running": [
                    {
                        "id": "historical-failed",
                        "status": "failed",
                        "completed_at": "2019-08-18T02:00:00Z",
                    },
                    {
                        "id": "running-a",
                        "status": "running",
                        "started_at": "2020-01-01T00:00:00Z",
                        "executor_action": {
                            "selected_model_id": "gpt-authoritative",
                            "selected_provider_id": "provider-a",
                        },
                        "secret": "raw-capability-token",
                    },
                ],
                "session-done": [
                    {
                        "id": "done-a",
                        "status": "completed",
                        "completed_at": "2099-08-18T01:00:00Z",
                    }
                ],
                "session-failed": [
                    {
                        "id": "failed-a",
                        "status": "failed",
                        "completed_at": "2099-08-18T02:00:00Z",
                    }
                ],
                "session-historical": [
                    {
                        "id": "historical-failed",
                        "status": "failed",
                        "completed_at": "2020-08-18T02:00:00Z",
                    }
                ],
            }[session_id]

        def providers(self):
            return [{"id": "provider-a", "kind": "codex"}]

        def normalized_snapshot(self, process_id):
            self.snapshots.append(process_id)
            return {
                "complete": True,
                "entries": [
                    {
                        "content": {
                            "entry_type": {
                                "type": "token_usage_info",
                                "total_tokens": 120,
                                "model_context_window": 1000,
                                "secret": "snapshot-secret",
                            }
                        }
                    }
                ],
            }

        def pending_approvals(self):
            return []

    client = OverviewClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    args = argparse.Namespace(url=None, since=None, json=True)

    assert cli.cmd_overview(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert [item["execution_id"] for item in output["needs_attention"]] == ["failed-a"]
    assert [item["execution_id"] for item in output["running"]] == ["running-a"]
    assert [item["execution_id"] for item in output["done_since_view"]] == ["done-a"]
    running = output["running"][0]
    assert running["model"] == "gpt-authoritative"
    assert running["selector"].endswith("/session-running")
    assert running["provider"] == "codex"
    assert running["account_id"] is None
    assert running["token_usage"] == {
        "total": 120,
        "unit": "tokens",
        "provenance": "cdesktop normalized snapshot",
    }
    assert running["context"] == {"used": 120, "limit": 1000, "pressure": 0.12}
    assert running["quota"] is None
    assert running["monetary_cost"] is None
    assert "historical-failed" not in json.dumps(output)
    assert "raw-capability-token" not in json.dumps(output)
    assert "snapshot-secret" not in json.dumps(output)
    assert client.snapshots == [
        "running-a",
        "done-a",
        "failed-a",
    ]
    selectors = [item["selector"] for group in output.values() for item in group]
    assert len(selectors) == len(set(selectors))

    args.since = "2019-01-01T00:00:00Z"
    assert cli.cmd_overview(args) == 0
    expanded = json.loads(capsys.readouterr().out)
    assert {item["execution_id"] for item in expanded["needs_attention"]} == {
        "failed-a",
        "historical-failed",
    }
    assert client.snapshots[-1] == "historical-failed"

    args.since = None
    args.json = False
    assert cli.cmd_overview(args) == 0
    default_output = capsys.readouterr().out
    assert "Needs attention\n" in default_output
    assert "Running\n" in default_output
    assert "Done since view\n" in default_output
    assert "Next:" in default_output
    assert "raw-capability-token" not in default_output


def test_spawn_records_automatic_parent(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli.leases, "sync_active_workspaces", lambda _client: [])
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.setenv("CDESKTOP_SESSION_ID", "parent-session")
    parents = []

    class ParentClient(FakeSpawnClient):
        def workspaces(self):
            return [
                {
                    "id": "parent-workspace",
                    "name": "parent",
                    "archived": False,
                }
            ]

        def workspace_summaries(self, _archived=False):
            return []

        def sessions(self, workspace_id):
            assert workspace_id == "parent-workspace"
            return [
                {
                    "id": "parent-session",
                    "name": "lead",
                    "created_at": "2026-08-13T00:00:00Z",
                }
            ]

        def set_parent(self, session_id, parent_session_id):
            parents.append((session_id, parent_session_id))
            return {"id": session_id, "parent_session_id": parent_session_id}

    monkeypatch.setattr(cli, "CdesktopClient", ParentClient)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="child",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        parent_session=None,
        lease_ttl_seconds=60,
        no_bridge=False,
        json=True,
    )

    assert cli.cmd_spawn(args) == 0

    assert parents == [("session-a", "parent-session")]


def _spawn_args(repo: Path, **overrides) -> argparse.Namespace:
    base = dict(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="child",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        parent_session=None,
        lease_ttl_seconds=60,
        no_bridge=False,
        json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_spawn_captures_external_launcher_identity_and_it_survives_restart(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    escalation_db = tmp_path / "escalations.sqlite3"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli.leases, "sync_active_workspaces", lambda _client: [])
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: escalation_db)
    monkeypatch.delenv("CDESKTOP_SESSION_ID", raising=False)
    monkeypatch.setenv("CONDUCTOR_WORKSPACE_NAME", "my-workspace")
    monkeypatch.setattr(cli, "CdesktopClient", FakeSpawnClient)

    assert cli.cmd_spawn(_spawn_args(repo)) == 0

    identity = escalation.EscalationStore(escalation_db).get_launcher("session-a")
    assert identity == escalation.LauncherIdentity(launcher="external", detail="conductor")

    reopened = escalation.EscalationStore(escalation_db).get_launcher("session-a")
    assert reopened == identity


def test_spawn_records_cdesktop_launcher_when_run_inside_a_cdesktop_session(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    escalation_db = tmp_path / "escalations.sqlite3"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli.leases, "sync_active_workspaces", lambda _client: [])
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: escalation_db)
    monkeypatch.setenv("CDESKTOP_SESSION_ID", "some-cdesktop-session")

    class CdesktopLaunchedClient(FakeSpawnClient):
        def workspaces(self):
            return [{"id": "parent-workspace", "name": "parent", "archived": False}]

        def workspace_summaries(self, _archived=False):
            return []

        def sessions(self, workspace_id):
            return [
                {
                    "id": "some-cdesktop-session",
                    "name": "lead",
                    "created_at": "2026-08-13T00:00:00Z",
                }
            ]

        def set_parent(self, session_id, parent_session_id):
            return {"id": session_id, "parent_session_id": parent_session_id}

    monkeypatch.setattr(cli, "CdesktopClient", CdesktopLaunchedClient)

    assert cli.cmd_spawn(_spawn_args(repo)) == 0

    identity = escalation.EscalationStore(escalation_db).get_launcher("session-a")
    assert identity == escalation.LauncherIdentity(launcher="cdesktop", detail=None)


def test_spawn_worktree_acquires_container_lease(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    container = tmp_path / "container"
    repo.mkdir()
    (container / repo.name).mkdir(parents=True)
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)

    class WorktreeClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data = {
                "id": "workspace-a",
                "container_ref": str(container),
                "use_worktree": True,
            }

    monkeypatch.setattr(cli, "CdesktopClient", WorktreeClient)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=True,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=False,
        json=True,
    )

    assert cli.cmd_spawn(args) == 0

    lease = LeaseStore(lease_dir).list()[0]
    assert lease.repo_path == str(repo.resolve())
    assert lease.worktree_path == str((container / repo.name).resolve())
    assert lease.workspace_id == "workspace-a"


def _worktree_spawn_args(repo: Path) -> argparse.Namespace:
    return argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=True,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=False,
        json=True,
    )


def _commit_worktree_fixture(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_worktree_spawn_configures_repository_setup_hook(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    settings = repo / ".conductor" / "settings.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('[scripts]\nsetup = "bun install --frozen-lockfile"\n')
    _commit_worktree_fixture(repo)
    container = tmp_path / "container"
    (container / repo.name).mkdir(parents=True)
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.delenv("CDESKTOP_SESSION_ID", raising=False)

    class Client(FakeSpawnClient):
        instance = None

        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            type(self).instance = self
            self.workspace_data.update(container_ref=str(container), use_worktree=True)

    monkeypatch.setattr(cli, "CdesktopClient", Client)
    assert cli.cmd_spawn(_worktree_spawn_args(repo)) == 0
    assert Client.instance.last_spawn["setup_script"] == "bun install --frozen-lockfile"


def test_worktree_spawn_without_setup_hook_is_a_noop(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _commit_worktree_fixture(repo)
    container = tmp_path / "container"
    (container / repo.name).mkdir(parents=True)
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(cli.routing, "enable", lambda _workspace_id: None)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.delenv("CDESKTOP_SESSION_ID", raising=False)

    class Client(FakeSpawnClient):
        instance = None

        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            type(self).instance = self
            self.workspace_data.update(container_ref=str(container), use_worktree=True)

    monkeypatch.setattr(cli, "CdesktopClient", Client)
    assert cli.cmd_spawn(_worktree_spawn_args(repo)) == 0
    assert Client.instance.last_spawn["setup_script"] is None


def test_worktree_setup_failure_propagates_without_a_lease(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    settings = repo / ".conductor" / "settings.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('[scripts]\nsetup = "false"\n')
    _commit_worktree_fixture(repo)
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.delenv("CDESKTOP_SESSION_ID", raising=False)

    class Client(FakeSpawnClient):
        def spawn_workspace(self, **kwargs):
            assert kwargs["setup_script"] == "false"
            raise RuntimeError("setup script failed")

    monkeypatch.setattr(cli, "CdesktopClient", Client)
    with pytest.raises(RuntimeError, match="setup script failed"):
        cli.cmd_spawn(_worktree_spawn_args(repo))
    assert LeaseStore(lease_dir).list() == []


def test_close_archive_releases_only_workspace_lease(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    lease_dir = tmp_path / "leases"
    store = LeaseStore(lease_dir)
    archived_lease = store.acquire(
        "owner-a", repo_a, ttl_seconds=60, workspace_id="workspace-a"
    )
    other = store.acquire("owner-b", repo_b, ttl_seconds=60, workspace_id="workspace-b")
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "CdesktopClient", FakeSpawnClient)
    monkeypatch.setattr(cli.routing, "disable", lambda _workspace_id: None)

    args = argparse.Namespace(
        workspace_id="workspace-a",
        url=None,
        archive=True,
        confirm_reconciled=True,
        preserve_dirty=False,
        json=True,
        message=None,
        message_file=None,
        sender_session=None,
    )

    assert cli.cmd_close(args) == 0
    assert archived_lease.token not in capsys.readouterr().out

    remaining = LeaseStore(lease_dir).list()
    assert [lease.token for lease in remaining] == [other.token]


def test_archive_refuses_dirty_managed_worktree_even_when_preserve_requested(
    monkeypatch, tmp_path: Path
) -> None:
    class DirtyWorktreeClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data["use_worktree"] = True
            self.dirty = [{"path": str(tmp_path), "status": "M file.txt"}]

    monkeypatch.setattr(cli, "CdesktopClient", DirtyWorktreeClient)
    args = parser().parse_args(
        [
            "workspace",
            "archive",
            "workspace-a",
            "--confirm-reconciled",
            "--preserve-dirty",
        ]
    )

    with pytest.raises(ValueError, match="dirty managed worktree"):
        args.func(args)


def test_workspace_delete_requires_archived_and_preserves_branch(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli.routing, "disable", lambda _workspace_id: None)

    class ArchivedClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data["archived"] = True

    client = ArchivedClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    args = parser().parse_args(
        ["--json", "workspace", "delete", "workspace-a", "--confirm-delete"]
    )

    assert args.func(args) == 0
    assert client.deleted == ["workspace-a"]
    assert '"branch_preserved": true' in capsys.readouterr().out


def test_workspace_delete_requires_extra_confirmation_for_missing_direct_repo(
    monkeypatch,
) -> None:
    class MissingRepoClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data["archived"] = True
            self.dirty = [
                {"path": "/missing/repo", "status": "repository path is missing"}
            ]

    client = MissingRepoClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    args = parser().parse_args(
        ["workspace", "delete", "workspace-a", "--confirm-delete"]
    )
    with pytest.raises(ValueError, match="--allow-missing-repo"):
        args.func(args)

    monkeypatch.setattr(cli.routing, "disable", lambda _workspace_id: None)
    args = parser().parse_args(
        [
            "workspace",
            "delete",
            "workspace-a",
            "--confirm-delete",
            "--allow-missing-repo",
        ]
    )
    assert args.func(args) == 0
    assert client.deleted == ["workspace-a"]


def test_workspace_delete_can_preserve_dirty_direct_repo(monkeypatch) -> None:
    class DirtyDirectClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data["archived"] = True
            self.dirty = [{"path": "/repo", "status": "?? marker.txt"}]

    client = DirtyDirectClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    monkeypatch.setattr(cli.routing, "disable", lambda _workspace_id: None)
    args = parser().parse_args(
        [
            "workspace",
            "delete",
            "workspace-a",
            "--confirm-delete",
            "--preserve-dirty",
        ]
    )

    assert args.func(args) == 0
    assert client.deleted == ["workspace-a"]


def test_workspace_restore_reactivates_route_and_lease(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    enabled = []
    monkeypatch.setattr(cli.routing, "enable", enabled.append)

    class ArchivedClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data["archived"] = True

        def workspaces(self):
            return [{**self.workspace_data, "archived": False}]

        def workspace_repos(self, _workspace_id):
            return [{"path": str(repo), "name": repo.name}]

    client = ArchivedClient()
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    args = parser().parse_args(["workspace", "restore", "workspace-a"])

    assert args.func(args) == 0
    assert client.restored == ["workspace-a"]
    assert enabled == ["workspace-a"]
    lease = LeaseStore(lease_dir).list()[0]
    assert lease.workspace_id == "workspace-a"
    assert lease.token not in capsys.readouterr().out


def test_spawn_direct_fails_closed_when_repo_leased(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    LeaseStore(lease_dir).acquire("other", repo, ttl_seconds=60)
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)

    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )

    with pytest.raises(cli.leases.LeaseError):
        cli.cmd_spawn(args)


def test_spawn_direct_releases_pending_lease_when_cdesktop_start_fails(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease_dir = tmp_path / "leases"
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)

    class FailingClient(FakeSpawnClient):
        def spawn_workspace(self, **_kwargs):
            raise RuntimeError("start failed")

    monkeypatch.setattr(cli, "CdesktopClient", FailingClient)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission="SUPERVISED",
        unattended=False,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )

    with pytest.raises(RuntimeError, match="start failed"):
        cli.cmd_spawn(args)

    assert LeaseStore(lease_dir).list() == []


def test_validate_base_branch_rejects_raw_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    cli._validate_base_branch(repo, "main")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pytest.raises(ValueError, match="raw commit"):
        cli._validate_base_branch(repo, head)


def test_unattended_requires_worktree(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=False,
        permission=None,
        unattended=True,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )
    with pytest.raises(ValueError, match="requires --worktree"):
        cli.cmd_spawn(args)


def test_unattended_worktree_selects_bypass(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    container = tmp_path / "container"
    repo.mkdir()
    (container / repo.name).mkdir(parents=True)
    lease_dir = tmp_path / "leases"
    instances = []

    class WorktreeClient(FakeSpawnClient):
        def __init__(self, _url=None) -> None:
            super().__init__(_url)
            self.workspace_data["container_ref"] = str(container)
            self.workspace_data["use_worktree"] = True
            instances.append(self)

    monkeypatch.setattr(cli, "CdesktopClient", WorktreeClient)
    monkeypatch.setattr(cli, "_validate_base_branch", lambda *_args: None)
    monkeypatch.setattr(cli.leases, "default_lease_dir", lambda: lease_dir)
    args = argparse.Namespace(
        prompt="start",
        prompt_file=None,
        repo=str(repo),
        url=None,
        name="demo",
        base="main",
        executor="CODEX",
        worktree=True,
        permission=None,
        unattended=True,
        model=None,
        reasoning=None,
        provider=None,
        lease_ttl_seconds=60,
        no_bridge=True,
        json=True,
    )
    assert cli.cmd_spawn(args) == 0
    assert instances[0].last_spawn["permission_policy"] == "BYPASS_PERMISSIONS"


class ParentTestClient:
    def __init__(self, *, sessions, workspaces):
        self.sessions_by_id = sessions
        self.workspaces_by_id = workspaces
        self.sent = []

    def session(self, session_id):
        return self.sessions_by_id[session_id]

    def workspace(self, workspace_id):
        return self.workspaces_by_id[workspace_id]

    def send(self, session_id, prompt, sender_session=None, *, dedupe_key=None, intent="continue"):
        self.sent.append({"session_id": session_id, "prompt": prompt})
        return {"ok": True}


def _parent_args(**overrides) -> argparse.Namespace:
    base = dict(session="child-a", url=None, message=None, message_file=None, json=True)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_parent_raises_without_recorded_parent_and_no_message(monkeypatch) -> None:
    client = ParentTestClient(
        sessions={"child-a": {"id": "child-a", "workspace_id": "child-workspace"}},
        workspaces={},
    )
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)

    with pytest.raises(ValueError, match="No recorded parent"):
        cli.cmd_parent(_parent_args())


def test_cmd_parent_parks_escalation_durably_when_no_parent_exists(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    escalation_db = tmp_path / "escalations.sqlite3"
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: escalation_db)
    client = ParentTestClient(
        sessions={"child-a": {"id": "child-a", "workspace_id": "child-workspace"}},
        workspaces={},
    )
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)

    assert cli.cmd_parent(_parent_args(message="STATUS: blocked, no parent")) == 0

    assert client.sent == []
    out = capsys.readouterr().out
    assert '"delivered": false' in out
    assert '"reason": "no_parent"' in out
    pending = escalation.EscalationStore(escalation_db).pending()
    assert len(pending) == 1
    assert pending[0].child_session_id == "child-a"
    assert pending[0].reason == "no_parent"


def test_cmd_parent_never_delivers_into_an_archived_parent_session(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    escalation_db = tmp_path / "escalations.sqlite3"
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: escalation_db)
    client = ParentTestClient(
        sessions={
            "child-a": {
                "id": "child-a",
                "workspace_id": "child-workspace",
                "parent_session_id": "parent-a",
            },
            "parent-a": {"id": "parent-a", "workspace_id": "parent-workspace"},
        },
        workspaces={"parent-workspace": {"id": "parent-workspace", "archived": True}},
    )
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)

    assert cli.cmd_parent(_parent_args(message="STATUS: parent retired mid-flight")) == 0

    assert client.sent == [], "must never deliver into an archived/retired session"
    out = capsys.readouterr().out
    assert '"reason": "parent_archived"' in out
    pending = escalation.EscalationStore(escalation_db).pending()
    assert len(pending) == 1
    assert pending[0].reason == "parent_archived"
    assert pending[0].recorded_parent_session_id == "parent-a"


def test_cmd_parent_delivers_normally_when_parent_is_live(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    escalation_db = tmp_path / "escalations.sqlite3"
    monkeypatch.setattr(escalation, "escalation_db_path", lambda: escalation_db)
    client = ParentTestClient(
        sessions={
            "child-a": {
                "id": "child-a",
                "workspace_id": "child-workspace",
                "parent_session_id": "parent-a",
            },
            "parent-a": {"id": "parent-a", "workspace_id": "parent-workspace"},
        },
        workspaces={"parent-workspace": {"id": "parent-workspace", "archived": False}},
    )
    monkeypatch.setattr(cli, "CdesktopClient", lambda _url=None: client)
    escalation.EscalationStore(escalation_db).expect_order(
        order_id="order-1",
        sender_session_id="parent-a",
        recipient_session_id="child-a",
        body="Report your status",
    )

    assert cli.cmd_parent(_parent_args(message="STATUS: done")) == 0
    assert escalation.EscalationStore(escalation_db).orders()[0].satisfied_at is not None

    assert len(client.sent) == 1
    assert client.sent[0]["session_id"] == "parent-a"
    assert escalation.EscalationStore(escalation_db).pending() == []
