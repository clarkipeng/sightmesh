"""Unit tests for the liveness classifier and its policy resolution.

The detector is the one component in the kernel whose *wrong* answers are
expensive in both directions: a false positive re-wakes a manager (and,
historically, killed healthy work), a false negative leaves a fleet silently
wedged. These tests pin the exact evidence-to-cause mapping from
docs/liveness-spec.md and, just as importantly, the cases where the honest
answer is "I cannot tell".
"""

from __future__ import annotations

import json

import pytest

from sightmesh.liveness import (
    DEFAULT_APPROVAL_TIMEOUT,
    DEFAULT_PROGRESS_TIMEOUT,
    IDLE_UNREPORTED_GRACE,
    Budget,
    BudgetError,
    DetectionPolicy,
    DetectionPolicyError,
    Finding,
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
    """A healthy, just-active worker; each test perturbs one axis.

    ``observed=True`` is part of the baseline because it means "the executor
    answered the process read", which is the ordinary case. The tests that
    care about *not* having been answered set it back to ``False`` explicitly,
    so the difference between "we looked and saw silence" and "we could not
    look" is always visible at the call site.
    """
    base: dict[str, object] = {
        "last_activity_at": NOW,
        "observed": True,
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


def test_a_missing_process_without_a_typed_marker_is_never_lost() -> None:
    """The honesty rule. A process absent from a list read is indistinguishable
    from a partial read, and the incident record shows infrastructure failures
    recorded as result failures. No marker, no attribution."""
    vanished = evidence(process_alive=False)
    assert classify(vanished, now=NOW, policy=POLICY) != "lost"


def test_an_unreadable_executor_is_unknown_however_stale_the_task_looks() -> None:
    """Why: `observed` is the difference between "we looked and saw silence"
    and "we could not look". Without the distinction an executor outage
    rewrites every task in the fleet to `stalled` in one tick."""
    blind = evidence(last_activity_at=NOW - 100_000, observed=False)
    assert classify(blind, now=NOW, policy=POLICY) == "unknown"


def test_a_between_turns_process_is_judged_on_its_timestamp_not_its_pid() -> None:
    """Why: on the shipped client a coding-agent process is `running` only
    during a turn, so "no running process" is the normal state of every idle
    worker. A classifier that answered `unknown` there could never see cause 1
    at all - a worker that ends its turn and goes quiet forever would stay
    `live` and block its manager's cohort indefinitely."""
    between_turns = evidence(last_activity_at=NOW - 30, process_alive=False)
    assert classify(between_turns, now=NOW, policy=POLICY) == "live"

    wedged = evidence(last_activity_at=NOW - 1501, process_alive=False)
    assert classify(wedged, now=NOW, policy=POLICY) == "stalled"


def test_a_running_process_vetoes_a_stale_loss_marker() -> None:
    """Why: sessions accumulate process history. A `killed` row from a turn
    that ended hours ago used to mark a running task irreversibly `lost`, and
    a lost task gets replaced - two live workers on one branch. A process the
    executor says is running right now outranks any loss attribution."""
    working = evidence(lost_reason="killed", process_alive=True)
    assert classify(working, now=NOW, policy=POLICY) == "live"


def test_no_timestamp_from_any_source_is_unknown_not_stalled() -> None:
    """Degraded mode's defining case: on a client that exposes nothing useful,
    the detector must say so rather than declare every task stalled."""
    blind = evidence(last_activity_at=None)
    assert classify(blind, now=NOW, policy=POLICY) == "unknown"


def test_an_unmeasurable_output_counter_blocks_a_stall_verdict() -> None:
    """Why: the first tick after a restart has no byte baseline to compare
    against, so "no growth observed" is not a fact about the task. Reporting
    stalled there flagged a 5KB-per-tick emitter the moment the service came
    back."""
    restarted = evidence(last_activity_at=NOW - 1501, output_unmeasured=True)
    assert classify(restarted, now=NOW, policy=POLICY) == "unknown"


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


def test_an_absent_output_byte_field_is_absent_evidence_not_zero() -> None:
    """Why: no real cdesktop process row carries `output_bytes`. Summing the
    missing field to 0 invented a progress signal that only the test double
    had, so the timestamp path - the one production actually depends on - was
    never the thing under test."""
    executor = _Executor([{"id": "p1", "status": "running", "updated_at": NOW - 10}])
    gathered = gather_evidence(executor, "s1", now=NOW, previous_output_bytes=None)
    assert gathered.output_bytes is None
    assert gathered.output_unmeasured is False
    assert "output_bytes" not in gathered.sources


def test_a_failed_process_read_is_unobserved_rather_than_empty() -> None:
    """Why: `[]` from a broken endpoint and `[]` from a quiet session are the
    same bytes but not the same fact. Only the un-observed marking stops a
    partial read from being read back as proof of silence."""

    class Broken:
        def execution_processes(self, _session_id: str) -> list[dict[str, object]]:
            raise RuntimeError("executor unavailable")

    gathered = gather_evidence(Broken(), "s1", now=NOW)
    assert gathered.observed is False
    assert classify(gathered, now=NOW, policy=POLICY) == "unknown"


def test_a_failed_snapshot_read_leaves_the_stream_state_unknown() -> None:
    """Why: `stream_alive` defaulting to True on a failed read meant one flaky
    endpoint could flip a task between `stalled` and `limbo` on alternate
    ticks, and every flip used to mint a fresh episode - unbounded wakes from
    a single unreliable snapshot endpoint."""
    executor = _Executor([{"id": "p1", "status": "running", "updated_at": NOW - 9_000}])
    gathered = gather_evidence(executor, "s1", now=NOW)
    assert gathered.stream_alive is None
    assert classify(gathered, now=NOW, policy=POLICY) == "stalled"


def test_a_dropped_or_devserver_row_is_not_the_worker() -> None:
    """Why: a dev server's heartbeat is not the worker's progress and a dropped
    row's `killed` status is not the worker's death. Both filters are the
    established ones from `latest_execution_process`."""
    executor = _Executor(
        [
            {"id": "dev", "status": "running", "run_reason": "devserver", "updated_at": NOW},
            {
                "id": "old",
                "status": "killed",
                "exit_reason": "oom",
                "dropped": True,
                "updated_at": NOW,
            },
            {"id": "p1", "status": "running", "updated_at": NOW - 9_000},
        ]
    )
    gathered = gather_evidence(executor, "s1", now=NOW)
    assert gathered.lost_reason is None
    assert gathered.last_activity_at == NOW - 9_000
    assert classify(gathered, now=NOW, policy=POLICY) == "stalled"


def test_loss_evidence_comes_from_the_current_execution_only() -> None:
    """Why (the duplicate-session bug): a session keeps every process it ever
    ran. Scanning the whole history let one stale `killed` row mark a healthy
    RUNNING task `lost`, which is terminal, which makes the manager replace a
    worker that never stopped working."""
    executor = _Executor(
        [
            {"id": "old", "status": "killed", "exit_reason": "restart", "updated_at": NOW - 5_000},
            {"id": "new", "status": "running", "updated_at": NOW - 5},
        ]
    )
    gathered = gather_evidence(executor, "s1", now=NOW)
    assert gathered.lost_reason is None
    assert gathered.process_alive is True
    assert classify(gathered, now=NOW, policy=POLICY) == "live"


@pytest.mark.parametrize(
    "stamp",
    [NOW + 86_400, float("nan"), NOW * 1000, -1, "not-a-time"],
    ids=["far-future", "nan", "milliseconds", "negative", "text"],
)
def test_an_implausible_timestamp_is_absent_evidence_not_proof_of_life(stamp) -> None:
    """Why: `silent_for` floors the age at zero, so a future timestamp, a NaN,
    or an epoch-milliseconds value all read as "active one moment ago" - a
    permanent, silent exemption from detection for that task."""
    executor = _Executor([{"id": "p1", "status": "running", "updated_at": stamp}])
    gathered = gather_evidence(executor, "s1", now=NOW)
    assert gathered.last_activity_at is None
    assert classify(gathered, now=NOW, policy=POLICY) == "unknown"


@pytest.mark.parametrize(
    "processes",
    [
        [None, "junk", 5],
        [{"id": "p1", "status": "running", "updated_at": NOW, "output_bytes": "lots"}],
        [{"id": "p1", "status": "running", "updated_at": NOW, "output_bytes": float("nan")}],
        "not-a-list",
        None,
    ],
    ids=["mixed-junk", "text-bytes", "nan-bytes", "not-a-list", "null"],
)
def test_a_hostile_process_payload_degrades_the_finding_instead_of_raising(
    processes,
) -> None:
    """Why: `gather_evidence` runs once per task inside one shared reconciler
    pass. A TypeError from one task's malformed payload used to escape the
    pass entirely, starving wake delivery and reservation expiry for every
    other task in the fleet."""
    executor = _Executor(processes)
    gathered = gather_evidence(executor, "s1", now=NOW)
    assert classify(gathered, now=NOW, policy=POLICY) in {"unknown", "live"}


def test_a_hostile_snapshot_payload_never_raises_through_the_pass() -> None:
    """Why: same isolation rule one layer down. `entries` is executor-supplied
    and has arrived as null and as a bare integer."""
    for entries in (None, 5, [None], [{"content": 7}]):
        executor = _Executor(
            [{"id": "p1", "status": "running", "updated_at": NOW - 10}],
            p1={"entries": entries, "stream_alive": True},
        )
        assert gather_evidence(executor, "s1", now=NOW).tokens == 0


def test_a_nonfinite_number_never_reaches_the_wake_payload() -> None:
    """Why: json.dumps emits bare NaN/Infinity by default, which no strict
    JSON parser downstream accepts. The payload is what a manager reads."""
    finding = Finding(
        reason="stalled",
        evidence=ProgressEvidence(observed=True, cost=float("nan")),
        now=NOW,
    )
    assert json.loads(finding.payload())["cost"] is None


# ----------------------------------------------------------------------
# Stored policy: poison in the row must not raise through a fleet pass
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        {"progress_timeout": 0},
        {"progress_timeout": -1},
        {"progress_timeout": "soon"},
        {"progress_timeout": float("nan")},
        {"progress_timeout": 600, "approval_timeout": 0},
        {"progress_timeout": 600, "budget": {"max_turns": -3}},
        {"progress_timeout": 600, "budget": "generous"},
        "not-a-policy",
    ],
)
def test_a_poisoned_stored_policy_is_refused_on_read(stored) -> None:
    """Why: a stored `progress_timeout` of 0 makes `silent >= timeout` true on
    the first tick of every task forever. Refusing it on read - as a typed
    error the caller turns into one attention item - is the only outcome that
    is neither a silent fleet-wide false positive nor a raise that kills the
    pass."""
    with pytest.raises(DetectionPolicyError):
        DetectionPolicy.from_dict(stored)


def test_a_readable_stored_policy_round_trips() -> None:
    """The control case: refusing poison is worthless if it also refuses the
    policy the SDK actually writes."""
    policy = DetectionPolicy(
        progress_timeout=600.0, approval_timeout=300.0, budget=Budget(max_turns=20)
    )
    assert DetectionPolicy.from_dict(policy.to_dict()) == policy


def test_a_worker_cannot_lengthen_its_own_approval_timeout() -> None:
    """Why: the trusted floor left `approval_timeout` unset, and the smaller of
    "unset" and a worker's requested ten days is ten days - an approval nobody
    is ever told about, which is the state the 30-minute reaper existed to
    paper over."""
    resolved = resolve_policy(
        progress_timeout=None,
        approval_timeout=864_000.0,
        budget=None,
        trusted=trusted_policy({}),
    )
    assert resolved.effective_approval_timeout == DEFAULT_APPROVAL_TIMEOUT
