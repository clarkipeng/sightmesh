from __future__ import annotations

import json
from pathlib import Path

import pytest

from sightmesh import cli, execution_routing
from sightmesh.execution_routing import (
    ExecutionRoutingError,
    ExecutionRoutingSettings,
    ExecutionRoutingStore,
    Route,
    select_route,
)
from sightmesh.pool import core as pool_core


@pytest.fixture
def pool_root(monkeypatch, tmp_path: Path) -> Path:
    """Keep every test off the operator's real ~/.config/agent-pool."""
    root = tmp_path / "agent-pool"
    monkeypatch.setattr(pool_core, "default_pool_root", lambda: root)
    return root


def _codex_account(account_id: str, **overrides) -> dict:
    account = {
        "id": account_id,
        "provider": "codex",
        "kind": "chatgpt",
        "codex_home": f"/tmp/{account_id}",
    }
    account.update(overrides)
    if account.get("kind") == "apikey":
        account.setdefault("token_fp", f"fp-{account_id}")
    return account


def _claude_account(account_id: str, **overrides) -> dict:
    account = {"id": account_id, "provider": "claude", "kind": "oauth"}
    account.update(overrides)
    return account


def _claude_token() -> str:
    return "sk-ant-oat01-" + "a" * 120


def _no_quota(_account):
    return {"known": False, "reason": "no quota source"}


def _subscription_route(route_id: str, executor: str, model: str, pool: str) -> Route:
    return Route(
        id=route_id,
        executor=executor,
        model=model,
        billing_class="subscription",
        account_pool=pool,
    )


def _metered_route(route_id: str, executor: str, model: str, account: str) -> Route:
    return Route(
        id=route_id,
        executor=executor,
        model=model,
        billing_class="metered",
        account=account,
    )


# ---------------------------------------------------------------- selection


def test_first_healthy_subscription_route_wins(pool_root: Path, monkeypatch) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1"),
                _claude_account("max-a"),
            ]
        }
    )
    monkeypatch.setattr(pool_core, "quota", _no_quota)

    settings = ExecutionRoutingSettings(
        routes=(
            _subscription_route("codex-luna-subscriptions", "CODEX", "gpt-5.6-luna", "codex"),
            _subscription_route("claude-opus-subscriptions", "CLAUDE_CODE", "opus", "claude"),
        )
    )

    result = select_route(settings)

    assert result.status == "resolved"
    assert result.target.route_id == "codex-luna-subscriptions"
    assert result.target.auth_binding_id == "codex-sub1"
    assert result.target.billing_class == "subscription"


def test_cooling_missing_disabled_and_zero_quota_accounts_are_skipped_in_stable_order(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("missing-cred", codex_home=""),
                _codex_account("cooling"),
                _codex_account("disabled", disabled=True),
                _codex_account("zero-quota"),
                _codex_account("healthy"),
            ]
        }
    )
    pool_core.set_cooldown("cooling", 3600)

    def quota(account):
        if account["id"] == "zero-quota":
            return {"known": True, "remaining": 0, "resetsAt": "2099-01-01T00:00:00Z"}
        return {"known": False}

    monkeypatch.setattr(pool_core, "quota", quota)

    settings = ExecutionRoutingSettings(
        routes=(_subscription_route("codex-subs", "CODEX", "luna", "codex"),)
    )

    result = select_route(settings)

    assert result.status == "resolved"
    assert result.target.auth_binding_id == "healthy"
    joined = "\n".join(result.trace)
    assert "missing-cred: no credential stored" in joined
    assert "cooling: cooling" in joined
    assert "disabled: account disabled" in joined
    assert "zero-quota: zero quota" in joined


