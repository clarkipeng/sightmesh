from __future__ import annotations

from .common import *
from .fleet import _compact_text, _normalized_snapshot_with_retry
from .messaging import _caller_session

def _approval_details(
    client: CdesktopClient, approval: dict[str, Any]
) -> dict[str, Any]:
    details = dict(approval)
    process = client.execution_process(str(approval["execution_process_id"]))
    session_id = str(process["session_id"])
    session = client.session(session_id)
    workspace_id = str(session["workspace_id"])
    workspace = client.workspace(workspace_id)
    tool_name = str(approval.get("tool_name") or "")
    request = None
    try:
        snapshot = _normalized_snapshot_with_retry(
            client, str(approval["execution_process_id"])
        )
        request = _pending_request_from_snapshot(snapshot, str(approval["approval_id"]))
    except CdesktopError:
        pass
    details.update(
        {
            "session_id": session_id,
            "session_name": session.get("name"),
            "executor": session.get("executor"),
            "workspace_id": workspace_id,
            "workspace_name": workspace.get("name"),
            "workspace_archived": bool(workspace.get("archived")),
            "request": request,
            "request_kind": (
                "question"
                if approval.get("is_question")
                else "plan"
                if tool_name == "ExitPlanMode"
                else "tool"
            ),
        }
    )
    return details


def _pending_request_from_snapshot(
    snapshot: dict[str, Any], approval_id: str
) -> dict[str, Any] | None:
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        return None
    for wrapped in reversed(entries):
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        entry_type = content.get("entry_type") if isinstance(content, dict) else None
        if not isinstance(entry_type, dict) or entry_type.get("type") != "tool_use":
            continue
        status = entry_type.get("status")
        if (
            not isinstance(status, dict)
            or status.get("status") != "pending_approval"
            or str(status.get("approval_id")) != approval_id
        ):
            continue
        action = entry_type.get("action_type")
        return {
            "summary": _compact_text(content.get("content"), 600),
            "action": action if isinstance(action, dict) else None,
        }
    return None


def _approval_response_template(approval: dict[str, Any]) -> dict[str, Any]:
    template: dict[str, Any] = {"approval_id": approval["approval_id"]}
    if approval.get("is_question"):
        action = (approval.get("request") or {}).get("action")
        questions = action.get("questions") if isinstance(action, dict) else None
        template["answers"] = (
            [""] * len(questions) if isinstance(questions, list) else []
        )
    else:
        template["decision"] = "approve|deny"
        if approval.get("request_kind") != "plan":
            template["allow_non_plan"] = False
    return template


