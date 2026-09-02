from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from sightmesh import cli, execution_routing
from sightmesh.execution_routing import (
    ExecutionRoutingError,
    ExecutionRoutingSettings,
    ExecutionRoutingStore,
    Route,
    RouteChain,
    select_route,
)
from sightmesh.pool import core as pool_core

from fixtures.routing import _chains


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


def _free_route(route_id: str, executor: str, model: str) -> Route:
    return Route(id=route_id, executor=executor, model=model, billing_class="free")


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
        chains=_chains(
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
        chains=_chains(_subscription_route("codex-subs", "CODEX", "luna", "codex"),)
    )

    result = select_route(settings)

    assert result.status == "resolved"
    assert result.target.auth_binding_id == "healthy"
    joined = "\n".join(result.trace)
    assert "missing-cred: no credential stored" in joined
    assert "cooling: cooling" in joined
    assert "disabled: account disabled" in joined
    assert "zero-quota: zero quota" in joined


def test_hidden_account_aliases_are_absent_from_selection_traces(
    pool_root: Path, monkeypatch
) -> None:
    pool_core.save_pool(
        {"accounts": [_codex_account("private-account-alias")]}
    )
    monkeypatch.setattr(pool_core, "quota", _no_quota)

    result = select_route(
        ExecutionRoutingSettings(
            chains=_chains(_subscription_route("codex-subs", "CODEX", "luna", "codex"),),
            expose_account_alias=False,
        )
    )

    assert result.target is not None
    assert result.target.auth_binding_id == "private-account-alias"
    assert result.target.account_alias is None
    assert "private-account-alias" not in "\n".join(result.trace)