def test_new_auth_entry_is_discovered_without_any_code_change(
    pool_root: Path, monkeypatch
) -> None:
    # No source change - only pool.json is written directly, the way an
    # operator or a separate `pool add-*` command would.
    pool_core.save_pool({"accounts": [_codex_account("existing")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    settings = ExecutionRoutingSettings(
        routes=(_subscription_route("codex-subs", "CODEX", "luna", "codex"),)
    )

    first = select_route(settings)
    assert first.target.auth_binding_id == "existing"

    pool = pool_core.load_pool()
    pool["accounts"].insert(0, _codex_account("brand-new"))
    pool_core.save_pool(pool)

    second = select_route(settings)
    assert second.target.auth_binding_id == "brand-new"


def test_preferred_model_unavailable_advances_to_the_next_route(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1"),
                _claude_account("max-a"),
            ]
        }
    )
    pool_core.write_token("max-a", _claude_token())
    monkeypatch.setattr(pool_core, "quota", _no_quota)

    settings = ExecutionRoutingSettings(
        routes=(
            _subscription_route("codex-luna-subscriptions", "CODEX", "gpt-5.6-luna", "codex"),
            _subscription_route("claude-opus-subscriptions", "CLAUDE_CODE", "opus", "claude"),
        )
    )

    result = select_route(settings, preferred_model="opus")

    assert result.status == "resolved"
    assert result.target.route_id == "claude-opus-subscriptions"
    assert any("skip, model gpt-5.6-luna != preferred opus" in line for line in result.trace)


def test_cross_provider_fallback_preserves_the_logical_command_shape(
    pool_root: Path, monkeypatch
) -> None:
    # Codex-sub1 has no stored credential, so selection must fall through to
    # the Claude route while producing the same target shape either way.
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1", codex_home=""),
                _claude_account("max-a"),
            ]
        }
    )
    pool_core.write_token("max-a", _claude_token())
    monkeypatch.setattr(pool_core, "quota", _no_quota)

    settings = ExecutionRoutingSettings(
        routes=(
            _subscription_route("codex-luna-subscriptions", "CODEX", "gpt-5.6-luna", "codex"),
            _subscription_route("claude-opus-subscriptions", "CLAUDE_CODE", "opus", "claude"),
        )
    )

    result = select_route(settings)

    assert result.status == "resolved"
    assert result.target.route_id == "claude-opus-subscriptions"
    assert set(result.target.to_dict()) == {
        "route_id",
        "executor",
        "model",
        "billing_class",
        "auth_binding_id",
        "account_alias",
    }


def test_hidden_account_alias_redacts_selection_explanation(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("private-disabled", disabled=True),
                _codex_account("private-account-id"),
            ]
        }
    )
    monkeypatch.setattr(pool_core, "quota", _no_quota)

    settings = ExecutionRoutingSettings(
        routes=(_subscription_route("codex-subs", "CODEX", "luna", "codex"),),
        expose_account_alias=False,
    )

    result = select_route(settings)

    assert result.status == "resolved"
    assert result.target.auth_binding_id == "private-account-id"
    assert result.target.account_alias is None
    explanation = json.dumps(result.to_dict())
    assert "private-account-id" not in explanation
    assert "private-disabled" not in explanation
    assert "<redacted>" in explanation


def test_selection_uses_only_non_secret_account_eligibility(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1", codex_home=""),
                _codex_account("codex-api", kind="apikey", token_fp="fp-codex-api"),
            ]
        }
    )
    monkeypatch.setattr(pool_core, "quota", lambda a: {"known": False, "metered": True})

    def fail_secret_resolution(*_args, **_kwargs):
        raise AssertionError("secret-bearing launch material was resolved")

    monkeypatch.setattr(pool_core, "env_for", fail_secret_resolution)
    monkeypatch.setattr(pool_core, "read_token", fail_secret_resolution)

    result = select_route(_exhausted_subscription_and_metered_settings("ask"))

    assert result.status == "approval_needed"
    assert result.target.auth_binding_id == "codex-api"


# ---------------------------------------------------------------- metered policy


def _exhausted_subscription_and_metered_settings(metered_fallback: str) -> ExecutionRoutingSettings:
    return ExecutionRoutingSettings(
        routes=(
            _subscription_route("codex-luna-subscriptions", "CODEX", "gpt-5.6-luna", "codex"),
            _metered_route("codex-metered-api", "CODEX", "gpt-5.6-luna", "codex-api"),
        ),
        metered_fallback=metered_fallback,
    )


