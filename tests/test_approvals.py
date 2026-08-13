import sqlite3

from sightmesh.approvals import ApprovalAuditStore


def test_approval_audit_records_metadata_without_reason_text(tmp_path) -> None:
    path = tmp_path / "approvals.sqlite3"
    store = ApprovalAuditStore(path)
    attempt = store.begin(
        approval={
            "approval_id": "approval-a",
            "execution_process_id": "process-a",
            "session_id": "session-a",
            "workspace_id": "workspace-a",
            "tool_name": "ExitPlanMode",
        },
        decision="denied",
        reviewer_kind="session",
        reviewer_id="manager-session",
        reason="Revise the validation section.",
    )
    completed = store.finish(attempt.decision_id, succeeded=True)

    assert completed.status == "responded"
    assert completed.reason_sha256
    assert store.history()[0] == completed
    raw = (
        sqlite3.connect(path)
        .execute("SELECT reason_sha256 FROM approval_decisions")
        .fetchone()
    )
    assert raw and raw[0] == completed.reason_sha256
    assert "Revise the validation section." not in path.read_bytes().decode(
        "utf-8", errors="ignore"
    )
