from datetime import UTC, datetime, timedelta

from sightmesh.fleet import FleetFacts, overview

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def execution(identifier: str, **extra):
    return {"id": identifier, "workspace_id": "ws", "status": "running", **extra}


def test_groups_deterministically_and_orders_attention_by_urgency_then_age():
    result = overview(
        FleetFacts(
            executions=(
                execution("running"),
                execution(
                    "blocked",
                    status="blocked",
                    last_event={"at": NOW - timedelta(minutes=2)},
                ),
                execution("approval", last_event={"at": NOW - timedelta(hours=3)}),
                execution(
                    "done",
                    status="completed",
                    last_event={"at": NOW - timedelta(minutes=1)},
                ),
            ),
            approvals=({"execution_id": "approval", "status": "pending"},),
        ),
        now=NOW,
        viewed_at=NOW - timedelta(hours=1),
    )
    assert [item.execution_id for item in result.needs_attention] == [
        "approval",
        "blocked",
    ]
    assert [item.execution_id for item in result.running] == ["running"]
    assert [item.execution_id for item in result.done_since_view] == ["done"]
    assert result.needs_attention[0].age_seconds == 10_800
    assert result.needs_attention[0].next_action == "Review the approval."


def test_duplicate_selectors_are_stably_disambiguated():
    result = overview(
        FleetFacts(executions=(execution("same"), execution("same"))), now=NOW
    )
    assert [item.selector for item in result.running] == [
        "fleet/ws/same",
        "fleet/ws/same~2",
    ]


def test_optional_facts_are_absent_without_affecting_projection():
    item = overview(FleetFacts(executions=(execution("bare"),)), now=NOW).running[0]
    assert item.model is item.provider is item.account_id is item.quota is None
    assert item.last_event is item.token_usage is item.monetary_cost is None
    assert item.context is item.parent is item.branch is item.delivery is None
    assert item.age_seconds is None


def test_projection_carries_supplied_native_details_without_reading_them():
    item = overview(
        FleetFacts(
            workspaces=({"id": "ws", "branch": "feature/fleet"},),
            executions=(
                execution(
                    "detailed",
                    model="gpt-5.6",
                    provider="codex",
                    account_id="owned",
                    context={"used": 80, "limit": 100},
                ),
            ),
            relationships=({"execution_id": "detailed", "parent_id": "parent"},),
            accounts=({"id": "owned", "quota": {"known": True, "remaining": 42}},),
            deliveries=({"execution_id": "detailed", "pr": "#12", "ci": "passing"},),
        ),
        now=NOW,
    ).running[0]
    assert item.model == "gpt-5.6"
    assert item.branch == "feature/fleet"
    assert item.context == {"used": 80, "limit": 100}
    assert item.parent == {"execution_id": "detailed", "parent_id": "parent"}
    assert item.delivery == {"execution_id": "detailed", "pr": "#12", "ci": "passing"}


def test_tokens_and_external_cost_keep_separate_provenance_without_price_guessing():
    item = overview(
        FleetFacts(
            executions=(
                execution(
                    "usage",
                    token_usage={"input": 12, "output": 8, "provenance": "provider"},
                    monetary_cost={
                        "amount": "0.03",
                        "currency": "USD",
                        "provenance": "billing-export",
                    },
                ),
            )
        ),
        now=NOW,
    ).running[0]
    assert item.token_usage == {"input": 12, "output": 8, "provenance": "provider"}
    assert item.monetary_cost == {
        "amount": "0.03",
        "currency": "USD",
        "provenance": "billing-export",
    }


def test_quota_reset_is_carried_for_display_and_can_require_attention():
    result = overview(
        FleetFacts(
            executions=(execution("limited", account_id="owned"),),
            accounts=(
                {
                    "id": "owned",
                    "provider": "codex",
                    "quota": {
                        "known": True,
                        "remaining": 0,
                        "resetsAt": "2026-08-18T14:00:00Z",
                    },
                },
            ),
        ),
        now=NOW,
    )
    item = result.needs_attention[0]
    assert item.urgency == "quota"
    assert item.quota["resetsAt"] == "2026-08-18T14:00:00Z"
    assert item.next_action == "Wait for the reported reset window."


def test_serialization_projects_only_public_fields_from_arbitrary_native_facts():
    result = overview(
        FleetFacts(
            executions=(
                execution(
                    "private",
                    token_usage={
                        "input": 7,
                        "provenance": "provider",
                        "access_token": "never",
                        "client_secret": "never",
                        "nested": {"cookie": "never"},
                    },
                    context={"used": 7, "cookie": "never", "session": "never"},
                ),
            ),
            deliveries=(
                {"execution_id": "private", "pr": "#12", "authorization": "never"},
            ),
        ),
        now=NOW,
    ).to_dict()
    encoded = repr(result)
    assert "never" not in encoded
    assert result["running"][0]["token_usage"] == {
        "input": 7,
        "provenance": "provider",
    }
    assert result["running"][0]["context"] == {"used": 7}
    assert result["running"][0]["delivery"] == {"pr": "#12"}


def test_usage_without_supplied_provenance_is_omitted_instead_of_relabelled():
    item = overview(
        FleetFacts(
            executions=(
                execution(
                    "unproven",
                    token_usage={"input": 7},
                    monetary_cost={"amount": "0.03", "currency": "USD"},
                ),
            )
        ),
        now=NOW,
    ).running[0]
    assert item.token_usage is None
    assert item.monetary_cost is None