def _approval_details_batch(
    client: CdesktopClient, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not items:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
        return list(pool.map(lambda item: _approval_details(client, item), items))


def cmd_inbox(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    fleet_rows = _fleet_sessions(client)
    selectors = {str(row["session_id"]): f"@{row['selector']}" for row in fleet_rows}
    rows = []
    for details in _approval_details_batch(client, client.pending_approvals()):
        details["agent"] = selectors.get(
            str(details["session_id"]), str(details["session_id"])
        )
        details["response_template"] = _approval_response_template(details)
        rows.append(details)
    rows.extend(_idle_unmet_orders(client, fleet_rows))
    _emit(rows, args.json)
    return 0


def _response_items(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Responses are not valid JSON: {exc}") from exc
    if isinstance(parsed, dict):
        parsed = parsed.get("responses")
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Responses must be a non-empty JSON array")
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError("Every batch response must be a JSON object")
    return [dict(item) for item in parsed]


def _question_items(approval: dict[str, Any]) -> list[dict[str, Any]]:
    action = (approval.get("request") or {}).get("action")
    questions = action.get("questions") if isinstance(action, dict) else None
    if not isinstance(questions, list) or not all(
        isinstance(question, dict) for question in questions
    ):
        raise ValueError(
            f"Question details are unavailable for approval {approval['approval_id']}"
        )
    return [dict(question) for question in questions]


def _structured_question_answers(
    approval: dict[str, Any], supplied: object
) -> list[dict[str, Any]]:
    questions = _question_items(approval)
    if not isinstance(supplied, list) or len(supplied) != len(questions):
        raise ValueError(
            f"Approval {approval['approval_id']} requires exactly "
            f"{len(questions)} ordered answers"
        )
    normalized = []
    for index, (question, answer_value) in enumerate(
        zip(questions, supplied, strict=True)
    ):
        expected_text = str(question.get("question") or "")
        if isinstance(answer_value, dict):
            supplied_question = answer_value.get("question")
            if (
                supplied_question is not None
                and str(supplied_question) != expected_text
            ):
                raise ValueError(
                    f"Approval {approval['approval_id']} answer {index + 1} "
                    "does not match its question"
                )
            answer_value = answer_value.get("answer")
        values = [answer_value] if isinstance(answer_value, str) else answer_value
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value.strip() for value in values)
        ):
            raise ValueError(
                f"Approval {approval['approval_id']} answer {index + 1} must "
                "contain one or more non-empty strings"
            )
        if not question.get("multiSelect") and len(values) != 1:
            raise ValueError(
                f"Approval {approval['approval_id']} answer {index + 1} is single-select"
            )
        normalized.append(
            {"question": expected_text, "answer": [value.strip() for value in values]}
        )
    return normalized


def _prepare_batch_responses(
    client: CdesktopClient,
    items: list[dict[str, Any]],
    reviewer_session: str | None,
) -> list[dict[str, Any]]:
    pending_items = client.pending_approvals()
    pending = {
        str(item["approval_id"]): item
        for item in _approval_details_batch(client, pending_items)
    }
    seen: set[str] = set()
    prepared = []
    for item in items:
        approval_id = str(item.get("approval_id") or "")
        if not approval_id or approval_id in seen:
            raise ValueError("Every batch item needs a unique approval_id")
        seen.add(approval_id)
        approval = pending.get(approval_id)
        if approval is None:
            raise ValueError(f"Approval is not currently pending: {approval_id}")
        reviewer_kind, reviewer_id = _approval_reviewer(
            client, reviewer_session, str(approval["session_id"])
        )
        if approval.get("is_question"):
            if "decision" in item:
                raise ValueError(
                    f"Question {approval_id} expects answers, not a decision"
                )
            prepared.append(
                {
                    "approval": approval,
                    "kind": "question",
                    "answers": _structured_question_answers(
                        approval, item.get("answers")
                    ),
                    "reviewer_kind": reviewer_kind,
                    "reviewer_id": reviewer_id,
                }
            )
            continue

        decision = str(item.get("decision") or "")
        if decision not in {"approve", "deny"}:
            raise ValueError(f"Approval {approval_id} decision must be approve or deny")
        if (
            decision == "approve"
            and approval["request_kind"] != "plan"
            and item.get("allow_non_plan") is not True
        ):
            raise ValueError(
                f"Approval {approval_id} is a non-plan tool request; set "
                '"allow_non_plan": true only after reviewing the exact action'
            )
        reason = item.get("reason")
        if decision == "deny" and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError(f"Approval {approval_id} denial requires a reason")
        prepared.append(
            {
                "approval": approval,
                "kind": "approval",
                "approved": decision == "approve",
                "reason": reason.strip() if isinstance(reason, str) else None,
                "reviewer_kind": reviewer_kind,
                "reviewer_id": reviewer_id,
            }
        )
    return prepared


def cmd_respond(args: argparse.Namespace) -> int:
    payload = _read_text(args.responses, args.responses_file, "responses")
    items = _response_items(payload)
    client = CdesktopClient(args.url)
    prepared = _prepare_batch_responses(client, items, args.reviewer_session)
    audit = approvals.ApprovalAuditStore()
    results = []
    failures = 0
    for item in prepared:
        approval = item["approval"]
        attempt = None
        try:
            if item["kind"] == "question":
                response = client.respond_to_question(
                    str(approval["approval_id"]),
                    str(approval["execution_process_id"]),
                    item["answers"],
                )
            else:
                attempt = audit.begin(
                    approval=approval,
                    decision="approved" if item["approved"] else "denied",
                    reviewer_kind=item["reviewer_kind"],
                    reviewer_id=item["reviewer_id"],
                    reason=item["reason"],
                )
                response = client.respond_to_approval(
                    str(approval["approval_id"]),
                    str(approval["execution_process_id"]),
                    approved=item["approved"],
                    reason=item["reason"],
                )
                audit.finish(attempt.decision_id, succeeded=True)
            results.append(
                {
                    "approval_id": approval["approval_id"],
                    "status": "responded",
                    "response": response,
                }
            )
        except (CdesktopError, approvals.ApprovalAuditError) as exc:
            failures += 1
            if attempt is not None:
                audit.finish(attempt.decision_id, succeeded=False, error=str(exc))
            results.append(
                {
                    "approval_id": approval["approval_id"],
                    "status": "failed",
                    "error": str(exc),
                }
            )
    reporter = args.reviewer_session or os.environ.get("CDESKTOP_SESSION_ID")
    if reporter and failures == 0:
        escalation.EscalationStore().satisfy_orders(reporter)
    _emit({"results": results, "failed": failures}, args.json)
    return int(failures > 0)


def _pending_approval(client: CdesktopClient, approval_id: str) -> dict[str, Any]:
    approval = next(
        (
            item
            for item in client.pending_approvals()
            if item.get("approval_id") == approval_id
        ),
        None,
    )
    if approval is None:
        raise ValueError(f"Approval is not currently pending: {approval_id}")
    return _approval_details(client, approval)


def _approval_reviewer(
    client: CdesktopClient,
    explicit_session: str | None,
    target_session_id: str,
) -> tuple[str, str]:
    reviewer_session_id = explicit_session or os.environ.get("CDESKTOP_SESSION_ID")
    if not reviewer_session_id:
        return "human", f"{getpass.getuser()}@local"
    if reviewer_session_id == target_session_id:
        raise ValueError("A session cannot approve its own plan")
    reviewer = client.session(reviewer_session_id)
    reviewer_workspace_id = str(reviewer["workspace_id"])
    sessions = sorted(
        client.sessions(reviewer_workspace_id),
        key=lambda item: (str(item.get("created_at") or ""), str(item["id"])),
    )
    if not sessions or str(sessions[0]["id"]) != reviewer_session_id:
        raise ValueError(
            "Only the lead session in a cdesktop workspace may approve another "
            "agent's plan"
        )
    return "session", reviewer_session_id


def cmd_approval(args: argparse.Namespace) -> int:
    client = CdesktopClient(args.url)
    if args.approval_action == "history":
        records = [
            record.to_dict()
            for record in approvals.ApprovalAuditStore().history(limit=args.limit)
        ]
        _emit(records, args.json)
        return 0

    if args.approval_action == "list":
        rows = [_approval_details(client, item) for item in client.pending_approvals()]
        if args.workspace_id:
            rows = [item for item in rows if item["workspace_id"] == args.workspace_id]
        if args.session_id:
            rows = [item for item in rows if item["session_id"] == args.session_id]
        _emit(rows, args.json)
        return 0

    approval = _pending_approval(client, args.approval_id)
    if args.approval_action == "show":
        _emit(approval, args.json)
        return 0
    if approval.get("is_question"):
        raise ValueError(
            "This request expects structured answers, not plan approval; answer it in "
            "the visible cdesktop session"
        )

    approved = args.approval_action == "approve"
    if approved and approval["request_kind"] != "plan" and not args.allow_non_plan:
        raise ValueError(
            "Refusing to approve a non-plan tool request. Review it in cdesktop or "
            "repeat with --allow-non-plan after verifying the exact tool action."
        )
    reason = None
    if not approved:
        reason = _read_text(args.reason, args.reason_file, "reason")
        if not reason.strip():
            raise ValueError("Rejection reason must not be empty")
    reviewer_kind, reviewer_id = _approval_reviewer(
        client,
        args.reviewer_session,
        str(approval["session_id"]),
    )
    store = approvals.ApprovalAuditStore()
    attempt = store.begin(
        approval=approval,
        decision="approved" if approved else "denied",
        reviewer_kind=reviewer_kind,
        reviewer_id=reviewer_id,
        reason=reason,
    )
    try:
        response = client.respond_to_approval(
            str(approval["approval_id"]),
            str(approval["execution_process_id"]),
            approved=approved,
            reason=reason,
        )
    except Exception as exc:
        store.finish(attempt.decision_id, succeeded=False, error=str(exc))
        raise
    completed = store.finish(attempt.decision_id, succeeded=True)
    _emit(
        {
            "approval": approval,
            "decision": completed.to_dict(),
            "cdesktop_response": response,
        },
        args.json,
    )
    return 0



def add_inbox_parser(sub: argparse._SubParsersAction[Any]) -> None:
    inbox = sub.add_parser(
        "inbox", help="Show every pending agent question, plan, and tool request"
    )
    inbox.set_defaults(func=cmd_inbox)

    respond = sub.add_parser(
        "respond", help="Prevalidate and answer multiple pending requests in one call"
    )
    response_source = respond.add_mutually_exclusive_group(required=True)
    response_source.add_argument("--responses", help="Inline JSON response array")
    response_source.add_argument(
        "--responses-file", help="Path to a JSON response file"
    )
    respond.add_argument(
        "--reviewer-session",
        help="Lead session responding; defaults to CDESKTOP_SESSION_ID",
    )
    respond.set_defaults(func=cmd_respond)




def add_parser(sub: argparse._SubParsersAction[Any]) -> None:
    approval = sub.add_parser(
        "approval", help="Review and respond to visible cdesktop plan approvals"
    )
    approval_sub = approval.add_subparsers(dest="approval_action", required=True)
    approval_list = approval_sub.add_parser(
        "list", help="List currently pending cdesktop approvals"
    )
    approval_list.add_argument("--workspace-id")
    approval_list.add_argument("--session-id")
    approval_list.set_defaults(func=cmd_approval)
    approval_show = approval_sub.add_parser(
        "show", help="Show one currently pending approval with workspace context"
    )
    approval_show.add_argument("approval_id")
    approval_show.set_defaults(func=cmd_approval)
    approval_approve = approval_sub.add_parser(
        "approve", help="Approve a reviewed plan and continue its visible session"
    )
    approval_approve.add_argument("approval_id")
    approval_approve.add_argument(
        "--reviewer-session",
        help="Lead cdesktop session performing the review; defaults to CDESKTOP_SESSION_ID",
    )
    approval_approve.add_argument(
        "--allow-non-plan",
        action="store_true",
        help="Explicitly allow a reviewed non-plan tool request",
    )
    approval_approve.set_defaults(func=cmd_approval)
    approval_reject = approval_sub.add_parser(
        "reject", help="Reject a pending approval with actionable feedback"
    )
    approval_reject.add_argument("approval_id")
    rejection_reason = approval_reject.add_mutually_exclusive_group(required=True)
    rejection_reason.add_argument("--reason")
    rejection_reason.add_argument("--reason-file")
    approval_reject.add_argument(
        "--reviewer-session",
        help="Lead cdesktop session performing the review; defaults to CDESKTOP_SESSION_ID",
    )
    approval_reject.set_defaults(func=cmd_approval, allow_non_plan=False)
    approval_history = approval_sub.add_parser(
        "history", help="Show the private local approval decision audit"
    )
    approval_history.add_argument("--limit", type=int, default=50)
    approval_history.set_defaults(func=cmd_approval)
