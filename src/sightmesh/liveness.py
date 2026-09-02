"""Progress evidence and the stall classifier (docs/liveness-spec.md).

This module holds the pure half of the liveness detector: read cheap,
metadata-only evidence from the executor, and turn it into one typed
classification. It never writes, never launches, and never stops anything -
:mod:`sightmesh.durable` owns the single reconciler pass that persists the
finding, and the owning manager owns every replace/cancel decision. Wall-clock
kill timers are banned by the contract; nothing here has the power to break
that rule even by accident.

Two properties are load-bearing:

*Absence of evidence is not evidence.* When the executor cannot tell us
anything - no timestamps, a process it has lost track of, a snapshot it will
not serve - the classifier answers ``unknown``, which arms nothing and closes
nothing. The alternative, defaulting to "stalled", is how automatic reapers
historically killed healthy work.

*Confidence is reported, never assumed.* cdesktop 0.2.6/0.2.7 exposes process
timestamps, process status, and normalized snapshots, but none of the typed
``turn_ended`` / ``parked`` / ``limbo`` / restart markers the Phase 4 seam
adds. Until then every finding carries ``confidence="degraded"`` and the
sources it was actually derived from, and that string travels into the wake
payload the manager reads.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger("sightmesh.liveness")

#: Seconds without any progress evidence before a live task is ``stalled``.
DEFAULT_PROGRESS_TIMEOUT = 1500.0
#: Cause 1's grace: a turn can end a couple of minutes before its lifecycle
#: call lands without that being a fault.
IDLE_UNREPORTED_GRACE = 120.0
#: ``None`` means "inherit the profile policy"; this is the fallback when no
#: policy names one. A parked approval never dies of it - it becomes
#: ``blocked(approval)`` plus a human attention item.
DEFAULT_APPROVAL_TIMEOUT = 1800.0
#: How far a supplied timestamp may lead our own clock before it is refused.
#: Small clock skew between the executor and the kernel is ordinary; a
#: timestamp minutes in the future is a unit or clock bug, and treating it as
#: "just active" is a permanent bypass of the whole detector.
MAX_CLOCK_SKEW_SECONDS = 300.0
#: Seconds-since-epoch values above this are not seconds. ``1e11`` is the year
#: 5138 read as seconds and 1973 read as milliseconds, so anything at or above
#: it is a unit error rather than a date.
IMPLAUSIBLE_TIMESTAMP = 1e11

PROGRESS_TIMEOUT_ENV = "SIGHTMESH_PROGRESS_TIMEOUT_SECONDS"
APPROVAL_TIMEOUT_ENV = "SIGHTMESH_APPROVAL_TIMEOUT_SECONDS"
MAX_TURNS_ENV = "SIGHTMESH_MAX_TURNS"
MAX_TOKENS_ENV = "SIGHTMESH_MAX_TOKENS"
MAX_COST_ENV = "SIGHTMESH_MAX_COST"

#: Classifications that arm nothing. ``live`` means progress was *observed*
#: and closes an open episode; ``unknown`` means nothing was observed either
#: way and leaves the row exactly as it found it.
INERT = frozenset({"live", "unknown"})


class BudgetError(ValueError):
    """A budget that cannot be satisfied by any run."""


class DetectionPolicyError(ValueError):
    """A stored detection policy no classification can be trusted against.

    Raised on read, never on the hot path of a classification: a task whose
    policy row is malformed becomes an ``unknown`` finding plus a human
    attention item, because a zero or negative ``progress_timeout`` would
    otherwise make every silence instantly, permanently ``stalled``.
    """


@dataclass(frozen=True)
class Budget:
    """Resource ceilings recorded as *evidence*, never as enforcement.

    Crossing one flags the task and wakes its manager; it never stops a
    process and never fails a task. "Grinding without converging" is a
    judgment only a manager can make (liveness-spec.md, cause 5), and the
    kernel deliberately declines to make it with a heuristic.
    """

    max_turns: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_turns", "max_tokens", "max_cost"):
            value = getattr(self, name)
            if value is None:
                continue
            if not _is_positive_number(value):
                raise BudgetError(f"Budget {name} must be positive, not {value!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in (
                ("max_turns", self.max_turns),
                ("max_tokens", self.max_tokens),
                ("max_cost", self.max_cost),
            )
            if value is not None
        }

    @classmethod
    def from_dict(cls, value: Any) -> Budget | None:
        if not isinstance(value, dict) or not value:
            return None
        return cls(
            max_turns=value.get("max_turns"),
            max_tokens=value.get("max_tokens"),
            max_cost=value.get("max_cost"),
        )

    def tightest(self, other: Budget | None) -> Budget:
        """Combine two budgets by taking the smaller ceiling on every axis.

        This is the "workers cannot weaken their own detection" rule: a spec
        supplied by a worker can only ever narrow the trusted ceiling, so a
        compromised or over-eager child cannot buy itself an unlimited run.
        """
        if other is None:
            return self
        return Budget(
            max_turns=_smaller(self.max_turns, other.max_turns),
            max_tokens=_smaller(self.max_tokens, other.max_tokens),
            max_cost=_smaller(self.max_cost, other.max_cost),
        )


@dataclass(frozen=True)
class DetectionPolicy:
    """The resolved, trusted detection settings for one task.

    Both timeouts are validated at construction. A stored ``0`` or ``-1``
    ``progress_timeout`` would make ``silent >= progress_timeout`` true on the
    first tick of every task forever, so an unusable value is refused here
    rather than quietly flagging the fleet.
    """

    progress_timeout: float = DEFAULT_PROGRESS_TIMEOUT
    approval_timeout: float | None = None
    budget: Budget | None = None

    def __post_init__(self) -> None:
        if not _is_positive_number(self.progress_timeout):
            raise DetectionPolicyError(
                f"progress_timeout must be a positive number of seconds, "
                f"not {self.progress_timeout!r}"
            )
        if self.approval_timeout is not None and not _is_positive_number(
            self.approval_timeout
        ):
            raise DetectionPolicyError(
                f"approval_timeout must be a positive number of seconds, "
                f"not {self.approval_timeout!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"progress_timeout": self.progress_timeout}
        if self.approval_timeout is not None:
            payload["approval_timeout"] = self.approval_timeout
        if self.budget is not None:
            payload["budget"] = self.budget.to_dict()
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> DetectionPolicy:
        """Rebuild a stored policy, refusing anything unusable.

        The task row is durable and hand-editable, and a poisoned
        ``spec_json`` must not be able to raise through a fleet-wide detector
        pass. Callers catch :class:`DetectionPolicyError` and turn it into one
        attention item for that task.
        """
        if not isinstance(value, dict):
            raise DetectionPolicyError(
                f"Detection policy must be an object, not {type(value).__name__}"
            )
        raw_budget = value.get("budget")
        try:
            budget = Budget.from_dict(raw_budget)
        except BudgetError as exc:
            raise DetectionPolicyError(str(exc)) from exc
        if raw_budget is not None and budget is None and raw_budget != {}:
            raise DetectionPolicyError(f"Unreadable budget: {raw_budget!r}")
        return cls(
            progress_timeout=_required_positive(
                value.get("progress_timeout", DEFAULT_PROGRESS_TIMEOUT),
                "progress_timeout",
            ),
            approval_timeout=(
                None
                if value.get("approval_timeout") is None
                else _required_positive(value["approval_timeout"], "approval_timeout")
            ),
            budget=budget,
        )

    @property
    def effective_approval_timeout(self) -> float:
        return (
            DEFAULT_APPROVAL_TIMEOUT
            if self.approval_timeout is None
            else self.approval_timeout
        )


@dataclass(frozen=True)
class ProgressEvidence:
    """Everything the detector is allowed to reason from, and where it came from.

    Metadata only: process rows, normalized snapshots, and the task's own
    checkpoint time. No Git, no worktree read, no transcript body.
    """

    last_activity_at: float | None = None
    #: True only when the executor answered the process read with a usable,
    #: non-empty list. False covers both a failed read and a session the
    #: executor reports nothing about, which are indistinguishable and equally
    #: uninformative - no negative verdict may rest on either.
    observed: bool = False
    process_alive: bool = False
    #: ``None`` means the snapshot could not be read. A failed read used to
    #: default to ``True``, which let one flaky endpoint flip a task between
    #: ``stalled`` and ``limbo`` on alternate ticks.
    stream_alive: bool | None = None
    output_growing: bool = False
    #: The executor reports output bytes but this tick has no comparable
    #: baseline (first observation, or a restart). Growth is unmeasured, so
    #: silence is unproven and the honest answer is ``unknown``.
    output_unmeasured: bool = False
    parked: bool = False
    turn_ended: bool = False
    lifecycle_called: bool = False
    queued_mail: bool = False
    lost_reason: str | None = None
    turns: int = 0
    tokens: int = 0
    cost: float | None = None
    #: ``None`` means the executor's process rows carry no ``output_bytes``
    #: field at all - the shape every real cdesktop row has today. Absence is
    #: not zero: a zero would manufacture "no growth" evidence out of nothing.
    output_bytes: int | None = None
    confidence: str = "degraded"
    sources: tuple[str, ...] = field(default_factory=tuple)

    def silent_for(self, now: float) -> float | None:
        if self.last_activity_at is None:
            return None
        return max(0.0, now - self.last_activity_at)

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "sources": list(self.sources),
            "silent_for": _finite(self.silent_for(now)),
            "observed": self.observed,
            "process_alive": self.process_alive,
            "stream_alive": self.stream_alive,
            "output_growing": self.output_growing,
            "turns": self.turns,
            "tokens": self.tokens,
            "cost": _finite(self.cost),
        }


@dataclass(frozen=True)
class Finding:
    """One classification plus the evidence that justifies it."""

    reason: str
    evidence: ProgressEvidence
    now: float
    over_budget: bool = False

    @property
    def actionable(self) -> bool:
        return self.reason not in INERT

    def payload(self) -> str:
        """Render the evidence as JSON a manager - and a reader - can trust.

        ``allow_nan=False`` because Python's default emits bare ``NaN`` and
        ``Infinity`` tokens, which are not JSON: one non-finite number from a
        hostile or buggy executor would produce a payload that every strict
        parser downstream rejects. Values are sanitized to ``None`` first, so
        the flag can only ever be a tripwire, never a raise on the hot path.
        """
        body = self.evidence.to_dict(self.now)
        body["reason"] = self.reason
        if self.over_budget:
            body["over_budget"] = True
        return json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        )


def trusted_policy(environment: dict[str, str] | None = None) -> DetectionPolicy:
    """The manager-side detection floor a worker's own spec cannot loosen.

    Sourced from the manager's process environment, which is where trusted
    profile and manager configuration land today. A worker's ``WorkerSpec``
    can only tighten what this returns (see :func:`resolve_policy`).
    """
    env = os.environ if environment is None else environment
    ceilings = {
        "max_turns": _positive_int_or_none(env.get(MAX_TURNS_ENV), MAX_TURNS_ENV),
        "max_tokens": _positive_int_or_none(env.get(MAX_TOKENS_ENV), MAX_TOKENS_ENV),
        "max_cost": _positive_float_or_none(env.get(MAX_COST_ENV), MAX_COST_ENV),
    }
    return DetectionPolicy(
        progress_timeout=_positive_float(
            env.get(PROGRESS_TIMEOUT_ENV), DEFAULT_PROGRESS_TIMEOUT, PROGRESS_TIMEOUT_ENV
        ),
        # Every axis of the floor carries a value, including the approval
        # timeout. Leaving it ``None`` made the floor unenforceable on that
        # axis: ``resolve_policy`` takes the smaller of the two, and the
        # smaller of "nothing" and a worker's requested ten days is ten days.
        approval_timeout=_positive_float(
            env.get(APPROVAL_TIMEOUT_ENV),
            DEFAULT_APPROVAL_TIMEOUT,
            APPROVAL_TIMEOUT_ENV,
        ),
        # Budget ceilings are opt-in by design: a fleet-wide default turn or
        # token cap would flag every long-running task the day it shipped,
        # and a budget is evidence rather than enforcement. Operators set the
        # floor through the environment; a worker can then only tighten it.
        budget=Budget(**ceilings) if any(value for value in ceilings.values()) else None,
    )


def resolve_policy(
    *,
    progress_timeout: float | None,
    approval_timeout: float | None,
    budget: Budget | None,
    trusted: DetectionPolicy | None = None,
) -> DetectionPolicy:
    """Merge a requested spec with the trusted floor, always tightening.

    Detection is a property of the fleet, not a favour a worker grants: every
    axis resolves to the *stricter* of the two, so a spec asking for a longer
    progress timeout, a longer approval timeout, or a larger budget than the
    manager allows is silently narrowed instead of honoured.
    """
    floor = trusted_policy() if trusted is None else trusted
    requested_progress = (
        float(progress_timeout)
        if _is_positive_number(progress_timeout)
        else floor.progress_timeout
    )
    requested_approval = (
        float(approval_timeout) if _is_positive_number(approval_timeout) else None
    )
    if progress_timeout is not None and not _is_positive_number(progress_timeout):
        raise DetectionPolicyError(
            f"progress_timeout must be a positive number of seconds, "
            f"not {progress_timeout!r}"
        )
    if approval_timeout is not None and requested_approval is None:
        raise DetectionPolicyError(
            f"approval_timeout must be a positive number of seconds, "
            f"not {approval_timeout!r}"
        )
    return DetectionPolicy(
        progress_timeout=min(floor.progress_timeout, requested_progress),
        approval_timeout=_smaller(floor.approval_timeout, requested_approval),
        budget=budget.tightest(floor.budget) if budget else floor.budget,
    )


def classify(
    evidence: ProgressEvidence, *, now: float, policy: DetectionPolicy
) -> str:
    """Map progress evidence onto one typed cause from the liveness table.

    Ordered by how definitive the signal is, most definitive first, so a
    weaker inference can never overrule a typed fact:

    1. a typed loss marker over a process that is *not* running is terminal;
    2. a parked approval is excluded from stall detection by contract;
    3. measured output growth is progress, however long the command runs;
    4. no timestamp from any source is ``unknown``, never a verdict;
    5. a turn that ended with no lifecycle call and no queued mail is
       ``idle_unreported`` once the grace lapses;
    6. silence inside ``progress_timeout`` is ``live``;
    7. past it, an unobserved or unmeasurable task is ``unknown``, a dead
       stream over a live process is ``limbo``, and everything else is
       ``stalled``.

    Two vetoes make the difference between this and a fleet-wrecker.

    *A running process vetoes ``lost``.* Loss evidence is read from the
    current execution only, but a stale ``killed`` row that survived that
    filter still cannot outrank a process the executor says is running right
    now: a live task marked irreversibly lost gets replaced, and the fleet
    ends up with two workers on one branch.

    *A process that is merely not running does not veto ``stalled``.* On the
    current client a coding-agent process is ``running`` only during a turn,
    so "no running process" is the ordinary between-turns shape and is the
    single most important thing degraded mode has to be able to judge. What
    it needs is a *timestamp*, not a live pid; what it must never do is judge
    a task the executor said nothing about at all (``observed``).
    """
    if evidence.lost_reason and not evidence.process_alive:
        return "lost"
    if evidence.parked:
        return "parked"
    if evidence.output_growing:
        return "live"
    silent = evidence.silent_for(now)
    if silent is None:
        # No timestamp from any source. The executor is not telling us whether
        # this task is working or wedged, and inventing an answer is exactly
        # the failure this spec exists to prevent.
        return "unknown"
    if evidence.turn_ended and not evidence.lifecycle_called and not evidence.queued_mail:
        if silent < IDLE_UNREPORTED_GRACE:
            return "live"
        return "idle_unreported" if evidence.observed else "unknown"
    if silent < policy.progress_timeout:
        return "live"
    if not evidence.observed or evidence.output_unmeasured:
        # Past the timeout, but nothing here is a *reading*: either the
        # executor told us nothing, or it reports an output-byte counter this
        # tick cannot compare against. Silence is unproven, so nothing is said.
        return "unknown"
    if evidence.stream_alive is False and evidence.process_alive:
        # Cause 3: the executor re-attaches through its durable handle, so
        # limbo is only *reported* once it has stayed unattachable for a full
        # progress_timeout. In degraded mode there is no attach signal to read,
        # so continued silence is the honest proxy for "unattachable".
        return "limbo"
    return "stalled"


def over_budget(evidence: ProgressEvidence, budget: Budget | None) -> bool:
    """Whether observed usage crossed any recorded ceiling.

    An axis the executor does not report (cost, on the current client) is
    never treated as crossed; a budget cannot be enforced against a number
    nobody measured.
    """
    if budget is None:
        return False
    if budget.max_turns is not None and evidence.turns >= budget.max_turns:
        return True
    if budget.max_tokens is not None and evidence.tokens >= budget.max_tokens:
        return True
    return (
        budget.max_cost is not None
        and evidence.cost is not None
        and evidence.cost >= budget.max_cost
    )


def gather_evidence(
    client: Any,
    session_id: str,
    *,
    now: float,
    checkpoint_at: float | None = None,
    previous_output_bytes: int | None = None,
) -> ProgressEvidence:
    """Read progress evidence from the executor's metadata endpoints only.

    Every read here is a list or a snapshot lookup cdesktop answers from its
    own database - no workspace refresh, no Git fan-out, per the contract's
    "Workspace lists are metadata-only". Typed markers are consumed when the
    executor offers them and simply absent otherwise, which is what lets the
    same code leave degraded mode when the Phase 4 seam lands rather than
    needing a rewrite.
    """
    sources: list[str] = []
    rows = _eligible_processes(_safe(lambda: client.execution_processes(session_id)))
    if not rows:
        # A failed read and an empty list are the same evidence: none. Return
        # the kernel-owned checkpoint (if any) and nothing else, so a task can
        # still be seen to be alive but can never be judged silent.
        return ProgressEvidence(
            last_activity_at=_plausible(checkpoint_at, now),
            sources=("checkpoint",) if checkpoint_at is not None else (),
        )
    sources.append("execution_processes")

    live = [item for item in rows if _status(item) == "running"]
    timestamps = [
        value for item in rows for value in [_timestamp(item, now)] if value is not None
    ]
    checkpoint = _plausible(checkpoint_at, now)
    if checkpoint is not None:
        sources.append("checkpoint")
        timestamps.append(checkpoint)

    turns = sum(1 for item in rows if item.get("run_reason") == "codingagent")
    output_bytes = _output_bytes(rows)
    if output_bytes is not None:
        sources.append("output_bytes")

    # One snapshot per task, for the current execution only. Reading every
    # live process multiplied the detector's executor calls by the fleet's
    # concurrency for no extra signal, and mixing a retired execution's
    # stream state into the current one is how a finished command's dead
    # stream became the running command's "limbo".
    current = _current_execution(rows, now)
    stream_alive: bool | None = None
    tokens = 0
    typed: dict[str, Any] = {}
    snapshot = (
        _read_snapshot(client, str(current.get("id") or "")) if current else None
    )
    if snapshot is not None:
        sources.append("normalized_snapshot")
        raw_stream = snapshot.get("stream_alive", True)
        stream_alive = bool(raw_stream) if isinstance(raw_stream, bool) else None
        tokens = _snapshot_tokens(snapshot)
        activity = _plausible(snapshot.get("last_activity_at"), now)
        if activity is not None:
            timestamps.append(activity)
            sources.append("transcript")
        for marker in ("turn_ended", "parked", "lifecycle_called", "lost_reason"):
            if marker in snapshot:
                typed[marker] = snapshot[marker]

    # Queued mail matters to exactly one cause: a turn that ended is not idle
    # while work is waiting to be dispatched into it. Asking otherwise spent
    # one executor round-trip per task per tick to answer a question nothing
    # was going to read.
    queued_mail: bool | None = None
    if typed.get("turn_ended"):
        queued_mail = _queued_mail(client, session_id)
        if queued_mail is not None:
            sources.append("queue_status")

    lost_reason = typed.get("lost_reason")
    if lost_reason is None and current is not None:
        lost_reason = _degraded_loss(current)
        if lost_reason:
            sources.append("process_status")

    growing = (
        previous_output_bytes is not None
        and output_bytes is not None
        and output_bytes > previous_output_bytes
    )
    return ProgressEvidence(
        last_activity_at=max(timestamps) if timestamps else None,
        observed=True,
        process_alive=bool(live),
        stream_alive=stream_alive,
        output_growing=growing,
        output_unmeasured=output_bytes is not None and previous_output_bytes is None,
        parked=bool(typed.get("parked")),
        turn_ended=bool(typed.get("turn_ended")),
        lifecycle_called=bool(typed.get("lifecycle_called")),
        queued_mail=bool(queued_mail),
        lost_reason=str(lost_reason) if lost_reason else None,
        turns=turns,
        tokens=tokens,
        cost=None,
        output_bytes=output_bytes,
        confidence="typed" if typed else "degraded",
        sources=tuple(dict.fromkeys(sources)),
    )


def _eligible_processes(value: Any) -> list[dict[str, Any]]:
    """Keep only the process rows this epoch's liveness may be judged from.

    Two filters, both of them the established ones (``cdesktop.py``'s
    ``latest_execution_process``): a ``dropped`` row is a row the executor has
    already disowned, and a ``devserver`` row is infrastructure the task did
    not author. A dev server humming along is not the worker making progress,
    and a dropped row's stale status is not the worker dying.

    Everything that is not a mapping is discarded rather than parsed. A
    hostile or half-written payload - a list of ``None``, of strings, of
    nested lists - must cost this task its evidence, never the whole pass.
    """
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, dict)
        and not item.get("dropped")
        and item.get("run_reason") != "devserver"
    ]


def _current_execution(
    rows: list[dict[str, Any]], now: float
) -> dict[str, Any] | None:
    """The one execution this epoch's liveness is read from.

    A running row always wins: whatever the history says, the thing running
    now is what the task is doing now. Among equals the newest plausible
    timestamp wins, with the row id as a stable tie-break so two ticks over
    an unchanged list never disagree.
    """
    if not rows:
        return None
    running = [item for item in rows if _status(item) == "running"]
    return max(
        running or rows,
        key=lambda item: (
            _timestamp(item, now) or 0.0,
            str(item.get("id") or ""),
        ),
    )


def _status(process: dict[str, Any]) -> str:
    status = process.get("status")
    return status.strip().lower() if isinstance(status, str) else ""


def _output_bytes(rows: list[dict[str, Any]]) -> int | None:
    """Total reported output bytes, or ``None`` when no row reports any.

    Presence is the whole point. No cdesktop row carries ``output_bytes``
    today, and summing a missing field to ``0`` invented a progress signal
    that does not exist in production - the baseline then said "no growth"
    every tick and the timestamp path, which is what actually has to work,
    was never exercised.
    """
    total: int | None = None
    for item in rows:
        if "output_bytes" not in item:
            continue
        value = _non_negative_int(item.get("output_bytes"))
        if value is None:
            continue
        total = value if total is None else total + value
    return total


def _read_snapshot(client: Any, process_id: str) -> dict[str, Any] | None:
    snapshot = _safe(lambda: client.normalized_snapshot(process_id))
    return snapshot if isinstance(snapshot, dict) else None


def _degraded_loss(process: dict[str, Any]) -> str | None:
    """Infer a loss attribution only from a typed field on the *current* run.

    Scoped to one process row on purpose. A session accumulates a process
    history, and scanning all of it meant a single ``killed`` row from a turn
    that ended hours ago marked a healthy, running task irreversibly lost -
    which is a replacement, which is a duplicate session on one branch.

    On the current client the only honest signal is an explicit ``exit_reason``
    or a ``killed`` status. A process row that has simply vanished from the
    list proves nothing - a partial read looks identical - so it yields
    ``None`` and the task stays whatever it already was.
    """
    reason = process.get("exit_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "killed" if _status(process) == "killed" else None


def _queued_mail(client: Any, session_id: str) -> bool | None:
    status = _safe(lambda: client.queue_status(session_id))
    if not isinstance(status, dict):
        return None
    for key in ("pending", "queued", "depth"):
        value = status.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and math.isfinite(value):
            return value > 0
        if isinstance(value, list):
            return bool(value)
    return None


def _snapshot_tokens(snapshot: dict[str, Any]) -> int:
    entries = snapshot.get("entries")
    if not isinstance(entries, list):
        return 0
    for wrapped in entries:
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        entry = content.get("entry_type") if isinstance(content, dict) else None
        if isinstance(entry, dict) and entry.get("type") == "token_usage_info":
            used = _non_negative_int(entry.get("total_tokens"))
            if used is not None:
                return used
    return 0


def _timestamp(process: dict[str, Any], now: float) -> float | None:
    for name in ("completed_at", "updated_at", "started_at", "created_at"):
        value = process.get(name)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            value = (
                parsed.replace(tzinfo=UTC).timestamp()
                if parsed.tzinfo is None
                else parsed.timestamp()
            )
        checked = _plausible(value, now)
        if checked is not None:
            return checked
    return None


def _plausible(value: Any, now: float) -> float | None:
    """Accept a timestamp only if it can actually be a moment in this run.

    Three rejections, all of which otherwise read as "just active" and give a
    task a permanent exemption from detection, because ``silent_for`` floors
    the age at zero:

    * ``NaN`` and infinities, which lose every comparison silently;
    * a value far in the future, which is a clock or a bug, not a heartbeat;
    * a magnitude that can only be milliseconds, the classic unit error.

    An implausible timestamp is *absent* evidence, never proof of life.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number >= IMPLAUSIBLE_TIMESTAMP:
        LOGGER.warning("Ignoring implausible liveness timestamp %r", value)
        return None
    if number > now + MAX_CLOCK_SKEW_SECONDS:
        LOGGER.warning(
            "Ignoring liveness timestamp %r from %.0fs in the future",
            value,
            number - now,
        )
        return None
    return number


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _finite(value: float | None) -> float | None:
    """Drop a non-finite number so the payload stays strict JSON."""
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def _is_positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _required_positive(value: Any, name: str) -> float:
    if not _is_positive_number(value):
        raise DetectionPolicyError(
            f"{name} must be a positive number of seconds, not {value!r}"
        )
    return float(value)


def _safe(call: Any) -> Any:
    """Run one executor read; a failed read is missing evidence, not a fault.

    The detector is a best-effort observer inside a reconciler tick. An
    endpoint that errors must degrade the finding (usually to ``unknown``),
    never abort the pass and starve the other tasks in it. ``None`` is the
    single "nothing was read" answer, so a caller can never confuse a failure
    with an empty but successful result.
    """
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - any executor read failure is absence
        LOGGER.debug("Liveness evidence read failed: %s", exc)
        return None


def _smaller(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _positive_float(raw: str | None, fallback: float, name: str) -> float:
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not _is_positive_number(value):
        LOGGER.warning("%s must be a positive number of seconds; using %s", name, fallback)
        return fallback
    return value


def _positive_float_or_none(raw: str | None, name: str) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not _is_positive_number(value):
        LOGGER.warning("%s must be a positive number; ignoring %r", name, raw)
        return None
    return value


def _positive_int_or_none(raw: str | None, name: str) -> int | None:
    value = _positive_float_or_none(raw, name)
    return None if value is None else int(value)
