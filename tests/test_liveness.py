"""Unit tests for the liveness classifier and its policy resolution.

The detector is the one component in the kernel whose *wrong* answers are
expensive in both directions: a false positive re-wakes a manager (and,
historically, killed healthy work), a false negative leaves a fleet silently
wedged. These tests pin the exact evidence-to-cause mapping from
docs/liveness-spec.md and, just as importantly, the cases where the honest
answer is "I cannot tell".
"""

from __future__ import annotations

import pytest

from sightmesh.liveness import (
    DEFAULT_APPROVAL_TIMEOUT,
    DEFAULT_PROGRESS_TIMEOUT,
    IDLE_UNREPORTED_GRACE,
    Budget,
    BudgetError,
    DetectionPolicy,
    ProgressEvidence,
    classify,
    gather_evidence,
    over_budget,
    resolve_policy,
    trusted_policy,
)

NOW = 1_000_000.0
POLICY = DetectionPolicy(progress_timeout=1500.0)


def evidence(**kwargs: object) -> ProgressEvidence:
    """A healthy, just-active worker; each test perturbs one axis."""
    base: dict[str, object] = {
        "last_activity_at": NOW,
        "process_alive": True,
        "stream_alive": True,
    }
    base.update(kwargs)
    return ProgressEvidence(**base)  # type: ignore[arg-type]


def test_a_recently_active_process_is_live() -> None:
    """The control case. Without it, a test suite full of stall assertions
    could pass against a classifier that calls everything stalled."""
    assert classify(evidence(), now=NOW, policy=POLICY) == "live"


def test_silence_past_the_progress_timeout_is_stalled() -> None:
    """Cause 6 of the table and the detector's core job: a live process that
    has produced no evidence of any kind for progress_timeout is stalled."""
    stale = evidence(last_activity_at=NOW - 1501)
    assert classify(stale, now=NOW, policy=POLICY) == "stalled"


def test_silence_inside_the_progress_timeout_is_not_yet_stalled() -> None:
    """The boundary matters more than the middle: an off-by-one here turns
    every ordinary long tool call into a spurious manager wake."""
    assert classify(evidence(last_activity_at=NOW - 1499), now=NOW, policy=POLICY) == "live"


def test_a_turn_that_ended_without_a_lifecycle_call_is_idle_unreported() -> None:
    """Cause 1, the single most common historical stall: the model finished
    its turn and simply never called complete/blocked/checkpoint, so nothing
    in the system knows the task is over."""
    ended = evidence(
        last_activity_at=NOW - IDLE_UNREPORTED_GRACE - 1, turn_ended=True
    )
    assert classify(ended, now=NOW, policy=POLICY) == "idle_unreported"


def test_a_just_ended_turn_is_given_its_grace() -> None:
    """The lifecycle call routinely lands a beat after the turn ends. Flagging
    inside the grace would wake a manager about a task that is about to report
    itself perfectly normally."""
    ended = evidence(last_activity_at=NOW - 5, turn_ended=True)
    assert classify(ended, now=NOW, policy=POLICY) == "live"


def test_a_turn_that_ended_with_queued_mail_is_not_idle() -> None:
    """Cause 1 requires "no pending queued mail": a worker with mail waiting is
    about to be dispatched again, so its ended turn is a pause, not silence."""
    ended = evidence(
        last_activity_at=NOW - 600, turn_ended=True, queued_mail=True
    )
    assert classify(ended, now=NOW, policy=POLICY) == "live"


def test_a_turn_that_ended_and_reported_itself_is_not_idle() -> None:
    """The whole cause is *unreported*. A turn that made its lifecycle call did
    exactly what it was supposed to."""
    ended = evidence(
        last_activity_at=NOW - 600, turn_ended=True, lifecycle_called=True
    )
    assert classify(ended, now=NOW, policy=POLICY) == "live"


def test_a_dead_stream_over_a_live_process_becomes_limbo() -> None:
    """Cause 3: the stream died but the process did not. Reported only after a
    full progress_timeout, because the executor gets that whole window to
    re-attach through its durable handle before anyone is woken."""
    stranded = evidence(last_activity_at=NOW - 1501, stream_alive=False)
    assert classify(stranded, now=NOW, policy=POLICY) == "limbo"


def test_a_briefly_dead_stream_is_not_yet_limbo() -> None:
    """Stream flaps are ordinary. Waking a manager for one the executor
    re-attaches a second later is pure noise."""
    flap = evidence(last_activity_at=NOW - 10, stream_alive=False)
    assert classify(flap, now=NOW, policy=POLICY) == "live"


