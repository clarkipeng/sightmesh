"""Durable wakes: one outbox row per satisfied parent predicate.

A manager waits on a predicate over its children, not on a stream of
per-child mail. The child's terminal transition and the parent's wake row
are written in one transaction on one connection, so a crash cannot land
between "the child finished" and "the parent was told" -- the two facts are
the same fact. Delivery is a separate, retryable pump; the reconciler is its
safety net.

Consolidating the cohort into one payload is also what removes the old
per-child notification path, and with it the ``intent="replace"`` that used
to interrupt a manager's turn whenever a child blocked.

Kernel v1.1 adds the liveness predicates (``docs/liveness-spec.md``). They
arm through this same outbox and deliver through the same pump: a liveness
finding is an observation that satisfies a predicate, never a kill and never
a respawn, so there is exactly one delivery pipe for every reason a manager
is woken.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .cdesktop import CdesktopClient, CdesktopError
from .succession import OwnershipStore, QuarantinedSessionError
from .task_store import (
    LIVE_STATES,
    STALL_LIVENESS_STATES,
    YIELD_STATES,
    TaskRecord,
    TaskStore,
    TaskStoreError,
)

LOGGER = logging.getLogger("sightmesh.wakes")

CLAIM_SECONDS = 60.0
#: Seconds without progress before a manager's un-restored intervention
#: escalates to the human attention queue. Overridden per task by the
#: resolved ``progress_timeout`` (see ``liveness.py``).
DEFAULT_PROGRESS_TIMEOUT = 1500.0
#: Which predicate each typed liveness reason satisfies. ``any_child_stalled``
#: deliberately covers three causes: a manager's response - look at the child,
#: judge, replace or cancel - is the same whether the child ended its turn
#: silently, sat unattachable in limbo, or simply went quiet.
LIVENESS_PREDICATES: dict[str, str] = {
    "lost": "any_child_lost",
    "idle_unreported": "any_child_stalled",
    "limbo": "any_child_stalled",
    "stalled": "any_child_stalled",
    "over_budget": "any_child_over_budget",
}
#: Episodes are capped at two wakes: the episode wake, and one escalation if
#: the manager's intervention did not restore progress. After that the kernel
#: goes quiet and the human attention queue owns the incident.
MAX_WAKES_PER_EPISODE = 2
#: Wake states that prove an incident was already told to someone. ``resolved``
#: is excluded on purpose: a wake suppressed at delivery (quarantined holder,
#: no successor yet) was *not* told, and counting it as told dropped the only
#: notification that incident would ever get. Same rule as the cohort re-arm.
TOLD_WAKE_STATES = ("pending", "claimed", "delivered")
#: Marks the second wake of an episode. It must be a distinct dedupe key: the
#: partial unique index binds uniqueness to un-consumed wakes, so re-using the
#: episode's key while the first wake is still pending - which is exactly the
#: unreachable-manager case escalation exists for - made the escalation INSERT
#: a silent no-op.
ESCALATION_SUFFIX = ":escalation"


@dataclass(frozen=True)
class Wake:
    wake_id: str
    parent_task_id: str
    predicate: str
    dedupe_key: str
    event_seq: int | None
    state: str
    claim_expires_at: float | None
    payload: str | None


def dedupe_key(parent_task_id: str, predicate: str) -> str:
    return f"{parent_task_id}:{predicate}"


def episode_key(
    child_task_id: str,
    epoch: int,
    reason: str,
    episode: int,
    *,
    escalation: bool = False,
) -> str:
    """The stall-episode dedupe key from ``docs/liveness-spec.md``.

    Keyed on the *child*, not the parent, because two silent children are two
    incidents; keyed on the epoch because a replacement is a fresh subject;
    keyed on the episode because a child that stalls, recovers, and stalls
    again is genuinely worth telling the manager about twice; and suffixed for
    the escalation, because the escalation's whole job is to be heard while
    the first wake is still sitting undelivered.
    """
    suffix = ESCALATION_SUFFIX if escalation else ""
    return f"{child_task_id}:{epoch}:{reason}:{episode}{suffix}"


def is_escalation(dedupe_key: str) -> bool:
    """True for the second wake of an episode, so delivery can label it."""
    return dedupe_key.endswith(ESCALATION_SUFFIX)


def finish_with_wake(
    store: TaskStore,
    task_id: str,
    state: str,
    result: str | None = None,
    *,
    expect_version: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[TaskRecord, list[str]]:
    """Finish a task and record any parent wake it satisfies, atomically.

    A caller that is already inside a transaction passes its connection, so
    the terminal, the parent's wake, and whatever the caller wrote alongside
    them stay one commit rather than three that a crash can land between.
    """
    if conn is not None:
        return _finish_with_wake(store, conn, task_id, state, result, expect_version)
    try:
        with store.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            result_pair = _finish_with_wake(
                store, owned, task_id, state, result, expect_version
            )
            owned.execute("COMMIT")
            return result_pair
    except TaskStoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise TaskStoreError(f"Cannot finish managed task: {exc}") from exc


def _finish_with_wake(
    store: TaskStore,
    conn: sqlite3.Connection,
    task_id: str,
    state: str,
    result: str | None,
    expect_version: int | None,
) -> tuple[TaskRecord, list[str]]:
    record = store.finish(
        task_id, state, result, expect_version=expect_version, conn=conn
    )
    created: list[str] = []
    if record.parent_task_id:
        # One child terminal/blocked transition is one cohort event: bump the
        # parent's monotonic counter in the same transaction so a wake's
        # watermark can prove, later, whether the manager has already been
        # woken for it.
        conn.execute(
            "UPDATE managed_tasks SET child_event_seq = child_event_seq + 1 "
            "WHERE task_id = ?",
            (str(record.parent_task_id),),
        )
        created = record_wakes(conn, record.parent_task_id)
        # A lost child is terminal but must not wait for its siblings
        # (liveness-spec.md, cause 4). Arming here, in the same transaction as
        # the terminal write, is what makes "immediate" true even if the
        # process that observed the loss dies next.
        created += record_liveness_wakes(conn, record.task_id)
    return record, created


def satisfied_predicates(conn: sqlite3.Connection, parent_task_id: str) -> list[str]:
    """Evaluate both wait predicates purely over durable child rows.

    Deriving them from stored state rather than from the transition that just
    happened is what lets the reconciler repair pre-migration history with the
    same code the live path uses.
    """
    placeholders = ", ".join("?" for _ in LIVE_STATES)
    total, live, blocked = conn.execute(
        "SELECT COUNT(*), "
        f"SUM(state IN ({placeholders})), "
        "SUM(state = 'blocked') "
        "FROM managed_tasks WHERE parent_task_id = ?",
        (*sorted(LIVE_STATES), str(parent_task_id)),
    ).fetchone()
    predicates: list[str] = []
    if blocked:
        predicates.append("any_child_blocked")
    if total and not live:
        predicates.append("all_children_terminal")
    return predicates


def has_live_wait_predicate(conn: sqlite3.Connection, task_id: str) -> bool:
    """True while a task is yielding to children that have not finished.

    A manager that ends its turn with children still running is doing the one
    thing the contract asks of it - "managers yield while children run" - so
    it is never silent, never idle_unreported, and never stalled
    (liveness-spec.md, cause 1 nuance). Deriving the exemption from durable
    child rows rather than from a flag means it cannot drift: the moment the
    last child goes terminal, the manager is a leaf again and back in scope.

    The exemption counts only children that are actually running
    (``YIELD_STATES``). Counting ``blocked`` and ``reserved`` children too was
    an invisible deadlock: a child blocks on a human, its manager therefore
    looks like it is legitimately yielding, and the whole subtree goes quiet
    with nothing in the system ever flagging it. A blocked child already woke
    the manager through its own predicate; a reserved child that never
    launched is its own attention item, not a reason to stop watching the
    manager waiting on it.
    """
    placeholders = ", ".join("?" for _ in YIELD_STATES)
    total, live = conn.execute(
        f"SELECT COUNT(*), SUM(state IN ({placeholders})) "
        "FROM managed_tasks WHERE parent_task_id = ?",
        (*sorted(YIELD_STATES), str(task_id)),
    ).fetchone()
    return bool(total and live)


def record_wakes(conn: sqlite3.Connection, parent_task_id: str) -> list[str]:
    """Arm one pending wake per satisfied predicate past the watermark; else nothing.

    A wake is due only while the parent has seen a child event its manager has
    not yet been woken for: ``child_event_seq > last_woken_seq``. That single
    comparison is what makes both invariants true by construction. A reconciler
    re-scanning an unchanged, already-delivered cohort finds the counters equal
    and arms nothing; a genuinely new child event (or a resolved, never
    -delivered wake that left the watermark where it was) leaves the counter
    ahead and re-arms. The partial unique index still collapses concurrent
    duplicate signals for the same un-consumed cohort event to one live row.
    """
    row = conn.execute(
        "SELECT child_event_seq, last_woken_seq FROM managed_tasks WHERE task_id = ?",
        (str(parent_task_id),),
    ).fetchone()
    if row is None:
        return []
    child_event_seq = int(row["child_event_seq"])
    if child_event_seq <= int(row["last_woken_seq"]):
        return []
    created: list[str] = []
    for predicate in satisfied_predicates(conn, parent_task_id):
        key = dedupe_key(parent_task_id, predicate)
        wake_id = str(uuid.uuid4())
        now = time.time()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO task_wakes
            (wake_id, parent_task_id, predicate, dedupe_key, event_seq,
             state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (wake_id, str(parent_task_id), predicate, key, child_event_seq, now, now),
        )
        if cursor.rowcount:
            created.append(wake_id)
    return created


def record_liveness_wakes(
    conn: sqlite3.Connection,
    child_task_id: str,
    *,
    now: float | None = None,
    progress_timeout: float = DEFAULT_PROGRESS_TIMEOUT,
) -> list[str]:
    """Arm the liveness predicates one silent child satisfies, episode-deduped.

    Separate from :func:`record_wakes` because the two answer different
    questions. A cohort wake asks "has the parent's wait predicate become
    true?" and is gated by the cohort watermark. A liveness wake asks "is this
    specific child in trouble?" - it is per-child, per-epoch, per-episode, and
    must fire while the cohort is still very much unfinished. Sharing the
    outbox and the pump keeps them one delivery path anyway.

    Idempotence comes from durable state, never from an in-memory set:
    ``lost`` and ``over_budget`` hold at most once per ``(child, epoch)``, so
    their own wake row is the latch; a stall episode is capped by
    ``liveness_wakes``, bumped in this same transaction as the insert.
    """
    moment = time.time() if now is None else now
    row = conn.execute(
        "SELECT task_id, parent_task_id, epoch, state, liveness, liveness_episode, "
        "liveness_since, liveness_wakes, liveness_evidence, over_budget "
        "FROM managed_tasks WHERE task_id = ?",
        (str(child_task_id),),
    ).fetchone()
    if row is None or not row["parent_task_id"]:
        return []
    parent_task_id = str(row["parent_task_id"])
    epoch = int(row["epoch"])
    created: list[str] = []

    if str(row["state"]) == "lost":
        # A dead child has no stall episode and no budget left to run; the
        # loss is the whole report.
        return _arm_liveness(
            conn,
            parent_task_id,
            "lost",
            episode_key(str(child_task_id), epoch, "lost", 1),
            moment,
        )
    if str(row["state"]) != "active":
        # Only a task that is supposed to be making progress can fail to.
        # A completed or cancelled child's findings are moot, and a blocked
        # one has already reported itself through `any_child_blocked` - waking
        # the manager a second time about the same child, for a silence it has
        # now explained, is the notification storm this design exists to kill.
        return []

    liveness = str(row["liveness"])
    if liveness in STALL_LIVENESS_STATES:
        emitted = int(row["liveness_wakes"])
        since = row["liveness_since"]
        escalating = emitted == 1 and since is not None and (
            moment - float(since) >= progress_timeout
        )
        if emitted == 0 or escalating:
            armed = _arm_liveness(
                conn,
                parent_task_id,
                liveness,
                episode_key(
                    str(child_task_id),
                    epoch,
                    liveness,
                    int(row["liveness_episode"]),
                    escalation=escalating,
                ),
                moment,
                one_shot=False,
            )
            if armed:
                # Same transaction as the insert: the counter and the row can
                # never disagree about how much of this episode has been told.
                # No ``version`` bump: an observation is not a state change,
                # and invalidating a manager's in-flight read would be the
                # detector manufacturing the conflicts it reports.
                conn.execute(
                    "UPDATE managed_tasks SET liveness_wakes = liveness_wakes + 1, "
                    "liveness_since = ? WHERE task_id = ?",
                    (moment, str(child_task_id)),
                )
            created += armed
    if row["over_budget"]:
        created += _arm_liveness(
            conn,
            parent_task_id,
            "over_budget",
            episode_key(str(child_task_id), epoch, "over_budget", 1),
            moment,
        )
    return created


def episode_is_exhausted(record: TaskRecord) -> bool:
    """True once an episode has spent both its wakes and the kernel goes quiet."""
    return (
        record.liveness in STALL_LIVENESS_STATES
        and record.liveness_wakes >= MAX_WAKES_PER_EPISODE
    )


def _arm_liveness(
    conn: sqlite3.Connection,
    parent_task_id: str,
    reason: str,
    key: str,
    now: float,
    *,
    one_shot: bool = True,
) -> list[str]:
    """Insert one liveness wake, or nothing if this incident was already told.

    The pre-check is scoped two ways. It looks only at wakes that actually
    reached someone (``TOLD_WAKE_STATES``), so a wake suppressed at delivery
    can arm again rather than being dropped for good; and it is an equality
    match on the indexed ``dedupe_key``, so it is a lookup rather than a scan
    of every wake the outbox has ever held.
    """
    told = ", ".join("?" for _ in TOLD_WAKE_STATES)
    if one_shot and conn.execute(
        f"SELECT 1 FROM task_wakes WHERE dedupe_key = ? AND state IN ({told})",
        (key, *TOLD_WAKE_STATES),
    ).fetchone():
        return []
    wake_id = str(uuid.uuid4())
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO task_wakes
        (wake_id, parent_task_id, predicate, dedupe_key, event_seq,
         state, created_at, updated_at)
        VALUES (?, ?, ?, ?, NULL, 'pending', ?, ?)
        """,
        (wake_id, parent_task_id, LIVENESS_PREDICATES[reason], key, now, now),
    )
    return [wake_id] if cursor.rowcount else []


