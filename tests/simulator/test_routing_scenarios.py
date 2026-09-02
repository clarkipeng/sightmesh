"""Routing failover scenarios S-D1..S-D15 (root cause D).

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
from sightmesh import sdk as sdk_module
from sightmesh.cdesktop import CdesktopError
from sightmesh.cli import parser
from sightmesh.pool import core as pool_core
from sightmesh.profiles import Profile, ProfileStore
from sightmesh.sdk import BatchError, SightMesh, SightMeshError
from sightmesh.succession import SuccessionError
from sightmesh.task_store import TaskStore, TaskStoreError

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
    """S-D6: a provider that is down is not a failure of one account, so every
    account in that route's pool cools together and the chain skips straight
    past its remaining hops on the same provider.

    Cooling one account at a time here would walk the chain through the whole
    pool, one doomed launch per account, before reaching a provider that works.

    The outcome is written onto the journal directly because nothing produces
    it yet: it can only come from an upstream signal cdesktop reports, and the
    pinned seam exposes none (see `_rejection_outcome`). This scenario is what
    keeps the handling correct and exercised until the seam does.
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

    started = mesh.start(routed_spec())
    task = store.get("operator", "audit")
    assert target_of(store)["auth_binding_id"] == "acct-a"
    mesh._record_provider_outcome(task, "provider_down", None)

    advanced = mesh.reconcile_provider_outcome(started.session_id)

    assert advanced is not None and advanced.state == "active"
    assert cooling("acct-a") > 0 and cooling("acct-b") > 0
    assert cooling("acct-a") <= pool_core.SHORT_COOLDOWN
    assert target_of(store)["route_id"] == "sol"
    assert target_of(store)["auth_binding_id"] == "codex-api"