def test_growing_output_is_progress_no_matter_how_stale_the_timestamps() -> None:
    """Cause 3's rule, stated positively: "live process emitting output bytes =
    progress". A four-hour build with no transcript activity is working, and
    the classifier must never need a timestamp to see that."""
    grinding = evidence(last_activity_at=NOW - 100_000, output_growing=True)
    assert classify(grinding, now=NOW, policy=POLICY) == "live"


def test_a_parked_approval_is_excluded_from_stall_detection() -> None:
    """Cause 2. A task waiting on a human is the system working correctly; the
    30-minute SIGKILL of exactly this state is the incident the spec bans."""
    waiting = evidence(last_activity_at=NOW - 100_000, parked=True)
    assert classify(waiting, now=NOW, policy=POLICY) == "parked"


def test_a_typed_loss_marker_outranks_every_other_signal() -> None:
    """Cause 4. Once the executor says the process died and why, no amount of
    stale-timestamp reasoning should relabel it as a stall - "killed without a
    terminal" and "went quiet" call for opposite manager responses."""
    dead = evidence(process_alive=False, lost_reason="restart", parked=True)
    assert classify(dead, now=NOW, policy=POLICY) == "lost"


def test_a_missing_process_without_a_typed_marker_is_unknown_not_lost() -> None:
    """The honesty rule. A process absent from a list read is indistinguishable
    from a partial read, and the incident record shows infrastructure failures
    recorded as result failures. No marker, no attribution."""
    vanished = evidence(process_alive=False)
    assert classify(vanished, now=NOW, policy=POLICY) == "unknown"


def test_no_timestamp_from_any_source_is_unknown_not_stalled() -> None:
    """Degraded mode's defining case: on a client that exposes nothing useful,
    the detector must say so rather than declare every task stalled."""
    blind = evidence(last_activity_at=None)
    assert classify(blind, now=NOW, policy=POLICY) == "unknown"


def test_a_tighter_policy_flags_sooner() -> None:
    """progress_timeout has to actually reach the classifier; a hard-coded
    constant would silently ignore every per-task and per-profile setting."""
    quiet = evidence(last_activity_at=NOW - 100)
    assert classify(quiet, now=NOW, policy=DetectionPolicy(progress_timeout=60)) == "stalled"


# ----------------------------------------------------------------------
# Budgets: evidence, never enforcement
# ----------------------------------------------------------------------


def test_budget_flags_when_a_measured_axis_is_crossed() -> None:
    """Cause 5's signal. The flag is all the kernel does - the manager judges
    whether grinding is converging, because no heuristic can."""
    assert over_budget(evidence(turns=40), Budget(max_turns=40))
    assert over_budget(evidence(tokens=900_000), Budget(max_tokens=500_000))


def test_budget_never_flags_an_axis_nobody_measured() -> None:
    """The current client reports no cost. A ceiling cannot be enforced against
    a number that does not exist, and pretending otherwise would flag every
    task with a cost budget on day one."""
    assert not over_budget(evidence(turns=1, cost=None), Budget(max_cost=5.0))


def test_a_task_under_every_ceiling_is_not_flagged() -> None:
    """Guards against an inverted comparison, which would flag the whole
    fleet and make the predicate worthless."""
    assert not over_budget(evidence(turns=1, tokens=10), Budget(max_turns=40, max_tokens=100))


def test_a_nonpositive_budget_is_rejected_at_construction() -> None:
    """A zero or negative ceiling is crossed by definition on the first turn.
    Failing at construction beats shipping a config that wakes a manager
    forever."""
    with pytest.raises(BudgetError):
        Budget(max_turns=0)


# ----------------------------------------------------------------------
# Policy resolution: workers cannot weaken their own detection
# ----------------------------------------------------------------------


def test_a_worker_cannot_lengthen_its_own_progress_timeout() -> None:
    """The security property of the whole feature. If a worker could ask for a
    24-hour timeout, any wedged or misbehaving child could opt out of being
    detected at all."""
    trusted = DetectionPolicy(progress_timeout=600.0)
    resolved = resolve_policy(
        progress_timeout=86_400.0, approval_timeout=None, budget=None, trusted=trusted
    )
    assert resolved.progress_timeout == 600.0


def test_a_worker_may_tighten_its_own_progress_timeout() -> None:
    """Tightening is always safe, and a task that knows it should be chatty
    every minute is better monitored than the floor allows."""
    trusted = DetectionPolicy(progress_timeout=600.0)
    resolved = resolve_policy(
        progress_timeout=60.0, approval_timeout=None, budget=None, trusted=trusted
    )
    assert resolved.progress_timeout == 60.0