class WakeDelivery:
    """Claim, consolidate, and deliver pending wakes; safe to run repeatedly."""

    def __init__(
        self,
        client: CdesktopClient,
        store: TaskStore,
        ownership: OwnershipStore | None = None,
        *,
        claim_seconds: float = CLAIM_SECONDS,
    ) -> None:
        self.client = client
        self.store = store
        self.ownership = ownership if ownership is not None else OwnershipStore()
        self.claim_seconds = claim_seconds

    def pump(self) -> int:
        """Deliver every claimable wake; return how many left the outbox."""
        delivered = 0
        for wake in self.claim():
            try:
                delivered += int(self._deliver(wake))
            except CdesktopError as exc:
                # The claim lease expires and the reconciler retries; the row
                # stays visible in the outbox rather than being dropped here.
                LOGGER.warning("Cannot deliver wake %s: %s", wake.wake_id, exc)
        return delivered

    def claim(self) -> list[Wake]:
        now = time.time()
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT * FROM task_wakes WHERE state = 'pending' "
                    "OR (state = 'claimed' AND claim_expires_at < ?) "
                    "ORDER BY created_at",
                    (now,),
                ).fetchall()
                claimed = [_decode(row) for row in rows]
                for wake in claimed:
                    conn.execute(
                        "UPDATE task_wakes SET state = 'claimed', "
                        "claim_expires_at = ?, updated_at = ? WHERE wake_id = ?",
                        (now + self.claim_seconds, now, wake.wake_id),
                    )
                conn.execute("COMMIT")
                return claimed
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot claim task wakes: {exc}") from exc

    def _deliver(self, wake: Wake) -> bool:
        parent = self.store.get_by_id(wake.parent_task_id)
        if parent is None:
            return self._resolve(wake, "parent task no longer exists")
        if not parent.holder_session_id:
            return self._resolve(wake, f"parent {parent.key} has no holder session")
        if parent.state not in LIVE_STATES:
            # A terminal parent refuses machine mail; delivering here would send
            # a continuation into a session that has already ended.
            return self._resolve(wake, f"parent {parent.key} is {parent.state}")
        try:
            self.ownership.assert_deliverable(parent.holder_session_id)
        except QuarantinedSessionError as exc:
            # A retired or superseded holder session can never resume; parking
            # the wake (and, with live re-arm, letting a fresh cohort event wake
            # the successor) is the only safe outcome.
            return self._resolve(wake, f"parent {parent.key} session is retired: {exc}")
        children = self.store.children(parent.task_id)
        if any(
            child.holder_session_id == parent.holder_session_id for child in children
        ):
            return self._resolve(
                wake, f"parent {parent.key} holds one of its own child sessions"
            )
        payload = _payload(
            wake.predicate,
            parent,
            children,
            escalation=is_escalation(wake.dedupe_key),
        )
        self.client.send(
            parent.holder_session_id,
            payload,
            None,
            dedupe_key=wake.wake_id,
            intent="continue",
        )
        self._settle(wake, "delivered", payload)
        return True

    def _resolve(self, wake: Wake, reason: str) -> bool:
        """Park a suppressed delivery with its reason; never return silently."""
        LOGGER.info("Wake %s resolved without delivery: %s", wake.wake_id, reason)
        self._settle(wake, "resolved", f"suppressed: {reason}")
        return False

    def _settle(self, wake: Wake, state: str, payload: str) -> None:
        now = time.time()
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE task_wakes SET state = ?, payload = ?, "
                    "claim_expires_at = NULL, updated_at = ? WHERE wake_id = ?",
                    (state, payload, now, wake.wake_id),
                )
                if state == "delivered" and wake.event_seq is not None:
                    # Only a real delivery advances the watermark. A resolved
                    # (suppressed) wake leaves it where it was, so the same
                    # cohort event re-arms on the next pass. MAX keeps an
                    # out-of-order delivery from ever moving it backwards.
                    conn.execute(
                        "UPDATE managed_tasks SET "
                        "last_woken_seq = MAX(last_woken_seq, ?), updated_at = ? "
                        "WHERE task_id = ?",
                        (int(wake.event_seq), now, wake.parent_task_id),
                    )
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot settle task wake: {exc}") from exc