def test_sd6_a_local_cdesktop_5xx_never_cools_an_account(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D6 (contrast): a 5xx from the localhost cdesktop call is a local fault
    and must not be read as the model provider being down.

    That status describes cdesktop restarting, out of disk, or panicking - none
    of which is evidence about any account. Typing it as a rejection cooled the
    entire pool for the short window and blocked the task; it is an ordinary
    unreachable-service error, so the reservation stays adoptable and the next
    tick simply retries.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    mesh.client.fail_launch(CdesktopError("PUT /task-launches failed: HTTP 503: down"))
    with pytest.raises(BatchError):
        mesh.start(routed_spec())

    task = store.get("operator", "audit")
    assert task is not None and task.state == "reserved"
    effect = mesh.journal.get(task.task_id, task.epoch)
    assert effect.state == "reserved" and effect.outcome is None
    assert cooling("acct-a") == 0 and cooling("acct-b") == 0

    # The local service comes back and the same reservation is filled.
    assert mesh.start(routed_spec()).state == "active"
    assert store.get("operator", "audit").epoch == 1


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


# ---------------------------------------------------------------- S-D11


def test_sd11_a_rate_limit_hit_mid_run_reroutes_onto_the_next_account(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D11: a task that launched fine and was refused an hour later still
    fails over.

    This is the case a launch-time-only design cannot see at all. There is no
    rejected launch to type: the epoch's effect is ``launched`` and stays that
    way, so with nothing watching the running session the task hangs on a dead
    account until a human notices. The session's own process record is the only
    place the refusal appears, and its typed outcome class - not its transcript
    - is what turns into a journal outcome the chain can act on.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(routed_spec())
    assert target_of(store)["auth_binding_id"] == "acct-a"
    mesh.client.fail_process(
        started.session_id, outcome_class="quota_exhausted", retry_after=1800
    )

    advanced = mesh.reconcile_provider_outcome(started.session_id)

    assert advanced is not None and advanced.state == "active"
    task = store.get("operator", "audit")
    assert task.epoch == 2
    assert target_of(store)["auth_binding_id"] == "acct-b"
    assert target_of(store)["recovery"] == "rate_limited"
    # The provider's own reset was honoured, not the blunt capacity default.
    assert 0 < cooling("acct-a") <= 1800
    assert cooling("acct-b") == 0


def test_sd11_a_mid_run_auth_failure_reroutes_on_the_short_cooldown(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D11 (auth): the same observer covers a credential that expired while
    the worker was running, and picks the duration from the outcome class
    rather than from the mere fact of failure."""
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(routed_spec())
    mesh.client.fail_process(started.session_id, outcome_class="auth_expired")

    advanced = mesh.reconcile_provider_outcome(started.session_id)

    assert advanced is not None and advanced.state == "active"
    assert target_of(store)["auth_binding_id"] == "acct-b"
    assert 0 < cooling("acct-a") <= pool_core.SHORT_COOLDOWN


def test_sd11_a_failed_process_with_no_provider_signal_blocks_the_task(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D11 (contrast): a worker process that died with no typed provider
    outcome failed at its work, so the task blocks and the manager is woken.

    Two wrong answers are both ruled out here. Guessing a provider outcome from
    a process that reported none is the transcript-scraping bug in another
    costume, and returning quietly leaves a task that is neither running nor
    finished with nobody told about it.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(routed_spec())
    mesh.client.fail_process(started.session_id, status="killed", exit_code=137)

    settled = mesh.reconcile_provider_outcome(started.session_id)

    assert settled is not None and settled.state == "blocked"
    task = store.get("operator", "audit")
    assert task.epoch == 1 and "killed" in str(task.result)
    assert cooling("acct-a") == 0 and cooling("acct-b") == 0
    assert mesh.reconcile_provider_outcomes() == []


def test_sd11_a_failure_the_native_queue_will_retry_is_left_alone(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D11 (contrast): cdesktop's own durable recovery requeues a claimed
    command whose execution died, so that failure is one it is about to retry.

    Blocking it here would be unrecoverable, not merely noisy: `blocked` is not
    a legal predecessor of `active`, so a task settled out from under a live
    retry can never be put back. A provider refusal is different - no retry
    fixes one - so it is still recorded even with work queued.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(routed_spec())
    mesh.client.commands[started.session_id] = [
        {"id": "cmd-1", "state": "claimed", "body": "keep going"}
    ]
    mesh.client.fail_process(started.session_id)

    assert mesh.reconcile_provider_outcome(started.session_id) is None
    assert store.get("operator", "audit").state == "active"

    mesh.client.fail_process(started.session_id, outcome_class="quota_exhausted")
    advanced = mesh.reconcile_provider_outcome(started.session_id)

    assert advanced is not None and advanced.state == "active"
    assert target_of(store)["auth_binding_id"] == "acct-b"


def test_sd11_a_running_process_is_left_alone(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D11 (contrast): the observer settles stopped processes only. Acting on
    a running one would replace a worker that is still doing the work."""
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(routed_spec())
    mesh.client.fail_process(
        started.session_id, status="running", exit_code=None, outcome_class=None
    )

    assert mesh.reconcile_provider_outcome(started.session_id) is None
    assert store.get("operator", "audit").state == "active"


# ---------------------------------------------------------------- S-D12


def test_sd12_a_task_stranded_mid_replacement_is_resumed_by_the_sweep(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D12: a replacement epoch that was opened but never filled is finished
    by the sweep instead of stranding the task forever.

    ``prepare_replacement`` and the launch that fills it are two steps, and a
    failure in between leaves the task ``replacing`` - holding no live session
    for the session-keyed pass and carrying no fresh terminal outcome for the
    journal-keyed one. It was invisible to both, which made a transient
    executor error during failover a permanent stop.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    start_rejected(mesh, 429)
    # The replacement launch fails for a reason that carries no provider
    # meaning, so nothing marks the new epoch terminal and the task stops here.
    mesh.client.fail_launch(CdesktopError("PUT /task-launches failed: HTTP 503: down"))
    assert mesh.reconcile_provider_outcomes() == []
    stranded = store.get("operator", "audit")
    assert stranded.state == "replacing" and stranded.epoch == 2

    advanced = mesh.reconcile_provider_outcomes()

    assert [worker.state for worker in advanced] == ["active"]
    task = store.get("operator", "audit")
    # Resumed into the epoch that was already open, not a third one.
    assert task.epoch == 2 and task.state == "active"
    assert target_of(store)["auth_binding_id"] == "acct-b"


def test_sd12_a_replacement_epoch_that_already_ended_blocks_and_then_advances(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D12 (contrast): a ``replacing`` task whose new epoch already ended
    settles by blocking, and the ordinary advance path takes it from there.

    A crash between recording that epoch's outcome and blocking the task leaves
    exactly this shape. Resuming would refill an epoch that is already spent;
    advancing straight from ``replacing`` is a transition the store rejects.
    Blocking first is the one settlement that both reconcilers already
    understand, so the chain moves next tick through one code path.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    start_rejected(mesh, 429)
    mesh.client.fail_launch(CdesktopError("PUT /task-launches failed: HTTP 503: down"))
    assert mesh.reconcile_provider_outcomes() == []
    stranded = store.get("operator", "audit")
    assert stranded.state == "replacing"
    mesh.journal.mark_terminal(stranded.task_id, stranded.epoch, "rejected:400")

    assert [worker.state for worker in mesh.reconcile_provider_outcomes()] == ["blocked"]
    blocked = store.get("operator", "audit")
    assert blocked.epoch == 2 and "rejected:400" in str(blocked.result)

    # A definitive rejection is not a reroute, so it stays put from here.
    assert mesh.reconcile_provider_outcomes() == []
    assert store.get("operator", "audit").epoch == 2


def test_sd12_a_manual_replacement_is_left_to_the_human_who_started_it(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D12 (contrast): the sweep finishes only the replacements it opened.

    A manual ``replace(worker, prompt)`` carries a prompt that lives nowhere
    but the caller's hands. Resuming one here would quietly re-run the original
    work under a new epoch - a wrong answer that looks like a right one - and
    its failure is already visible to the human who ran the command.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    started = mesh.start(routed_spec())
    mesh.client.fail_launch(CdesktopError("PUT /task-launches failed: HTTP 503: down"))
    with pytest.raises(CdesktopError):
        mesh.replace("audit", "do only the second half")
    stranded = store.get("operator", "audit")
    assert stranded.state == "replacing" and "recovery" not in stranded.spec["target"]

    assert mesh.reconcile_provider_outcomes() == []
    assert mesh.reconcile_provider_outcome(started.session_id) is None
    assert store.get("operator", "audit").epoch == stranded.epoch

    # The human re-runs their own command, prompt and all, and it resumes the
    # epoch already open rather than burning another.
    resumed = mesh.replace("audit", "do only the second half")
    assert resumed.state == "active"
    assert store.get("operator", "audit").epoch == stranded.epoch


# ---------------------------------------------------------------- S-D13


def test_sd13_a_task_at_its_attempt_limit_stops_instead_of_retrying_forever(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D13: the attempt circuit breaker ends the sweep's interest in a task,
    once, rather than every tick forever.

    A terminal effect stays visible to the journal-keyed sweep for the life of
    the task, so a task whose replacements have run out was re-attempted on
    every single tick - each one a doomed `prepare_replacement`, logged and
    discarded. It has to stop, and it has to say so.
    """
    seed_pool(
        claude_account("acct-a"),
        claude_account("acct-b"),
        claude_account("acct-c"),
        claude_account("acct-d"),
    )
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    start_rejected(mesh, 429)
    # Every replacement is refused too, and there is always another eligible
    # account, so nothing but the attempt limit can stop the walk.
    for _ in range(4):
        mesh.client.reject_after("launch", 429)
        mesh.reconcile_provider_outcomes()

    task = store.get("operator", "audit")
    assert task.attempts == task.max_attempts and task.state == "blocked"
    assert cooling("acct-d") == 0
    launches = len(mesh.client.calls("managed_launch"))

    assert mesh.reconcile_provider_outcomes() == []
    assert len(mesh.client.calls("managed_launch")) == launches
    assert store.get("operator", "audit").attempts == task.attempts


# ---------------------------------------------------------------- S-D15


def test_sd15_one_task_that_cannot_advance_does_not_abort_the_sweep(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings, monkeypatch
) -> None:
    """S-D15: the sweep isolates every task, whatever the failure type.

    The routing lane's own errors - SuccessionError, ExecutionRoutingError,
    PoolError - are plain RuntimeErrors, so an except tuple naming only the
    cdesktop and task-store families let one of them escape the loop and skip
    every task queued behind it. A corrupt settings read or an unreadable pool
    file would take down failover for the whole fleet, one tick at a time.
    """
    seed_pool(
        claude_account("acct-a"), claude_account("acct-b"), claude_account("acct-c")
    )
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    for key in ("first", "second"):
        mesh.client.reject_after("launch", 429)
        with pytest.raises(BatchError):
            mesh.start(routed_spec(key=key))

    real_advance = sdk_module.advance_route_after_outcome
    calls: list[int] = []

    def advance(*args: object, **kwargs: object):
        calls.append(1)
        if len(calls) == 1:
            raise SuccessionError("pool state is unreadable this tick")
        return real_advance(*args, **kwargs)

    monkeypatch.setattr(sdk_module, "advance_route_after_outcome", advance)

    advanced = mesh.reconcile_provider_outcomes()

    assert len(calls) == 2
    assert [worker.state for worker in advanced] == ["active"]
    states = {key: store.get("operator", key).state for key in ("first", "second")}
    assert sorted(states.values()) == ["active", "blocked"]


def test_sd13_a_live_task_out_of_attempts_blocks_rather_than_hanging(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D13 (contrast): excluding a spent task from the sweep is not enough on
    its own.

    A task can reach its attempt limit and still be *running* - it is the
    replacement that ran out, not the work. When such a task is then refused
    mid-run, dropping it from the sweep would be a silent hang. It blocks with
    the reason instead, once, and the sweep filter keeps it from being retried
    after that.
    """
    seed_pool(
        claude_account("acct-a"), claude_account("acct-b"), claude_account("acct-c")
    )
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    start_rejected(mesh, 429)
    mesh.client.reject_after("launch", 429)
    mesh.reconcile_provider_outcomes()
    live = mesh.reconcile_provider_outcomes()[0]
    task = store.get("operator", "audit")
    assert task.state == "active" and task.attempts == task.max_attempts

    mesh.client.fail_process(live.session_id, outcome_class="quota_exhausted")
    settled = mesh.reconcile_provider_outcome(live.session_id)

    assert settled is not None and settled.state == "blocked"
    assert "circuit breaker" in str(store.get("operator", "audit").result)
    assert store.get("operator", "audit").epoch == task.epoch
    # And the account it was refused on is still cooled, spent attempts or not.
    assert cooling("acct-c") > 0


# ---------------------------------------------------------------- S-D14


def test_sd14_a_legacy_quota_outcome_still_reroutes_after_the_upgrade(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings
) -> None:
    """S-D14: a task written before the outcomes were typed still fails over.

    Pre-upgrade code spelled the capacity outcome ``quota``. An upgrade is
    exactly when a task is most likely to be caught mid-failover, so refusing
    to read the old spelling would strand the tasks the change most affects.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    mesh.start(routed_spec())
    task = store.get("operator", "audit")
    mesh.journal.mark_terminal(task.task_id, task.epoch, "quota")

    advanced = mesh.reconcile_provider_outcomes()

    assert [worker.state for worker in advanced] == ["active"]
    assert target_of(store)["auth_binding_id"] == "acct-b"
    assert target_of(store)["recovery"] == "rate_limited"


# ---------------------------------------------------------------- S-D10


def test_sd10_a_crash_between_cooling_and_the_journal_still_cools(
    mesh: SightMesh, store: TaskStore, pool_root, routing_settings, monkeypatch
) -> None:
    """S-D10: the exhausted account is cooled even if the process dies before
    the outcome reaches the journal.

    The two writes are to different stores and cannot be one transaction, so
    one of them has to be safe to lose. Recording the outcome first is the
    unsafe order: the reconcile that later reads a terminal outcome advances
    the task without ever checking whether its binding was cooled, so the
    account stays eligible forever and the chain walks straight back onto it.
    Cooling first is safe precisely because cooling is monotonic - a repeat
    after recovery is a no-op.
    """
    seed_pool(claude_account("acct-a"), claude_account("acct-b"))
    configure_chains(standard=(subscription("terra", "terra", "claude"),))

    def crash(*_args: object, **_kwargs: object):
        raise TaskStoreError("simulated crash after cooling")

    monkeypatch.setattr(mesh.journal, "mark_terminal", crash)
    mesh.client.reject_after("launch", 429)
    with pytest.raises(BatchError):
        mesh.start(routed_spec())

    task = store.get("operator", "audit")
    assert mesh.journal.get(task.task_id, task.epoch).outcome is None
    assert cooling("acct-a") > 0


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