def test_a_worker_cannot_enlarge_a_trusted_budget() -> None:
    """Same rule on the budget axis, which the spec calls out explicitly
    ("same rule as child budgets")."""
    trusted = DetectionPolicy(progress_timeout=600.0, budget=Budget(max_turns=20))
    resolved = resolve_policy(
        progress_timeout=None,
        approval_timeout=None,
        budget=Budget(max_turns=10_000, max_tokens=50),
        trusted=trusted,
    )
    assert resolved.budget == Budget(max_turns=20, max_tokens=50)


def test_an_unset_approval_timeout_inherits_the_profile_policy() -> None:
    """`None` means inherit, not "no timeout"; an approval that never times out
    is an approval nobody is ever told about."""
    resolved = resolve_policy(
        progress_timeout=None,
        approval_timeout=None,
        budget=None,
        trusted=DetectionPolicy(progress_timeout=600.0),
    )
    assert resolved.approval_timeout is None
    assert resolved.effective_approval_timeout == DEFAULT_APPROVAL_TIMEOUT


def test_the_trusted_floor_defaults_to_the_specified_twenty_five_minutes() -> None:
    """The spec fixes the default at 1500s. A drifting default silently changes
    detection behavior across the whole fleet."""
    assert trusted_policy({}).progress_timeout == DEFAULT_PROGRESS_TIMEOUT == 1500.0


def test_a_malformed_environment_override_falls_back_instead_of_disabling() -> None:
    """Bad operator config must never turn detection off. A zero or unparsable
    timeout would otherwise make every task instantly stalled or never."""
    assert trusted_policy({"SIGHTMESH_PROGRESS_TIMEOUT_SECONDS": "-5"}).progress_timeout == 1500.0
    assert trusted_policy({"SIGHTMESH_PROGRESS_TIMEOUT_SECONDS": "abc"}).progress_timeout == 1500.0


# ----------------------------------------------------------------------
# Evidence gathering against a partly-broken executor
# ----------------------------------------------------------------------


class _Executor:
    """A client that answers some reads and fails others, as a real one does."""

    def __init__(self, processes: list[dict[str, object]], **snapshots: object) -> None:
        self._processes = processes
        self._snapshots = snapshots

    def execution_processes(self, _session_id: str) -> list[dict[str, object]]:
        return self._processes

    def normalized_snapshot(self, process_id: str) -> object:
        if process_id not in self._snapshots:
            raise RuntimeError("snapshot unavailable")
        return self._snapshots[process_id]


def test_a_failing_snapshot_read_degrades_the_finding_instead_of_the_pass() -> None:
    """The detector runs inside a shared reconciler tick. One endpoint erroring
    must cost that task its evidence, not cost every other task its pass."""
    executor = _Executor([{"id": "p1", "status": "running", "updated_at": NOW - 10}])
    gathered = gather_evidence(executor, "s1", now=NOW)
    assert gathered.process_alive
    assert gathered.last_activity_at == NOW - 10
    assert gathered.confidence == "degraded"


def test_typed_markers_upgrade_the_reported_confidence() -> None:
    """Confidence is not decoration: it travels into the wake payload so a
    manager can tell "cdesktop says the turn ended" from "we inferred it"."""
    executor = _Executor(
        [{"id": "p1", "status": "running", "updated_at": NOW - 10}],
        p1={"entries": [], "stream_alive": True, "turn_ended": True},
    )
    gathered = gather_evidence(executor, "s1", now=NOW)
    assert gathered.turn_ended
    assert gathered.confidence == "typed"


def test_a_checkpoint_counts_as_progress_evidence() -> None:
    """`checkpoint()` is in the spec's evidence list, and it is the only entry
    the kernel owns rather than reads from the executor - a worker that
    checkpoints regularly is provably working."""
    executor = _Executor([{"id": "p1", "status": "running", "updated_at": NOW - 9_000}])
    gathered = gather_evidence(executor, "s1", now=NOW, checkpoint_at=NOW - 5)
    assert gathered.last_activity_at == NOW - 5
    assert classify(gathered, now=NOW, policy=POLICY) == "live"


def test_output_growth_is_measured_against_the_previous_tick() -> None:
    """S16's mechanism. Growth is a difference, so the detector has to carry
    the previous total; without it a silent-but-working command looks stalled."""
    executor = _Executor(
        [{"id": "p1", "status": "running", "updated_at": NOW - 9_000, "output_bytes": 4_096}]
    )
    assert not gather_evidence(executor, "s1", now=NOW, previous_output_bytes=4_096).output_growing
    assert gather_evidence(executor, "s1", now=NOW, previous_output_bytes=10).output_growing