def test_auto_selects_the_first_eligible_metered_route_once_subscriptions_exhaust(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1", codex_home=""),  # exhausted: no credential
                _codex_account("codex-api", kind="apikey"),
            ]
        }
    )
    monkeypatch.setattr(pool_core, "quota", lambda a: {"known": False, "metered": True})

    settings = _exhausted_subscription_and_metered_settings("auto")
    result = select_route(settings)

    assert result.status == "resolved"
    assert result.target.route_id == "codex-metered-api"
    assert result.target.billing_class == "metered"
    assert result.target.auth_binding_id == "codex-api"


def test_ask_produces_a_request_for_approval_outcome_rather_than_a_resolved_target(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1", codex_home=""),
                _codex_account("codex-api", kind="apikey"),
            ]
        }
    )
    monkeypatch.setattr(pool_core, "quota", lambda a: {"known": False, "metered": True})

    settings = _exhausted_subscription_and_metered_settings("ask")
    result = select_route(settings)

    assert result.status == "approval_needed"
    assert result.reason == "approval_needed"
    assert result.target is not None
    assert result.target.route_id == "codex-metered-api"


def test_never_returns_blocked_without_touching_any_metered_account(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1", codex_home=""),
                _codex_account("codex-api", kind="apikey"),
            ]
        }
    )
    monkeypatch.setattr(pool_core, "quota", lambda a: {"known": False, "metered": True})

    looked_up: list[str] = []
    real_find = pool_core.find

    def spying_find(pool, account_id):
        looked_up.append(account_id)
        return real_find(pool, account_id)

    monkeypatch.setattr(pool_core, "find", spying_find)

    settings = _exhausted_subscription_and_metered_settings("never")
    result = select_route(settings)

    assert result.status == "blocked"
    assert result.reason == "routes_exhausted"
    assert result.target is None
    assert "codex-api" not in looked_up


def test_metered_fallback_policy_change_from_never_to_auto_takes_effect_immediately(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("codex-sub1", codex_home=""),
                _codex_account("codex-api", kind="apikey"),
            ]
        }
    )
    monkeypatch.setattr(pool_core, "quota", lambda a: {"known": False, "metered": True})

    blocked = select_route(_exhausted_subscription_and_metered_settings("never"))
    assert blocked.status == "blocked"

    resolved = select_route(_exhausted_subscription_and_metered_settings("auto"))
    assert resolved.status == "resolved"


# ---------------------------------------------------------------- settings validation


def test_invalid_metered_fallback_value_is_rejected_at_construction() -> None:
    with pytest.raises(ExecutionRoutingError, match="meteredFallback"):
        ExecutionRoutingSettings(metered_fallback="sometimes")


def test_invalid_metered_fallback_value_is_rejected_at_load_time(tmp_path: Path) -> None:
    path = tmp_path / "execution_routing.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "executionRouting": {"meteredFallback": "sometimes"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExecutionRoutingError, match="meteredFallback"):
        ExecutionRoutingStore(path).load()


def test_subscription_route_requires_account_pool_not_account() -> None:
    with pytest.raises(ExecutionRoutingError, match="requires accountPool"):
        Route(id="r1", executor="CODEX", model="luna", billing_class="subscription")
    with pytest.raises(ExecutionRoutingError, match="must not set account"):
        Route(
            id="r1",
            executor="CODEX",
            model="luna",
            billing_class="subscription",
            account_pool="codex",
            account="codex-api",
        )


def test_metered_route_requires_account_not_account_pool() -> None:
    with pytest.raises(ExecutionRoutingError, match="requires account"):
        Route(id="r1", executor="CODEX", model="luna", billing_class="metered")
    with pytest.raises(ExecutionRoutingError, match="must not set accountPool"):
        Route(
            id="r1",
            executor="CODEX",
            model="luna",
            billing_class="metered",
            account_pool="codex",
            account="codex-api",
        )


