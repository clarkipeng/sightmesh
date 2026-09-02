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

PROGRESS_TIMEOUT_ENV = "SIGHTMESH_PROGRESS_TIMEOUT_SECONDS"
APPROVAL_TIMEOUT_ENV = "SIGHTMESH_APPROVAL_TIMEOUT_SECONDS"

#: Classifications that arm nothing. ``live`` means progress was *observed*
#: and closes an open episode; ``unknown`` means nothing was observed either
#: way and leaves the row exactly as it found it.
INERT = frozenset({"live", "unknown"})


class BudgetError(ValueError):
    """A budget that cannot be satisfied by any run."""


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
            if value is not None and value <= 0:
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
    """The resolved, trusted detection settings for one task."""

    progress_timeout: float = DEFAULT_PROGRESS_TIMEOUT
    approval_timeout: float | None = None
    budget: Budget | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"progress_timeout": self.progress_timeout}
        if self.approval_timeout is not None:
            payload["approval_timeout"] = self.approval_timeout
        if self.budget is not None:
            payload["budget"] = self.budget.to_dict()
        return payload

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
    process_alive: bool = False
    stream_alive: bool = True
    output_growing: bool = False
    parked: bool = False
    turn_ended: bool = False
    lifecycle_called: bool = False
    queued_mail: bool = False
    lost_reason: str | None = None
    turns: int = 0
    tokens: int = 0
    cost: float | None = None
    output_bytes: int = 0
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
            "silent_for": self.silent_for(now),
            "process_alive": self.process_alive,
            "stream_alive": self.stream_alive,
            "output_growing": self.output_growing,
            "turns": self.turns,
            "tokens": self.tokens,
            "cost": self.cost,
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
        body = self.evidence.to_dict(self.now)
        body["reason"] = self.reason
        if self.over_budget:
            body["over_budget"] = True
        return json.dumps(body, sort_keys=True, separators=(",", ":"))


