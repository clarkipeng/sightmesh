"""Routing failover scenarios S-D1..S-D8 (root cause D).

Each one pins a behaviour the typed-outcome failover design is required to
hold, over the *real* pool, routing settings, task store, and effect journal
wired to ``FakeCdesktop``. Every assertion reads journal or pool state; none
reads text, because reading text is the bug these scenarios exist to prevent.

The historical failure they replace: the reroute trigger was a regex over the
worker's transcript (`pool_core.looks_limited`), so a real rate limit and a
test suite that merely printed "429" were indistinguishable, and a task whose
launch was rejected before it ever held a session could not reroute at all.
"""

from __future__ import annotations

import json
import time

import pytest

from sightmesh import execution_routing
from sightmesh.cli import parser
from sightmesh.pool import core as pool_core
from sightmesh.profiles import Profile, ProfileStore
from sightmesh.sdk import BatchError, SightMesh, SightMeshError
from sightmesh.task_store import TaskStore

from .conftest import (
    claude_account,
    configure_chains,
    cooling,
    metered,
    metered_account,
    seed_pool,
    subscription,
    target_of,
    worker_spec,
)

pytestmark = pytest.mark.simulator


def routed_spec(**kwargs: object):
    """A worker that goes through execution routing rather than an override."""
    return worker_spec(executor=None, **kwargs)


def start_rejected(mesh: SightMesh, status: int, retry_after: float | None = None):
    """Launch one routed task whose native launch is rejected with ``status``."""
    mesh.client.reject_after("launch", status, retry_after)
    with pytest.raises(BatchError):
        mesh.start(routed_spec())


# ---------------------------------------------------------------- S-D1