def test_duplicate_route_ids_are_rejected() -> None:
    with pytest.raises(ExecutionRoutingError, match="unique"):
        ExecutionRoutingSettings(
            routes=(
                _metered_route("dup", "CODEX", "luna", "codex-api"),
                _metered_route("dup", "CODEX", "luna", "codex-api-2"),
            )
        )


def test_same_route_retries_and_backoff_bounds_are_enforced() -> None:
    with pytest.raises(ExecutionRoutingError, match="sameRouteRetries"):
        ExecutionRoutingSettings(same_route_retries=4)
    with pytest.raises(ExecutionRoutingError, match="transientBackoffSeconds"):
        ExecutionRoutingSettings(transient_backoff_seconds=())
    with pytest.raises(ExecutionRoutingError, match="transientBackoffSeconds"):
        ExecutionRoutingSettings(transient_backoff_seconds=(0,))


def test_disabled_routing_blocks_without_inspecting_routes(pool_root: Path) -> None:
    settings = ExecutionRoutingSettings(
        enabled=False,
        routes=(_subscription_route("codex-subs", "CODEX", "luna", "codex"),),
    )

    result = select_route(settings)

    assert result.status == "blocked"
    assert result.reason == "routing_disabled"


def test_settings_round_trip_never_persists_a_secret_like_value(tmp_path: Path) -> None:
    path = tmp_path / "execution_routing.json"
    settings = ExecutionRoutingSettings(
        routes=(
            _subscription_route("codex-luna-subscriptions", "CODEX", "gpt-5.6-luna", "codex"),
            _metered_route("codex-metered-api", "CODEX", "gpt-5.6-luna", "codex-api"),
        ),
        metered_fallback="ask",
    )

    ExecutionRoutingStore(path).save(settings)
    loaded = ExecutionRoutingStore(path).load()

    assert loaded == settings
    raw = path.read_text(encoding="utf-8")
    lowered = raw.lower()
    assert "token" not in lowered
    assert "sk-ant" not in lowered
    assert "authorization" not in lowered
    assert path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- CLI wiring


@pytest.fixture
def routing_settings_path(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "execution_routing.json"
    monkeypatch.setattr(execution_routing, "default_settings_path", lambda: path)
    return path


def test_cli_routing_set_metered_persists_and_rejects_an_invalid_value(
    routing_settings_path: Path, capsys
) -> None:
    args = cli.parser().parse_args(["--json", "routing", "set-metered", "ask"])
    assert args.func(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["meteredFallback"] == "ask"

    with pytest.raises(SystemExit):
        cli.parser().parse_args(["--json", "routing", "set-metered", "sometimes"])


def test_cli_routing_routes_add_list_and_explain(
    pool_root: Path, routing_settings_path: Path, monkeypatch, capsys
) -> None:
    pool_core.save_pool({"accounts": [_codex_account("codex-sub1")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)

    add_args = cli.parser().parse_args(
        [
            "--json",
            "routing",
            "routes",
            "add",
            "--id",
            "codex-luna-subscriptions",
            "--executor",
            "CODEX",
            "--model",
            "gpt-5.6-luna",
            "--billing-class",
            "subscription",
            "--account-pool",
            "codex",
        ]
    )
    assert add_args.func(add_args) == 0
    capsys.readouterr()

    explain_args = cli.parser().parse_args(
        ["--json", "routing", "explain", "--workspace", "workspace-a"]
    )
    assert explain_args.func(explain_args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "resolved"
    assert payload["target"]["auth_binding_id"] == "codex-sub1"
    assert payload["workspace_id"] == "workspace-a"


def test_cli_routing_validate_reports_zero_eligible_accounts(
    pool_root: Path, routing_settings_path: Path, monkeypatch, capsys
) -> None:
    pool_core.save_pool(
        {"accounts": [_codex_account("disabled-account", disabled=True)]}
    )
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    ExecutionRoutingStore(routing_settings_path).save(
        ExecutionRoutingSettings(
            routes=(_subscription_route("codex-subs", "CODEX", "luna", "codex"),)
        )
    )

    args = cli.parser().parse_args(["--json", "routing", "validate"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["valid"] is False
    assert payload["warnings"] == ["route codex-subs: no eligible account"]