def _payload(
    predicate: str,
    parent: TaskRecord,
    children: list[TaskRecord],
    *,
    escalation: bool = False,
) -> str:
    """One consolidated cohort view, with each child's liveness finding inline.

    A liveness wake names its subject through the cohort listing rather than a
    dedicated column: the manager reads ``any_child_stalled`` and sees exactly
    which child carries the finding, its evidence, and the detector's own
    confidence in that evidence. Degraded evidence is labelled as such so a
    manager never mistakes "cdesktop could not tell us" for "the child is
    definitely wedged".

    An escalation says so in its first line. It is the second and last time
    the kernel will raise this incident, and a manager that reads it exactly
    like the first wake has no way to know that.
    """
    heading = "ESCALATION" if escalation else "COHORT"
    lines = [f"{heading} {predicate}: {parent.key}"]
    for child in children:
        line = f"- {child.key}: {child.state}"
        if child.liveness != "live":
            line += f" | liveness={child.liveness}"
        if child.over_budget:
            line += " | over_budget"
        if child.result:
            line += f" | {child.result}"
        if child.liveness_evidence and (
            child.liveness != "live" or child.over_budget or child.state == "lost"
        ):
            line += f" | evidence={child.liveness_evidence}"
        lines.append(line)
    return "\n".join(lines)


def _decode(row: Any) -> Wake:
    return Wake(
        wake_id=str(row["wake_id"]),
        parent_task_id=str(row["parent_task_id"]),
        predicate=str(row["predicate"]),
        dedupe_key=str(row["dedupe_key"]),
        event_seq=row["event_seq"],
        state=str(row["state"]),
        claim_expires_at=row["claim_expires_at"],
        payload=row["payload"],
    )