def test_sd1_a_typed_429_advances_the_chain_exactly_once(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D1: one typed 429 cools exactly one binding and opens exactly one new
    epoch; with no further outcome, no third epoch ever appears.

    Locks the whole transition against both directions of failure: a reroute
    that never happens (the historical bug for a launch-time rejection, which
    holds no session for a session-keyed sweep to find) and a reroute that
    happens repeatedly off one stale outcome.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(
        standard=(subscription("terra", "terra", "claude"),)
    )

    start_rejected(mesh, 429)
    task = store.get("operator", "audit")
    assert task is not None and task.state == "blocked" and task.epoch == 1
    assert mesh.journal.get(task.task_id, 1).outcome == "rate_limited"

    advanced = mesh.reconcile_provider_outcomes()

    assert [worker.state for worker in advanced] == ["active"]
    task = store.get("operator", "audit")
    assert task.epoch == 2 and task.state == "active"
    assert cooling("acct-a") > 0
    assert cooling("acct-b") == 0

    # No new outcome, so no new epoch: the journal is the only trigger.
    assert mesh.reconcile_provider_outcomes() == []
    assert store.get("operator", "audit").epoch == 2


# ---------------------------------------------------------------- S-D2


def test_sd2_a_second_account_is_tried_before_the_model_changes(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D2: the chain retries the same model on the pool's next account before
    it ever falls through to the next model.

    This needs no retry counter and has none: cooling only the failed binding
    and excluding it makes pool order do the work. The scenario is what proves
    that, because a design that switched models on the first refusal would pass
    S-D1 identically - the next model is right there in the chain, and must not
    be reached yet.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(
        standard=(
            subscription("terra", "terra", "claude"),
            subscription("luna", "luna", "claude"),
        )
    )

    start_rejected(mesh, 429)
    mesh.reconcile_provider_outcomes()

    target = target_of(store)
    assert (target["route_id"], target["model"]) == ("terra", "terra")
    assert target["auth_binding_id"] == "acct-b"


def test_sd2_the_second_refusal_falls_through_to_the_next_model(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D2 (continued): once both accounts of a model are cooled, the next
    epoch lands on the following hop rather than back on a cooled account.

    Runs the chain to its end so the walk is proven to terminate somewhere
    usable, not merely to move once.
    """
    seed_pool(
        claude_account("acct-a"), claude_account("acct-b"), metered_account("codex-api")
    )
    configure_chains(
        standard=(
            subscription("terra", "terra", "claude"),
            metered("sol", "sol", "codex-api"),
        )
    )

    start_rejected(mesh, 429)
    # The replacement launch is refused too, so the second account cools as
    # part of opening epoch 2 rather than in a separate step.
    mesh.client.reject_after("launch", 429)
    mesh.reconcile_provider_outcomes()
    assert target_of(store)["auth_binding_id"] == "acct-b"
    assert store.get("operator", "audit").state == "blocked"

    advanced = mesh.reconcile_provider_outcomes()

    assert [worker.state for worker in advanced] == ["active"]
    assert target_of(store)["route_id"] == "sol"
    assert target_of(store)["auth_binding_id"] == "codex-api"
    assert cooling("acct-a") > 0 and cooling("acct-b") > 0


# ---------------------------------------------------------------- S-D3


def test_sd3_a_cooling_account_is_skipped_until_its_reset_passes(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings, monkeypatch
) -> None:
    """S-D3: a cooldown written from the provider's own ``retry_at`` is honoured
    until that moment and no longer.

    The failure this guards is a chain that walks straight back onto the
    account it just cooled - which is what happens the instant cooldown lives
    anywhere but durable pool state.
    """
    seed_pool(
        claude_account("acct-a"),
        claude_account("acct-b", disabled=True),
        metered_account("codex-api"),
    )
    settings = configure_chains(
        standard=(
            subscription("terra", "terra", "claude"),
            metered("luna", "luna", "codex-api"),
        )
    )
    reset_at = time.time() + 3600
    pool_core.cool_until_timestamp("acct-a", reset_at)

    skipped = execution_routing.select_route(settings)

    assert skipped.status == "resolved"
    assert skipped.target.route_id == "luna"
    assert any("cooling" in line for line in skipped.trace)

    monkeypatch.setattr(time, "time", lambda: reset_at + 1)
    recovered = execution_routing.select_route(settings)

    assert recovered.target.route_id == "terra"
    assert recovered.target.auth_binding_id == "acct-a"


# ---------------------------------------------------------------- S-D4


def test_sd4_a_test_failure_never_reroutes_however_its_output_reads(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D4: a task that ended on its own work - a failing test suite - carries
    no provider outcome, so no reroute and no cooldown can follow.

    The hostile half is the point: the worker's transcript literally says
    "429 Too Many Requests". Under the regex trigger that was a reroute onto a
    fresh account; under the typed trigger it cannot be, because a task-level
    block writes no capacity outcome anywhere.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(routed_spec())
    mesh.client.processes[started.session_id] = [
        {"id": "failed-tests", "run_reason": "codingagent", "status": "failed"}
    ]
    mesh.client.snapshots["failed-tests"] = {
        "entries": [
            {
                "content": {
                    "entry_type": {"type": "assistant_message"},
                    "content": "FAIL test_rate_limits: expected 429 Too Many Requests",
                }
            }
        ]
    }
    mesh.blocked("2 tests failed", worker="audit")

    assert mesh.reconcile_provider_outcome(started.session_id) is None
    assert mesh.reconcile_provider_outcomes() == []
    task = store.get("operator", "audit")
    assert task.epoch == 1 and task.state == "blocked"
    assert cooling("acct-a") == 0 and cooling("acct-b") == 0


# ---------------------------------------------------------------- S-D5


def test_sd5_an_auth_outcome_reroutes_on_a_short_cooldown(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D5: a 401 is per-account and is not capacity, so the account cools for
    the short window rather than the multi-hour capacity default.

    Cooling a rotated credential for five hours would strand a healthy account
    for the rest of the working day, which is why the outcome - not the mere
    fact of failure - picks the duration.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    start_rejected(mesh, 401)
    task = store.get("operator", "audit")
    assert mesh.journal.get(task.task_id, 1).outcome == "auth"

    mesh.reconcile_provider_outcomes()

    assert target_of(store)["auth_binding_id"] == "acct-b"
    assert store.get("operator", "audit").state == "active"
    assert 0 < cooling("acct-a") <= pool_core.SHORT_COOLDOWN
    assert cooling("acct-a") < pool_core.DEFAULT_COOLDOWN


# ---------------------------------------------------------------- S-D6


def test_sd6_provider_down_cools_the_whole_provider_and_falls_to_the_next_hop(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D6: a 5xx is the provider failing, not the account, so every account in
    that route's pool cools together and the chain skips straight past its
    remaining hops on the same provider.

    Cooling one account at a time here would walk the chain through the whole
    pool, one doomed launch per account, before reaching a provider that works.
    """
    seed_pool(
        claude_account("acct-a"), claude_account("acct-b"), metered_account("codex-api")
    )
    configure_chains(
        standard=(
            subscription("terra", "terra", "claude"),
            subscription("luna", "luna", "claude"),
            metered("sol", "sol", "codex-api"),
        )
    )

    start_rejected(mesh, 503)
    task = store.get("operator", "audit")
    assert mesh.journal.get(task.task_id, 1).outcome == "provider_down"

    mesh.reconcile_provider_outcomes()

    assert cooling("acct-a") > 0 and cooling("acct-b") > 0
    assert cooling("acct-a") <= pool_core.SHORT_COOLDOWN
    assert target_of(store)["route_id"] == "sol"
    assert target_of(store)["auth_binding_id"] == "codex-api"


# ---------------------------------------------------------------- S-D7


def test_sd7_validate_fails_closed_before_any_epoch_exists(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings, capsys
) -> None:
    """S-D7: a class with no usable hop is refused *before* dispatch opens an
    epoch, and ``routing validate`` names the offending class.

    Failing closed after the epoch exists is not the same guarantee: it leaves
    a reserved task and an effect row behind for a launch that was never
    possible. The contrasting standard dispatch is what proves the gate is a
    real check and not a blanket refusal.
    """
    seed_pool(claude_account("acct-a"))
    configure_chains(
        standard=(subscription("terra", "terra", "claude"),),
        deep=(subscription("opus", "opus", "codex"),),
    )

    args = parser().parse_args(["--json", "routing", "validate"])
    assert args.func(args) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert [entry["routeClass"] for entry in payload["classes"] if not entry["valid"]] == [
        "deep"
    ]

    with pytest.raises(SightMeshError, match="deep"):
        mesh.start(routed_spec(key="deepwork", route_class="deep"))

    assert store.get("operator", "deepwork") is None
    assert mesh.journal.with_outcomes({"rate_limited", "auth", "provider_down"}) == []
    assert mesh.client.calls("managed_launch") == []

    started = mesh.start(routed_spec(key="ordinary"))
    assert started.state == "active"


# ---------------------------------------------------------------- S-D8


def test_sd8_an_explicit_profile_override_remains_recoverable(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings, tmp_path, monkeypatch
) -> None:
    """S-D8: a task launched from an explicit profile still advances on a typed
    outcome instead of dying where it stands.

    The old target for an explicit profile carried neither a route id nor a
    binding, and the reconciler required both, so such a task returned ``None``
    forever - silently pinned to a provider that had already refused it. It now
    records its class, so the chain can pick up the work.
    """
    monkeypatch.setattr(
        "sightmesh.sdk.ProfileStore", lambda: ProfileStore(tmp_path / "profiles.json")
    )
    ProfileStore(tmp_path / "profiles.json").set(
        Profile(
            name="pinned",
            executor="CODEX",
            provider_id="default-provider",
            automatic_failover=True,
        )
    )
    seed_pool(claude_account("acct-a"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    mesh.client.reject_after("launch", 429)
    with pytest.raises(BatchError):
        mesh.start(worker_spec(executor=None, profile="pinned"))
    assert target_of(store)["route_id"] == "profile:pinned"

    advanced = mesh.reconcile_provider_outcomes()

    assert [worker.state for worker in advanced] == ["active"]
    assert target_of(store)["route_id"] == "terra"
    assert target_of(store)["auth_binding_id"] == "acct-a"


def test_sd8_a_profile_that_forbids_failover_blocks_with_a_reason(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings, tmp_path, monkeypatch
) -> None:
    """S-D8 (contrast): ``automatic_failover`` off is the operator saying this
    task runs here or not at all - but the task must still say so.

    Recoverable does not mean automatic. What it cannot mean is the old
    behaviour, where the reconciler returned ``None`` and left no trace of why.
    """
    monkeypatch.setattr(
        "sightmesh.sdk.ProfileStore", lambda: ProfileStore(tmp_path / "profiles.json")
    )
    ProfileStore(tmp_path / "profiles.json").set(
        Profile(
            name="pinned",
            executor="CODEX",
            provider_id="default-provider",
            automatic_failover=False,
        )
    )
    seed_pool(claude_account("acct-a"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(worker_spec(executor=None, profile="pinned"))
    task = store.get("operator", "audit")
    mesh.journal.mark_terminal(task.task_id, task.epoch, "rate_limited")

    blocked = mesh.reconcile_provider_outcome(started.session_id)

    assert blocked is not None and blocked.state == "blocked"
    task = store.get("operator", "audit")
    assert task.epoch == 1
    assert "automatic failover is off" in str(task.result)


# ---------------------------------------------------------------- S-D9


def test_sd9_an_upgraded_v1_install_still_starts_a_fanning_out_manager(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D9: the exact shape every existing install upgrades into.

    A v1 settings file is one flat route list with no class concept, so the
    forward migration can only fill ``standard`` - it has no way to invent a
    ``deep`` chain the operator never configured. Meanwhile ``class_for``
    promotes any top-level supervised manager with children to ``deep``. The
    two together meant `sightmesh start mgr --children 4` failed closed on
    every upgraded install for work that ran the day before, so this writes a
    genuine v1 file and proves the promotion degrades onto the chain that
    exists instead of refusing.
    """
    seed_pool(claude_account("acct-a"))
    routing_settings.write_text(
        json.dumps(
            {
                "version": 1,
                "executionRouting": {
                    "enabled": True,
                    "routes": [
                        {
                            "id": "fable",
                            "executor": "CLAUDE_CODE",
                            "model": "fable",
                            "billingClass": "subscription",
                            "accountPool": "claude",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    settings = execution_routing.ExecutionRoutingStore().load()
    assert [chain.route_class for chain in settings.chains] == ["standard"]
    assert execution_routing.validate_chain(settings, "deep").valid is False

    manager = mesh.start(routed_spec(key="mgr", children=4))

    assert manager.state == "active"
    target = target_of(store, "mgr")
    assert (target["route_class"], target["route_id"]) == ("standard", "fable")
    assert target["auth_binding_id"] == "acct-a"