def trusted_policy(environment: dict[str, str] | None = None) -> DetectionPolicy:
    """The manager-side detection floor a worker's own spec cannot loosen.

    Sourced from the manager's process environment, which is where trusted
    profile and manager configuration land today. A worker's ``WorkerSpec``
    can only tighten what this returns (see :func:`resolve_policy`).
    """
    env = os.environ if environment is None else environment
    return DetectionPolicy(
        progress_timeout=_positive_float(
            env.get(PROGRESS_TIMEOUT_ENV), DEFAULT_PROGRESS_TIMEOUT, PROGRESS_TIMEOUT_ENV
        ),
        approval_timeout=(
            _positive_float(
                env.get(APPROVAL_TIMEOUT_ENV),
                DEFAULT_APPROVAL_TIMEOUT,
                APPROVAL_TIMEOUT_ENV,
            )
            if env.get(APPROVAL_TIMEOUT_ENV)
            else None
        ),
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
    return DetectionPolicy(
        progress_timeout=min(
            floor.progress_timeout,
            progress_timeout if progress_timeout is not None else floor.progress_timeout,
        ),
        approval_timeout=_smaller(floor.approval_timeout, approval_timeout),
        budget=budget.tightest(floor.budget) if budget else floor.budget,
    )


def classify(
    evidence: ProgressEvidence, *, now: float, policy: DetectionPolicy
) -> str:
    """Map progress evidence onto one typed cause from the liveness table.

    Ordered by how definitive the signal is, most definitive first, so a
    weaker inference can never overrule a typed fact:

    1. a typed loss marker is a terminal fact the executor already owns;
    2. a parked approval is excluded from stall detection by contract;
    3. a process the executor cannot vouch for is ``unknown``, not ``lost`` -
       guessing an attribution is how "infrastructure failure" got recorded as
       "result failure";
    4. growing output bytes are progress, however long the command runs (S16);
    5. a turn that ended with no lifecycle call and no queued mail is
       ``idle_unreported`` once the grace lapses;
    6. a dead stream over a live process is ``limbo``;
    7. otherwise, silence past ``progress_timeout`` is ``stalled``.
    """
    if evidence.lost_reason:
        return "lost"
    if evidence.parked:
        return "parked"
    if not evidence.process_alive:
        return "unknown"
    if evidence.output_growing:
        return "live"
    silent = evidence.silent_for(now)
    if silent is None:
        # No timestamp from any source. The executor is not telling us whether
        # this task is working or wedged, and inventing an answer is exactly
        # the failure this spec exists to prevent.
        return "unknown"
    if evidence.turn_ended and not evidence.lifecycle_called and not evidence.queued_mail:
        return "idle_unreported" if silent >= IDLE_UNREPORTED_GRACE else "live"
    if not evidence.stream_alive:
        # Cause 3: the executor re-attaches through its durable handle, so
        # limbo is only *reported* once it has stayed unattachable for a full
        # progress_timeout. In degraded mode there is no attach signal to read,
        # so continued silence is the honest proxy for "unattachable".
        return "limbo" if silent >= policy.progress_timeout else "live"
    return "stalled" if silent >= policy.progress_timeout else "live"


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
    processes = _safe(lambda: client.execution_processes(session_id), []) or []
    live = [item for item in processes if str(item.get("status") or "") == "running"]
    timestamps = [
        value
        for item in processes
        for value in [_timestamp(item)]
        if value is not None
    ]
    if timestamps:
        sources.append("execution_processes")
    if checkpoint_at is not None:
        sources.append("checkpoint")
        timestamps.append(checkpoint_at)

    turns = sum(1 for item in processes if item.get("run_reason") == "codingagent")
    output_bytes = sum(int(item.get("output_bytes") or 0) for item in processes)
    if any("output_bytes" in item for item in processes):
        sources.append("output_bytes")

    stream_alive = True
    tokens = 0
    typed: dict[str, Any] = {}
    for item in live or processes[-1:]:
        snapshot = _read_snapshot(client, str(item.get("id") or ""))
        if snapshot is None:
            continue
        sources.append("normalized_snapshot")
        stream_alive = stream_alive and bool(snapshot.get("stream_alive", True))
        tokens = max(tokens, _snapshot_tokens(snapshot))
        activity = snapshot.get("last_activity_at")
        if isinstance(activity, (int, float)):
            timestamps.append(float(activity))
            sources.append("transcript")
        for marker in ("turn_ended", "parked", "lifecycle_called", "lost_reason"):
            if marker in snapshot:
                typed[marker] = snapshot[marker]

    queued_mail = _queued_mail(client, session_id)
    if queued_mail is not None:
        sources.append("queue_status")

    lost_reason = typed.get("lost_reason")
    if lost_reason is None:
        lost_reason = _degraded_loss(processes)
        if lost_reason:
            sources.append("process_status")

    return ProgressEvidence(
        last_activity_at=max(timestamps) if timestamps else None,
        process_alive=bool(live),
        stream_alive=stream_alive,
        output_growing=(
            previous_output_bytes is not None and output_bytes > previous_output_bytes
        ),
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


def _read_snapshot(client: Any, process_id: str) -> dict[str, Any] | None:
    snapshot = _safe(lambda: client.normalized_snapshot(process_id), None)
    return snapshot if isinstance(snapshot, dict) else None


def _degraded_loss(processes: list[dict[str, Any]]) -> str | None:
    """Infer a loss attribution only from a typed executor field, never a guess.

    On the current client the only honest signal is an explicit ``exit_reason``
    or a ``killed`` status. A process row that has simply vanished from the
    list proves nothing - a partial read looks identical - so it yields
    ``None`` and the task stays whatever it already was.
    """
    for item in processes:
        reason = item.get("exit_reason")
        if reason:
            return str(reason)
        if str(item.get("status") or "") == "killed":
            return "killed"
    return None


def _queued_mail(client: Any, session_id: str) -> bool | None:
    status = _safe(lambda: client.queue_status(session_id), None)
    if not isinstance(status, dict):
        return None
    for key in ("pending", "queued", "depth"):
        value = status.get(key)
        if isinstance(value, (int, float)):
            return value > 0
        if isinstance(value, list):
            return bool(value)
    return None


def _snapshot_tokens(snapshot: dict[str, Any]) -> int:
    for wrapped in snapshot.get("entries", []):
        content = wrapped.get("content") if isinstance(wrapped, dict) else None
        entry = content.get("entry_type") if isinstance(content, dict) else None
        if isinstance(entry, dict) and entry.get("type") == "token_usage_info":
            used = entry.get("total_tokens")
            if isinstance(used, (int, float)):
                return int(used)
    return 0


def _timestamp(process: dict[str, Any]) -> float | None:
    for name in ("completed_at", "updated_at", "started_at", "created_at"):
        value = process.get(name)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return (
                parsed.replace(tzinfo=UTC).timestamp()
                if parsed.tzinfo is None
                else parsed.timestamp()
            )
    return None


def _safe(call: Any, fallback: Any) -> Any:
    """Run one executor read; a failed read is missing evidence, not a fault.

    The detector is a best-effort observer inside a reconciler tick. An
    endpoint that errors must degrade the finding (usually to ``unknown``),
    never abort the pass and starve the other tasks in it.
    """
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - any executor read failure is absence
        LOGGER.debug("Liveness evidence read failed: %s", exc)
        return fallback


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
    if value <= 0:
        LOGGER.warning("%s must be a positive number of seconds; using %s", name, fallback)
        return fallback
    return value