def test_new_auth_entry_is_discovered_without_any_code_change(
    pool_root: Path, monkeypatch
) -> None:
    # No source change - only pool.json is written directly, the way an
    # operator or a separate `pool add-*` command would.
    pool_core.save_pool({"accounts": [_codex_account("existing")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    settings = ExecutionRoutingSettings(
        chains=_chains(_subscription_route("codex-subs", "CODEX", "luna", "codex"),)
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
        chains=_chains(
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
        chains=_chains(
            _subscription_route("codex-luna-subscriptions", "CODEX", "gpt-5.6-luna", "codex"),
            _subscription_route("claude-opus-subscriptions", "CLAUDE_CODE", "opus", "claude"),
        )
    )

    result = select_route(settings)

    assert result.status == "resolved"
    assert result.target.route_id == "claude-opus-subscriptions"
    assert set(result.target.to_dict()) == {
        "route_class",
        "route_id",
        "executor",
        "model",
        "billing_class",
        "auth_binding_id",
        "account_alias",
    }


# ---------------------------------------------------------------- metered policy


def _exhausted_subscription_and_metered_settings(metered_fallback: str) -> ExecutionRoutingSettings:
    return ExecutionRoutingSettings(
        chains=_chains(
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


def test_selection_never_reads_launch_material_even_for_metered_ask(
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
    pool_core.write_token("codex-api", "sk-secret")
    monkeypatch.setattr(pool_core, "quota", lambda _account: {"known": False})
    monkeypatch.setattr(
        pool_core,
        "env_for",
        lambda _account: pytest.fail("selection must not resolve launch material"),
    )
    monkeypatch.setattr(
        pool_core,
        "read_token",
        lambda _account_id: pytest.fail("selection must not read launch material"),
    )

    result = select_route(_exhausted_subscription_and_metered_settings("ask"))

    assert result.status == "approval_needed"
    assert result.target is not None
    assert result.target.auth_binding_id == "codex-api"


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
            chains=_chains(
                _metered_route("dup", "CODEX", "luna", "codex-api"),
                _metered_route("dup", "CODEX", "luna", "codex-api-2"),
            )
        )


def test_an_unknown_route_class_is_rejected_at_construction() -> None:
    """Class membership is the one closed set the routing model owns, so a
    typo in a chain or a default must fail loudly at load rather than silently
    select nothing at dispatch time."""
    with pytest.raises(ExecutionRoutingError, match="route class"):
        RouteChain("shallow", ())
    with pytest.raises(ExecutionRoutingError, match="defaultClass"):
        ExecutionRoutingSettings(default_class="shallow")


def test_disabled_routing_blocks_without_inspecting_routes(pool_root: Path) -> None:
    settings = ExecutionRoutingSettings(
        enabled=False,
        chains=_chains(_subscription_route("codex-subs", "CODEX", "luna", "codex"),),
    )

    result = select_route(settings)

    assert result.status == "blocked"
    assert result.reason == "routing_disabled"


def test_settings_round_trip_never_persists_a_secret_like_value(tmp_path: Path) -> None:
    path = tmp_path / "execution_routing.json"
    settings = ExecutionRoutingSettings(
        chains=_chains(
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


def test_cli_routing_set_free_fallback_toggles_the_billing_opt_in(
    routing_settings_path: Path, capsys
) -> None:
    """Why: degrading a failed free route onto an account that bills is an
    operator decision, so it needs an explicit switch and must start off."""
    args = cli.parser().parse_args(["--json", "routing", "show"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["fallbackOnFreeFailure"] is False

    args = cli.parser().parse_args(["--json", "routing", "set-free-fallback", "on"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["fallbackOnFreeFailure"] is True
    assert json.loads(routing_settings_path.read_text(encoding="utf-8"))[
        "executionRouting"
    ]["fallbackOnFreeFailure"] is True

    args = cli.parser().parse_args(["--json", "routing", "set-free-fallback", "off"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["fallbackOnFreeFailure"] is False

    with pytest.raises(SystemExit):
        cli.parser().parse_args(["--json", "routing", "set-free-fallback", "maybe"])


def test_cli_routing_validate_reports_routes_without_an_eligible_account(
    pool_root: Path, routing_settings_path: Path, monkeypatch, capsys
) -> None:
    pool_core.save_pool(
        {
            "accounts": [
                _codex_account("disabled", disabled=True),
                _codex_account("cooling"),
                _codex_account("missing-credential", codex_home=""),
                _codex_account("zero-quota"),
                _codex_account("api-key-only", kind="apikey"),
            ]
        }
    )
    pool_core.set_cooldown("cooling", 3600)
    monkeypatch.setattr(
        pool_core,
        "quota",
        lambda account: {"known": True, "remaining": 0}
        if account["id"] == "zero-quota"
        else {"known": False},
    )
    ExecutionRoutingStore(routing_settings_path).save(
        ExecutionRoutingSettings(
            chains=_chains(_subscription_route("codex-subs", "CODEX", "luna", "codex"),)
        )
    )

    args = cli.parser().parse_args(["--json", "routing", "validate"])

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["warnings"] == [
        "class standard: route codex-subs: no eligible account"
    ]
    assert [
        (entry["routeClass"], entry["valid"], entry["reason"])
        for entry in payload["classes"]
    ] == [
        ("standard", False, "routes_exhausted"),
        ("deep", False, "routes_exhausted"),
    ]


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


# ---------------------------------------------------------------- free routes


OPENCODE_FREE_MODEL = "opencode/x-preview-f-free"


def test_free_route_resolves_without_consulting_the_pool(
    pool_root: Path, monkeypatch
) -> None:
    # A free tier owns no account, so nothing about the pool - not eligibility,
    # not cooldowns, and above all not credentials - may gate it.
    for name in ("load_pool", "load_state"):
        monkeypatch.setattr(
            pool_core,
            name,
            lambda name=name: pytest.fail(f"free route read pool {name}"),
        )
    monkeypatch.setattr(
        pool_core,
        "env_for",
        lambda _account: pytest.fail("free route resolved launch material"),
    )
    monkeypatch.setattr(
        pool_core,
        "read_token",
        lambda _account_id: pytest.fail("free route read launch material"),
    )

    result = select_route(
        ExecutionRoutingSettings(
            chains=_chains(_free_route("opencode-ox-free", "OPENCODE", OPENCODE_FREE_MODEL),)
        )
    )

    assert result.status == "resolved"
    assert result.target is not None
    assert result.target.executor == "OPENCODE"
    assert result.target.model == OPENCODE_FREE_MODEL
    assert result.target.billing_class == "free"


def test_free_route_binds_to_no_account(pool_root: Path) -> None:
    result = select_route(
        ExecutionRoutingSettings(
            chains=_chains(_free_route("opencode-ox-free", "OPENCODE", OPENCODE_FREE_MODEL),)
        )
    )

    assert result.target is not None
    assert result.target.auth_binding_id == execution_routing.FREE_AUTH_BINDING
    assert result.target.account_alias is None


def test_free_route_is_skipped_when_another_model_is_preferred(
    pool_root: Path,
) -> None:
    result = select_route(
        ExecutionRoutingSettings(
            chains=_chains(_free_route("opencode-ox-free", "OPENCODE", OPENCODE_FREE_MODEL),)
        ),
        preferred_model="opus",
    )

    assert result.status == "blocked"
    assert result.reason == "routes_exhausted"


def test_free_route_may_not_claim_an_account_or_pool() -> None:
    for extra in ({"account": "codex-api"}, {"account_pool": "codex"}):
        with pytest.raises(ExecutionRoutingError, match="free route"):
            Route(
                id="opencode-ox-free",
                executor="OPENCODE",
                model=OPENCODE_FREE_MODEL,
                billing_class="free",
                **extra,
            )


def test_free_route_never_warns_about_missing_accounts(pool_root: Path) -> None:
    settings = ExecutionRoutingSettings(
        chains=_chains(_free_route("opencode-ox-free", "OPENCODE", OPENCODE_FREE_MODEL),)
    )

    assert execution_routing.route_warnings(settings) == []


def test_free_route_survives_a_store_round_trip(tmp_path: Path) -> None:
    store = ExecutionRoutingStore(tmp_path / "execution_routing.json")
    saved = store.save(
        ExecutionRoutingSettings(
            chains=_chains(_free_route("opencode-ox-free", "OPENCODE", OPENCODE_FREE_MODEL),)
        )
    )

    assert store.load() == saved
    assert "account" not in json.loads(
        (tmp_path / "execution_routing.json").read_text(encoding="utf-8")
    )["executionRouting"]["chains"][0]["routes"][0]


# ---------------------------------------------------------------- route classes


def test_a_v1_flat_route_list_migrates_into_the_standard_chain(tmp_path: Path) -> None:
    """A settings file written before route classes existed still has to load.

    v1 had one ordered list and no class concept, so that list *is* the
    standard chain - the migration is total and invents nothing. Refusing the
    file instead would silently leave an upgraded operator with no routes at
    all, which reads as "everything is blocked" rather than "read your config".
    """
    path = tmp_path / "execution_routing.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "executionRouting": {
                    "routes": [
                        {
                            "id": "codex-subs",
                            "executor": "CODEX",
                            "model": "gpt-5.6-luna",
                            "billingClass": "subscription",
                            "accountPool": "codex",
                        }
                    ],
                    "meteredFallback": "ask",
                },
            }
        ),
        encoding="utf-8",
    )

    settings = ExecutionRoutingStore(path).load()

    assert [chain.route_class for chain in settings.chains] == ["standard"]
    assert [route.id for route in settings.routes_for("standard")] == ["codex-subs"]
    assert settings.metered_fallback == "ask"
    assert settings.routes_for("deep") == ()


def test_saving_migrated_settings_writes_the_current_version(tmp_path: Path) -> None:
    """Migration is forward-only: once written back, the file is v2 chains and
    the next load takes no migration path at all."""
    path = tmp_path / "execution_routing.json"
    path.write_text(
        json.dumps({"version": 1, "executionRouting": {"routes": []}}), encoding="utf-8"
    )
    store = ExecutionRoutingStore(path)

    store.save(store.load())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == execution_routing.SETTINGS_VERSION
    assert "routes" not in payload["executionRouting"]
    assert payload["executionRouting"]["chains"] == []


def test_selection_never_leaves_the_class_it_was_asked_for(
    pool_root: Path, monkeypatch
) -> None:
    """The class is frozen at dispatch so every failover walks the same chain.

    A selector that fell through to another class on exhaustion would quietly
    downgrade deep work onto the standard chain - the exact decision the
    operator made a class for.
    """
    pool_core.save_pool({"accounts": [_codex_account("codex-sub1")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    settings = ExecutionRoutingSettings(
        chains=(
            RouteChain("standard", (_subscription_route("terra", "CODEX", "terra", "codex"),)),
            RouteChain("deep", (_subscription_route("opus", "CLAUDE_CODE", "opus", "claude"),)),
        )
    )

    assert select_route(settings, route_class="standard").target.route_id == "terra"
    deep = select_route(settings, route_class="deep")
    assert deep.status == "blocked"
    assert deep.reason == "routes_exhausted"


def test_a_route_id_may_repeat_across_classes_but_not_within_one() -> None:
    """Ids are chain-scoped because a class is the unit of selection: two
    classes naming their terminal hop `sol` is ordinary, and forcing global
    uniqueness would make one class's naming leak into the other."""
    hop = _subscription_route("sol", "CODEX", "gpt-5.6-sol", "codex")
    settings = ExecutionRoutingSettings(
        chains=(RouteChain("standard", (hop,)), RouteChain("deep", (hop,)))
    )
    assert settings.route("deep", "sol") == hop

    with pytest.raises(ExecutionRoutingError, match="unique within class"):
        RouteChain("standard", (hop, hop))


def test_one_class_may_hold_only_one_chain() -> None:
    """Two chains for one class would make "which routes does standard have"
    ambiguous, and selection would silently answer with whichever came first."""
    hop = _subscription_route("sol", "CODEX", "gpt-5.6-sol", "codex")
    with pytest.raises(ExecutionRoutingError, match="only one chain"):
        ExecutionRoutingSettings(
            chains=(RouteChain("standard", (hop,)), RouteChain("standard", ()))
        )


def test_class_for_reads_scope_and_risk_and_never_a_model_name() -> None:
    """Which class a task takes is decided from its own shape - permission,
    parentage, fan-out - never from the model it happens to name. Model names
    are operator data, so a policy that branched on them would break the moment
    a chain was reconfigured."""
    settings = ExecutionRoutingSettings()
    deep_shape = execution_routing.ScopeRisk(
        permission="SUPERVISED", top_level=True, children=4
    )

    assert execution_routing.class_for(deep_shape, settings) == "deep"
    # An explicit operator choice outranks the policy in both directions.
    assert (
        execution_routing.class_for(
            dataclasses.replace(deep_shape, route_class="standard"), settings
        )
        == "standard"
    )
    assert (
        execution_routing.class_for(
            execution_routing.ScopeRisk(route_class="deep", children=0), settings
        )
        == "deep"
    )
    # A child, an unsupervised task, and a manager with no children are all
    # ordinary work.
    for ordinary in (
        dataclasses.replace(deep_shape, top_level=False),
        dataclasses.replace(deep_shape, permission="ACCEPT_EDITS"),
        dataclasses.replace(deep_shape, children=0),
    ):
        assert execution_routing.class_for(ordinary, settings) == "standard"


def test_validate_chain_fails_closed_on_an_empty_or_dead_chain(
    pool_root: Path, monkeypatch
) -> None:
    """`routing validate` has to be a proof, not a warning: an unconfigured
    class, a disabled selector, and a chain whose every account is cooled all
    have to come back invalid, or a dispatch gated on it opens an epoch it can
    never fill."""
    pool_core.save_pool({"accounts": [_codex_account("codex-sub1")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    live = _subscription_route("terra", "CODEX", "terra", "codex")
    settings = ExecutionRoutingSettings(chains=(RouteChain("standard", (live,)),))

    assert execution_routing.validate_chain(settings, "standard").valid
    assert execution_routing.validate_chain(settings, "deep").reason == "routes_exhausted"
    assert (
        execution_routing.validate_chain(
            dataclasses.replace(settings, enabled=False), "standard"
        ).reason
        == "routing_disabled"
    )

    pool_core.set_cooldown("codex-sub1", 3600)
    dead = execution_routing.validate_chain(settings, "standard")
    assert dead.valid is False
    assert any("cooling" in line for line in dead.trace)


def test_validate_all_covers_every_class_including_unconfigured_ones(
    pool_root: Path, monkeypatch
) -> None:
    """Regression guard for the check that could not see the problem it exists
    to find: `class_for` promotes work onto `deep`, and the v1->v2 migration
    fills only `standard`, so on every migrated install `deep` is empty. A
    validate that iterated only the *configured* chains reported that install
    fully valid, and the first fanning-out manager then failed to dispatch."""
    pool_core.save_pool({"accounts": [_codex_account("codex-sub1")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    migrated = ExecutionRoutingSettings(
        chains=_chains(_subscription_route("terra", "CODEX", "terra", "codex"))
    )

    results = execution_routing.validate_all(migrated)

    assert [(r.route_class, r.valid) for r in results] == [
        ("standard", True),
        ("deep", False),
    ]
    assert [(r.route_class, r.valid) for r in execution_routing.validate_all(
        ExecutionRoutingSettings()
    )] == [("standard", False), ("deep", False)]


def test_a_metered_hop_awaiting_approval_still_counts_as_a_usable_path(
    pool_root: Path, monkeypatch
) -> None:
    """Approval is a human gate, not an absence of capacity. Treating it as
    invalid would refuse dispatch for work that has somewhere to go."""
    pool_core.save_pool({"accounts": [_codex_account("codex-api", kind="apikey")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    settings = ExecutionRoutingSettings(
        chains=_chains(_metered_route("sol", "CODEX", "gpt-5.6-sol", "codex-api")),
        metered_fallback="ask",
    )

    assert execution_routing.validate_chain(settings, "standard").valid


def test_cli_routes_edits_one_class_and_leaves_the_others_alone(
    pool_root: Path, routing_settings_path: Path, monkeypatch, capsys
) -> None:
    """Editing a chain must be a per-class operation. A `routes add` that
    rewrote the whole settings object would silently drop the other class's
    chain, which is the kind of loss an operator only discovers at dispatch.
    """
    pool_core.save_pool({"accounts": [_codex_account("codex-sub1")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)

    def run(*argv: str) -> object:
        args = cli.parser().parse_args(["--json", "routing", *argv])
        assert args.func(args) == 0
        return json.loads(capsys.readouterr().out)

    for route_class, model in (("standard", "terra"), ("deep", "opus")):
        run(
            "routes", "add", "--class", route_class,
            "--id", f"{model}-hop", "--executor", "CODEX",
            "--model", model, "--billing-class", "subscription",
            "--account-pool", "codex",
        )

    assert [row["id"] for row in run("routes", "list")] == ["terra-hop"]
    assert [row["id"] for row in run("routes", "list", "--class", "deep")] == [
        "opus-hop"
    ]

    run("routes", "remove", "--class", "deep", "opus-hop")
    assert [row["id"] for row in run("routes", "list")] == ["terra-hop"]
    assert run("routes", "list", "--class", "deep") == []


def test_cli_explain_narrates_the_class_chain_it_walked(
    pool_root: Path, routing_settings_path: Path, monkeypatch, capsys
) -> None:
    """`explain` is the operator's answer to "why did this land here". Naming
    the class and the chain it walked is what makes that answer complete when
    two classes are configured."""
    pool_core.save_pool({"accounts": [_codex_account("codex-sub1")]})
    monkeypatch.setattr(pool_core, "quota", _no_quota)
    ExecutionRoutingStore(routing_settings_path).save(
        ExecutionRoutingSettings(
            chains=(
                RouteChain(
                    "standard",
                    (_subscription_route("terra", "CODEX", "terra", "codex"),),
                ),
                RouteChain(
                    "deep", (_subscription_route("opus", "CODEX", "opus", "codex"),)
                ),
            )
        )
    )

    args = cli.parser().parse_args(["--json", "routing", "explain", "--class", "deep"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["routeClass"] == "deep"
    assert [route["id"] for route in payload["chain"]] == ["opus"]
    assert payload["target"]["route_id"] == "opus"
    assert payload["target"]["route_class"] == "deep"
