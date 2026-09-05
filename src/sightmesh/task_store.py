from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .escalation import EscalationStore, escalation_db_path
from .fence import HELD_TASK_FENCE

TASK_NAMESPACE = uuid.UUID("620f9fa2-f939-4a9f-aed5-2a558f2ed107")

#: States a task can still leave under its own power.
LIVE_STATES = frozenset({"reserved", "active", "replacing", "blocked"})
#: Legal predecessors per finish target. ``completed``, ``cancelled``, ``lost``
#: and ``exhausted`` appear in no set, which is what makes them immutable terminals:
#: the first legal terminal transition wins and every later one is stale.
FINISH_PREDECESSORS: dict[str, frozenset[str]] = {
    "completed": frozenset({"active", "replacing", "blocked"}),
    # ``reserved`` is a legal predecessor because a definitive launch rejection
    # blocks the epoch before it ever activates: the task must land in a
    # replaceable terminal rather than stay ``reserved`` and relaunchable (a
    # retry is an explicit new epoch via ``replace()``).
    "blocked": frozenset({"reserved", "active", "replacing"}),
    "cancelled": frozenset({"reserved", "active", "replacing", "blocked"}),
    "lost": frozenset({"reserved", "active", "replacing"}),
    "exhausted": frozenset({"active", "blocked"}),
}
#: ``replacing`` is included because a replacement launch activates the
#: successor session it just spawned for the prepared epoch.
ACTIVATE_PREDECESSORS = frozenset({"reserved", "active", "replacing"})
REPLACE_PREDECESSORS = frozenset({"active", "blocked", "lost"})

#: Typed liveness classifications a task row can carry (liveness-spec.md, the
#: per-cause table). ``live`` is the absence of a finding, not a claim of
#: health: a task the detector cannot judge keeps whatever value it had, so
#: "no evidence" never masquerades as "progress observed".
LIVENESS_STATES = ("live", "parked", "idle_unreported", "limbo", "stalled")
#: The subset that satisfies ``any_child_stalled``. ``parked`` is deliberately
#: absent: cause 2 is excluded from stall detection by contract.
STALL_LIVENESS_STATES = frozenset({"idle_unreported", "limbo", "stalled"})
#: States in which a child is genuinely working on its manager's behalf, and
#: so exempts that manager from stall detection. ``blocked`` and ``reserved``
#: are pointedly absent: a child that is blocked, or that never launched, is
#: not making progress, and counting it as a live wait wedged the manager
#: behind it into permanent invisibility.
YIELD_STATES = frozenset({"active", "replacing"})


def liveness_stretch(liveness: str) -> str:
    """The behavioural stretch a classification belongs to.

    Three of them, and only a move between two is an episode boundary:
    ``live`` (progress observed), ``parked`` (waiting on a human by design),
    and ``silent`` (every stall cause). Collapsing the stall causes into one
    stretch is what makes ``stalled`` <-> ``limbo`` flapping free.
    """
    return liveness if liveness in {"live", "parked"} else "silent"


_LIVENESS_CHECK = "liveness IN (" + ", ".join(f"'{v}'" for v in LIVENESS_STATES) + ")"
#: Forward-only liveness columns, in ``ALTER TABLE ADD COLUMN`` form. Each
#: carries a constant default so it is a legal in-place addition; the fresh
#: DDL above lists the identical definitions, so an upgraded database and a
#: newly created one are the same schema.
_LIVENESS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("liveness", f"TEXT NOT NULL DEFAULT 'live' CHECK ({_LIVENESS_CHECK})"),
    ("liveness_episode", "INTEGER NOT NULL DEFAULT 0"),
    ("liveness_since", "REAL"),
    ("liveness_wakes", "INTEGER NOT NULL DEFAULT 0"),
    ("liveness_evidence", "TEXT"),
    ("over_budget", "INTEGER NOT NULL DEFAULT 0 CHECK (over_budget IN (0, 1))"),
    ("checkpoint_at", "REAL"),
)

_MANAGED_TASKS_DDL = """
    CREATE TABLE {name} (
        task_id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        task_key TEXT NOT NULL,
        parent_task_id TEXT,
        state TEXT NOT NULL CHECK (state IN
            ('reserved', 'active', 'replacing', 'blocked',
             'completed', 'cancelled', 'lost', 'exhausted')),
        epoch INTEGER NOT NULL CHECK (epoch > 0),
        attempts INTEGER NOT NULL CHECK (attempts >= 0),
        max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
        child_limit INTEGER NOT NULL CHECK (child_limit >= 0),
        spec_json TEXT NOT NULL,
        workspace_id TEXT,
        holder_session_id TEXT,
        checkpoint TEXT,
        result TEXT,
        version INTEGER NOT NULL DEFAULT 0,
        child_event_seq INTEGER NOT NULL DEFAULT 0,
        last_woken_seq INTEGER NOT NULL DEFAULT 0,
        liveness TEXT NOT NULL DEFAULT 'live' CHECK (liveness IN
            ('live', 'parked', 'idle_unreported', 'limbo', 'stalled')),
        liveness_episode INTEGER NOT NULL DEFAULT 0,
        liveness_since REAL,
        liveness_wakes INTEGER NOT NULL DEFAULT 0,
        liveness_evidence TEXT,
        over_budget INTEGER NOT NULL DEFAULT 0 CHECK (over_budget IN (0, 1)),
        checkpoint_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(scope, task_key),
        CHECK (parent_task_id IS NULL OR parent_task_id != task_id),
        FOREIGN KEY(parent_task_id) REFERENCES managed_tasks(task_id)
    )
"""
_MANAGED_TASKS_COLUMNS = (
    "task_id",
    "scope",
    "task_key",
    "parent_task_id",
    "state",
    "epoch",
    "attempts",
    "max_attempts",
    "child_limit",
    "spec_json",
    "workspace_id",
    "holder_session_id",
    "checkpoint",
    "result",
    "version",
    "child_event_seq",
    "last_woken_seq",
    "liveness",
    "liveness_episode",
    "liveness_since",
    "liveness_wakes",
    "liveness_evidence",
    "over_budget",
    "checkpoint_at",
    "created_at",
    "updated_at",
)
_SELF_PARENT_CHECK = "parent_task_id != task_id"
_REBUILD_TABLE = "managed_tasks_kernel_v1"

#: ``dedupe_key`` (``{parent}:{predicate}``) carries no column-level
#: ``UNIQUE``: uniqueness binds only *live* wakes through ``idx_task_wakes_live``
#: below, so a consumed wake's key can arm again for the next cohort transition
#: instead of once per epoch ever. ``event_seq`` records the parent's
#: ``child_event_seq`` at wake creation; delivery advances the parent's
#: ``last_woken_seq`` to it, so a re-scan of an unchanged cohort (same seq) arms
#: nothing while a genuinely new child event (higher seq) arms afresh.
_TASK_WAKES_DDL = """
    CREATE TABLE {name} (
        wake_id TEXT PRIMARY KEY,
        parent_task_id TEXT NOT NULL,
        predicate TEXT NOT NULL CHECK (predicate IN
            ('all_children_terminal', 'any_child_blocked',
             'any_child_lost', 'any_child_stalled', 'any_child_over_budget')),
        dedupe_key TEXT NOT NULL,
        event_seq INTEGER,
        state TEXT NOT NULL CHECK (state IN
            ('pending', 'claimed', 'delivered', 'resolved')),
        claim_expires_at REAL,
        payload TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
"""
_TASK_WAKES_REBUILD_TABLE = "task_wakes_kernel_v1"
#: Columns a pre-fix ``task_wakes`` carries, in order, for a rebuild carry-over.
_TASK_WAKES_LEGACY_COLUMNS = (
    "wake_id",
    "parent_task_id",
    "predicate",
    "dedupe_key",
    "state",
    "claim_expires_at",
    "payload",
    "created_at",
    "updated_at",
)
#: The exact fragment a pre-fix ``task_wakes`` carries; its presence is what
#: tells the migration a lifetime-unique ``dedupe_key`` still needs unbinding.
_TASK_WAKES_LEGACY_UNIQUE = "dedupe_key TEXT NOT NULL UNIQUE"
#: A predicate only kernel v1.1 can write. Its absence from the stored table
#: SQL is what tells the migration the ``predicate`` CHECK still has to be
#: widened - SQLite cannot alter a CHECK in place, so widening is a rebuild.
_TASK_WAKES_V11_PREDICATE = "any_child_stalled"


class TaskStoreError(RuntimeError):
    pass


class StaleTransition(TaskStoreError):
    """A guarded transition observed a task the caller no longer describes.

    Carries the current record so a caller can decide between an idempotent
    no-op (the observed state already is the attempted target) and a real
    conflict (another writer won the race).
    """

    def __init__(self, current: TaskRecord, attempted: str) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(
            f"Task {current.key!r} is {current.state} at version {current.version}; "
            f"it cannot transition to {attempted}"
        )


@dataclass
class TaskFence:
    """Unforgeable-in-practice capability yielded only by ``task_lock``."""

    _store: TaskStore
    task_id: str
    _stream: Any
    _token: Any
    _external_depth: int = 0

    @contextmanager
    def external_io(self) -> Iterator[None]:
        """Release this task's lifecycle gate while asking cdesktop.

        The durable row/version is the claim across the call.  Reacquiring the
        same gate before examining its result lets a terminal or replacement
        writer proceed during a slow request without letting that old result
        revive the task.
        """
        outermost = self._external_depth == 0
        self._external_depth += 1
        if outermost:
            HELD_TASK_FENCE.reset(self._token)
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            self._external_depth -= 1
            if outermost:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
                self._token = HELD_TASK_FENCE.set(self.task_id)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    scope: str
    key: str
    parent_task_id: str | None
    state: str
    epoch: int
    attempts: int
    max_attempts: int
    child_limit: int
    spec: dict[str, Any]
    workspace_id: str | None
    holder_session_id: str | None
    checkpoint: str | None
    result: str | None
    version: int
    created_at: float
    updated_at: float
    liveness: str = "live"
    liveness_episode: int = 0
    liveness_since: float | None = None
    liveness_wakes: int = 0
    liveness_evidence: str | None = None
    over_budget: bool = False
    checkpoint_at: float | None = None


class TaskStore:
    """Persist semantic parentage and budgets cdesktop cannot reconstruct."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or escalation_db_path()
        self._database = EscalationStore(self.path)
        self._initialize()

    def _initialize(self) -> None:
        try:
            with self._database._connect() as conn:
                # Take the write lock BEFORE reading any schema state. Two
                # processes racing a first-run migration would otherwise both
                # observe a pre-kernel database and P2's rebuild would wipe the
                # rows P1 just preserved; serializing here means P2 re-reads the
                # schema under the lock and sees P1's completed rebuild.
                conn.execute("BEGIN IMMEDIATE")
                self._migrate_managed_tasks(conn)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_managed_tasks_holder "
                    "ON managed_tasks(holder_session_id) "
                    "WHERE holder_session_id IS NOT NULL AND state IN "
                    "('active', 'replacing', 'blocked')"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_managed_tasks_parent "
                    "ON managed_tasks(parent_task_id, created_at)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_effects (
                        task_id TEXT NOT NULL,
                        epoch INTEGER NOT NULL,
                        request_hash TEXT NOT NULL,
                        state TEXT NOT NULL
                            CHECK (state IN ('reserved', 'launched', 'terminal')),
                        workspace_id TEXT,
                        session_id TEXT,
                        outcome TEXT,
                        retry_at REAL,
                        owner_instance TEXT NOT NULL,
                        lease_expires_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (task_id, epoch)
                    )
                    """
                )
                self._ensure_effect_retry_at(conn)
                self._migrate_task_wakes(conn)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_cleanup_intents (
                        task_id TEXT NOT NULL,
                        epoch INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('command_cancel', 'execution_stop')),
                        native_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'acknowledged')),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (task_id, epoch, kind, native_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_cleanup_pending "
                    "ON task_cleanup_intents(state, created_at)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_outgoing_commands (
                        task_id TEXT NOT NULL,
                        epoch INTEGER NOT NULL,
                        dedupe_key TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        native_id TEXT,
                        state TEXT NOT NULL CHECK (state IN
                            ('sending', 'settled', 'cleanup', 'acknowledged')),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (task_id, epoch, dedupe_key)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_outgoing_pending "
                    "ON task_outgoing_commands(state, created_at)"
                )
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot initialize managed tasks: {exc}") from exc

    @contextmanager
    def task_lock(self, task_id: str) -> Iterator[TaskFence]:
        """Serialize one task's durable lifecycle decisions.

        This is deliberately a per-task advisory file lock rather than a
        SQLite transaction: it survives separate SightMesh processes and the
        kernel releases it if its owner crashes.  cdesktop I/O explicitly
        releases it; the row's version and epoch carry the claim across that
        external boundary.
        """
        digest = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()
        directory = self.path.parent / f".{self.path.name}.task-locks"
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / digest
        with lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            token = HELD_TASK_FENCE.set(str(task_id))
            fence = TaskFence(self, str(task_id), stream, token)
            try:
                yield fence
            finally:
                HELD_TASK_FENCE.reset(fence._token)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def admission_lock(self) -> Iterator[None]:
        """Serialize process admission across every local SightMesh starter.

        The lock spans the executor snapshot and launch so simultaneous
        starters cannot all observe the same spare slot. It is intentionally
        separate from a task fence: executor I/O must remain unlocked per task.
        """
        path = self.path.parent / f".{self.path.name}.admission-lock"
        with path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _migrate_managed_tasks(conn: sqlite3.Connection) -> None:
        """Bring ``managed_tasks`` to the kernel v1 shape, forward only.

        SQLite cannot add a CHECK constraint in place, so an existing table
        without the self-parent constraint or the ``version`` column is
        rebuilt. Running this twice is a no-op: the second pass sees both
        additions and returns before touching any row. The caller holds
        ``BEGIN IMMEDIATE``, so this reads the schema under the write lock and
        never races a peer process into a double rebuild.
        """
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'managed_tasks'"
        ).fetchone()
        if existing is None:
            conn.execute(_MANAGED_TASKS_DDL.format(name="managed_tasks"))
            return
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(managed_tasks)").fetchall()
        }
        has_version = "version" in columns
        current_sql = str(existing["sql"])
        if (
            has_version
            and _SELF_PARENT_CHECK in current_sql
            and "attempts >= 0" in current_sql
        ):
            # The table is already at the kernel-v1 shape; only the watermark
            # and liveness columns may still be missing. Re-read table_info
            # under the same write lock and add them forward-only, never a
            # pre-lock read.
            TaskStore._ensure_watermark_columns(conn)
            TaskStore._ensure_liveness_columns(conn)
            return
        carried = ", ".join(
            name for name in _MANAGED_TASKS_COLUMNS if name != "parent_task_id"
        )
        conn.execute(f"DROP TABLE IF EXISTS {_REBUILD_TABLE}")
        conn.execute(_MANAGED_TASKS_DDL.format(name=_REBUILD_TABLE))
        conn.execute(
            f"INSERT INTO {_REBUILD_TABLE} (parent_task_id, {carried}) "
            # A legacy self-parent row cannot satisfy the new CHECK; keeping
            # the row and dropping the impossible link preserves history.
            "SELECT CASE WHEN parent_task_id = task_id THEN NULL "
            "ELSE parent_task_id END, "
            + ", ".join(
                # Pre-#119 attempts counted launches, not failures. That
                # number has no recoverable failure meaning, so migration
                # starts the new failure budget at its only sound value: zero.
                "0" if name == "attempts" else
                name if name in columns else {
                    "version": "0",
                    "child_event_seq": "0",
                    "last_woken_seq": "0",
                    "liveness": "'live'",
                    "liveness_episode": "0",
                    "liveness_since": "NULL",
                    "liveness_wakes": "0",
                    "liveness_evidence": "NULL",
                    "over_budget": "0",
                    "checkpoint_at": "NULL",
                }[name]
                for name in _MANAGED_TASKS_COLUMNS if name != "parent_task_id"
            )
            + " FROM managed_tasks"
        )
        conn.execute("DROP TABLE managed_tasks")
        conn.execute(f"ALTER TABLE {_REBUILD_TABLE} RENAME TO managed_tasks")
        # Carried rows inherit the DDL ``DEFAULT 0`` watermark columns, so a
        # cohort already satisfied before this upgrade must be backfilled or it
        # can never arm.
        TaskStore._backfill_child_event_seq(conn)

    @staticmethod
    def _backfill_child_event_seq(conn: sqlite3.Connection) -> None:
        """Seed ``child_event_seq`` from durable history for pre-watermark rows.

        The counter only ever counts child terminal/blocked events. A database
        upgraded from before the watermark starts every parent at ``0``, so a
        parent whose cohort was already satisfied but whose wake was never
        delivered (the exact crash gap the reconciler heals) would stay at
        ``0 <= last_woken_seq(0)`` and never arm. Setting the counter to the
        count of children already in a blocked-or-terminal state makes the
        first reconciler pass arm any genuinely satisfied pre-migration cohort;
        ``last_woken_seq`` stays ``0``, and the live predicate check still gates
        an unsatisfied cohort, so the only effect on an already-delivered cohort
        is one harmless re-wake. Runs once, only when the columns were just
        added, so it never clobbers a live counter.
        """
        conn.execute(
            "UPDATE managed_tasks SET child_event_seq = ("
            "  SELECT COUNT(*) FROM managed_tasks AS child"
            "  WHERE child.parent_task_id = managed_tasks.task_id"
            "    AND child.state IN ('blocked', 'completed', 'cancelled', 'lost', 'exhausted')"
            ") WHERE EXISTS ("
            "  SELECT 1 FROM managed_tasks AS child"
            "  WHERE child.parent_task_id = managed_tasks.task_id"
            ")"
        )

    @staticmethod
    def _ensure_watermark_columns(conn: sqlite3.Connection) -> None:
        """Add the wake watermark columns forward-only, under the write lock.

        ``child_event_seq`` counts child terminal/blocked events observed by a
        parent; ``last_woken_seq`` records how far its manager has already been
        woken. A wake arms only while the former outruns the latter, so the two
        counters are all the state re-arm and idempotent re-scan need. Both
        carry a constant ``DEFAULT 0``, so ``ADD COLUMN`` is a legal forward
        migration where a CHECK-adding change would need a full rebuild.
        """
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(managed_tasks)").fetchall()
        }
        added = False
        for column in ("child_event_seq", "last_woken_seq"):
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE managed_tasks ADD COLUMN {column} "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                added = True
        if added:
            # Only on the first upgrade, when the counter was just introduced -
            # never on a re-open, which would clobber live counters.
            TaskStore._backfill_child_event_seq(conn)

    @staticmethod
    def _ensure_effect_retry_at(conn: sqlite3.Connection) -> None:
        """Add the provider reset timestamp to existing effect journals."""
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(task_effects)").fetchall()
        }
        if "retry_at" not in columns:
            conn.execute("ALTER TABLE task_effects ADD COLUMN retry_at REAL")

    @staticmethod
    def _ensure_liveness_columns(conn: sqlite3.Connection) -> None:
        """Add the kernel v1.1 liveness columns forward-only, under the write lock.

        ``liveness`` is the typed classification the detector writes;
        ``liveness_episode`` names the current stall episode, ``liveness_since``
        marks when the current episode *phase* began (episode open, then reset
        at each wake so the escalation clock is "one further progress_timeout
        after the manager was told"), ``liveness_wakes`` caps an episode at two
        wakes, and ``liveness_evidence`` carries the JSON the wake payload
        quotes - including the detector's own confidence, so a degraded read is
        never dressed up as a typed one. ``over_budget`` is a soft latch that
        never changes a task's state.

        Every column has a constant default, so ``ADD COLUMN`` is legal here
        where the widened ``task_wakes`` CHECK needed a rebuild. No backfill:
        the defaults ("no finding yet", episode 0) are exactly right for a task
        the detector has never looked at.
        """
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(managed_tasks)").fetchall()
        }
        for column, definition in _LIVENESS_COLUMNS:
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE managed_tasks ADD COLUMN {column} {definition}"
                )

    @staticmethod
    def _migrate_task_wakes(conn: sqlite3.Connection) -> None:
        """Create ``task_wakes`` and bind uniqueness to *live* wakes only.

        A pre-fix table carries a column-level ``UNIQUE`` on ``dedupe_key`` that
        makes a manager wake once per predicate per epoch for the lifetime of
        the row, so multi-wave managers hang after wave one. The constraint
        cannot be dropped in place, so the table is rebuilt without it and a
        partial unique index rebinds uniqueness to un-consumed wakes. Widening
        the ``predicate`` CHECK for the v1.1 liveness predicates is the same
        kind of change and rides the same rebuild. The caller holds
        ``BEGIN IMMEDIATE``; running this twice is a no-op.
        """
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'task_wakes'"
        ).fetchone()
        if existing is None:
            conn.execute(_TASK_WAKES_DDL.format(name="task_wakes"))
        else:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(task_wakes)").fetchall()
            }
            needs_rebuild = (
                _TASK_WAKES_LEGACY_UNIQUE in str(existing["sql"])
                or "event_seq" not in columns
                or _TASK_WAKES_V11_PREDICATE not in str(existing["sql"])
            )
            if needs_rebuild:
                # Carry only what the old table actually has, in the new
                # table's order: a database mid-way through the kernel-v1
                # migrations has ``event_seq`` and one before it does not, and
                # naming a missing column would fail the whole upgrade.
                carried = ", ".join(
                    name
                    for name in (*_TASK_WAKES_LEGACY_COLUMNS, "event_seq")
                    if name in columns
                )
                conn.execute(f"DROP TABLE IF EXISTS {_TASK_WAKES_REBUILD_TABLE}")
                conn.execute(_TASK_WAKES_DDL.format(name=_TASK_WAKES_REBUILD_TABLE))
                conn.execute(
                    f"INSERT INTO {_TASK_WAKES_REBUILD_TABLE} ({carried}) "
                    f"SELECT {carried} FROM task_wakes"
                )
                conn.execute("DROP TABLE task_wakes")
                conn.execute(
                    f"ALTER TABLE {_TASK_WAKES_REBUILD_TABLE} RENAME TO task_wakes"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_wakes_pending "
            "ON task_wakes(state, created_at)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_wakes_live "
            "ON task_wakes(dedupe_key) WHERE state IN ('pending', 'claimed')"
        )
        # The liveness one-shot pre-check asks "has this exact incident already
        # been told to anyone?", across all states. Without this index that is
        # a full scan of every wake the outbox has ever held, run once per
        # actionable finding per tick.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_wakes_dedupe "
            "ON task_wakes(dedupe_key, state)"
        )

    @staticmethod
    def task_id(scope: str, key: str) -> str:
        return str(uuid.uuid5(TASK_NAMESPACE, f"{scope}\0{key}"))

    def reserve_all(
        self,
        *,
        scope: str,
        parent_task_id: str | None,
        specs: list[dict[str, Any]],
        max_attempts: int,
    ) -> list[tuple[TaskRecord, bool]]:
        if not specs:
            return []
        keys = [str(spec["key"]) for spec in specs]
        if len(keys) != len(set(keys)):
            raise TaskStoreError("A start batch cannot contain duplicate task keys")
        now = time.time()
        try:
            with self._database._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if parent_task_id:
                    parent = conn.execute(
                        "SELECT child_limit FROM managed_tasks WHERE task_id = ?",
                        (parent_task_id,),
                    ).fetchone()
                    if parent is None:
                        raise TaskStoreError(
                            "The current managed parent no longer exists"
                        )
                    existing_children = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM managed_tasks WHERE parent_task_id = ?",
                            (parent_task_id,),
                        ).fetchone()[0]
                    )
                    new_keys = sum(
                        conn.execute(
                            "SELECT 1 FROM managed_tasks WHERE scope = ? AND task_key = ?",
                            (scope, key),
                        ).fetchone()
                        is None
                        for key in keys
                    )
                    if existing_children + new_keys > int(parent["child_limit"]):
                        raise TaskStoreError(
                            f"Task child limit is {parent['child_limit']}; "
                            f"this batch would create {existing_children + new_keys}"
                        )

                reserved: list[tuple[TaskRecord, bool]] = []
                for spec in specs:
                    key = str(spec["key"])
                    task_id = self.task_id(scope, key)
                    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"))
                    row = conn.execute(
                        "SELECT * FROM managed_tasks WHERE scope = ? AND task_key = ?",
                        (scope, key),
                    ).fetchone()
                    inserted = row is None
                    if row is None:
                        conn.execute(
                            """
                            INSERT INTO managed_tasks
                            (task_id, scope, task_key, parent_task_id, state, epoch,
                             attempts, max_attempts, child_limit, spec_json,
                             created_at, updated_at)
                            VALUES (?, ?, ?, ?, 'reserved', 1, 0, ?, ?, ?, ?, ?)
                            """,
                            (
                                task_id,
                                scope,
                                key,
                                parent_task_id,
                                max_attempts,
                                int(spec.get("children", 0)),
                                encoded,
                                now,
                                now,
                            ),
                        )
                        row = conn.execute(
                            "SELECT * FROM managed_tasks WHERE task_id = ?", (task_id,)
                        ).fetchone()
                    elif str(row["spec_json"]) != encoded:
                        raise TaskStoreError(
                            f"Task {key!r} already exists with a different specification"
                        )
                    reserved.append((self._decode(row), inserted))
                conn.execute("COMMIT")
                return reserved
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot reserve managed tasks: {exc}") from exc

    def get(self, scope: str, key: str) -> TaskRecord | None:
        return self._one("scope = ? AND task_key = ?", (scope, key))

    def count_running(self, processes: Collection[dict[str, Any]]) -> int:
        """Count the executor's live coding-agent processes for admission.

        The process list is executor truth, so managed-task state, effect
        epoch, cleanup intent, and outgoing mail cannot alter the count.
        """
        try:
            return sum(
                process["status"] == "running" and process["run_reason"] == "codingagent"
                for process in processes
            )
        except (KeyError, TypeError) as exc:
            raise TaskStoreError("Malformed running execution-process response") from exc

    def record_cleanup_intents(
        self, task_id: str, epoch: int, session_id: str, commands: Iterable[Any]
    ) -> None:
        """Durably fence every live native command before terminalization.

        The command snapshot is taken under the task fence by the caller.  The
        idempotent inserts make a retry and a restarted reconciler converge on
        the same native rows rather than on process-local memory.
        """
        now = time.time()
        rows: list[tuple[str, str]] = []
        for command in commands:
            state = str(command.get("state", "") if isinstance(command, dict) else command.state)
            if state in {"done", "failed", "cancelled", "completed", "killed"}:
                continue
            command_id = str(command.get("id") if isinstance(command, dict) else command.id)
            rows.append(("command_cancel", command_id))
            process_id = command.get("execution_process_id") if isinstance(command, dict) else command.execution_process_id
            if process_id:
                rows.append(("execution_stop", str(process_id)))
        if not rows:
            return
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "INSERT OR IGNORE INTO task_cleanup_intents "
                    "(task_id, epoch, kind, native_id, session_id, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                    [(str(task_id), int(epoch), kind, native_id, str(session_id), now, now) for kind, native_id in rows],
                )
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot record terminal cleanup intents: {exc}") from exc

    def begin_outgoing_command(self, task_id: str, epoch: int, session_id: str, dedupe_key: str) -> None:
        """Persist the send admission before opening the task fence for HTTP."""
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO task_outgoing_commands "
                "(task_id, epoch, dedupe_key, session_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'sending', ?, ?)",
                (str(task_id), int(epoch), str(dedupe_key), str(session_id), now, now),
            )
            conn.execute("COMMIT")

    def settle_outgoing_command(
        self, task_id: str, epoch: int, dedupe_key: str, native_id: str | None, *, terminal: bool
    ) -> None:
        """Publish the native id, then bind a terminal send to durable cleanup."""
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE task_outgoing_commands SET native_id = COALESCE(?, native_id), "
                "state = ?, updated_at = ? WHERE task_id = ? AND epoch = ? AND dedupe_key = ?",
                (native_id, "cleanup" if terminal else "settled", now, str(task_id), int(epoch), str(dedupe_key)),
            )
            if terminal and native_id:
                row = conn.execute(
                    "SELECT session_id FROM task_outgoing_commands WHERE task_id = ? "
                    "AND epoch = ? AND dedupe_key = ?",
                    (str(task_id), int(epoch), str(dedupe_key)),
                ).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO task_cleanup_intents "
                    "(task_id, epoch, kind, native_id, session_id, state, created_at, updated_at) "
                    "VALUES (?, ?, 'command_cancel', ?, ?, 'pending', ?, ?)",
                    (str(task_id), int(epoch), str(native_id), str(row["session_id"]), now, now),
                )
            conn.execute("COMMIT")

    def terminal_outgoing_commands(self, task_id: str, epoch: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM task_outgoing_commands WHERE task_id = ? AND epoch = ? "
                "AND state IN ('sending', 'cleanup') ORDER BY created_at",
                (str(task_id), int(epoch)),
            ).fetchall()

    def unresolved_terminal_outgoing_commands(self) -> list[sqlite3.Row]:
        """Sends whose durable key survived terminalization without a native id."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT o.* FROM task_outgoing_commands AS o JOIN managed_tasks AS t "
                "ON t.task_id = o.task_id AND t.epoch = o.epoch "
                "WHERE t.state IN ('completed', 'cancelled', 'lost', 'exhausted') "
                "AND o.native_id IS NULL AND o.state IN ('sending', 'cleanup') "
                "ORDER BY o.created_at"
            ).fetchall()

    def pending_cleanup_intents(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM task_cleanup_intents WHERE state = 'pending' ORDER BY created_at"
            ).fetchall()

    def acknowledge_cleanup_intent(self, task_id: str, epoch: int, kind: str, native_id: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE task_cleanup_intents SET state = 'acknowledged', updated_at = ? "
                "WHERE task_id = ? AND epoch = ? AND kind = ? AND native_id = ? AND state = 'pending'",
                (time.time(), str(task_id), int(epoch), str(kind), str(native_id)),
            )
            if kind == "command_cancel":
                conn.execute(
                    "UPDATE task_outgoing_commands SET state = 'acknowledged', updated_at = ? "
                    "WHERE task_id = ? AND epoch = ? AND native_id = ? AND state = 'cleanup'",
                    (time.time(), str(task_id), int(epoch), str(native_id)),
                )
            conn.execute("COMMIT")

    def get_by_id(self, task_id: str) -> TaskRecord | None:
        return self._one("task_id = ?", (str(task_id),))

    def get_by_session(self, session_id: str) -> TaskRecord | None:
        return self._one("holder_session_id = ?", (str(session_id),))

    def list_scope(self, scope: str) -> list[TaskRecord]:
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM managed_tasks WHERE scope = ? ORDER BY created_at",
                    (scope,),
                ).fetchall()
            return [self._decode(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot list managed tasks: {exc}") from exc

    def list_state(self, state: str) -> list[TaskRecord]:
        """Every task in one lifecycle state, across every scope.

        Reconcile sweeps are keyed on durable state rather than on a live
        session, because the states that need sweeping are exactly the ones
        where no live session exists to key on.
        """
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM managed_tasks WHERE state = ? ORDER BY updated_at",
                    (str(state),),
                ).fetchall()
            return [self._decode(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot list managed tasks: {exc}") from exc

    def children(self, parent_task_id: str) -> list[TaskRecord]:
        try:
            with self._database._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM managed_tasks WHERE parent_task_id = ? "
                    "ORDER BY created_at",
                    (str(parent_task_id),),
                ).fetchall()
            return [self._decode(row) for row in rows]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot list managed children: {exc}") from exc

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Lend the store's connection so a caller can bundle its own writes.

        The wake outbox needs the child transition and the wake row in one
        transaction; without a shared connection they would be two.
        """
        with self._database._connect() as conn:
            yield conn

    def transition(
        self,
        task_id: str,
        *,
        expect_states: frozenset[str],
        expect_version: int | None,
        assign: str,
        values: tuple[object, ...],
        attempted: str,
        fence: TaskFence | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TaskRecord:
        """Apply one state change only if the observed task still holds.

        Every mutation bumps ``version``, so a caller that read the row can
        prove nothing moved underneath it by passing ``expect_version``.
        """
        self._require_fence(task_id, fence)
        if conn is not None:
            return self._transition(
                conn, task_id, expect_states, expect_version, assign, values, attempted
            )
        try:
            with self._database._connect() as owned:
                return self._transition(
                    owned,
                    task_id,
                    expect_states,
                    expect_version,
                    assign,
                    values,
                    attempted,
                )
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot update managed task: {exc}") from exc

    def _transition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        expect_states: frozenset[str],
        expect_version: int | None,
        assign: str,
        values: tuple[object, ...],
        attempted: str,
    ) -> TaskRecord:
        states = sorted(expect_states)
        placeholders = ", ".join("?" for _ in states)
        cursor = conn.execute(
            f"UPDATE managed_tasks SET {assign}, version = version + 1, updated_at = ? "
            f"WHERE task_id = ? AND state IN ({placeholders}) "
            "AND (? IS NULL OR version = ?)",
            (
                *values,
                time.time(),
                str(task_id),
                *states,
                expect_version,
                expect_version,
            ),
        )
        row = conn.execute(
            "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
        ).fetchone()
        if row is None:
            raise TaskStoreError("Managed task not found")
        if cursor.rowcount != 1:
            raise StaleTransition(self._decode(row), attempted)
        return self._decode(row)

    def _require_fence(self, task_id: str, fence: TaskFence | None) -> None:
        if fence is None or fence._store is not self or fence.task_id != str(task_id):
            raise TaskStoreError("Lifecycle state writes require the task fence")

    def activate(
        self,
        task_id: str,
        *,
        workspace_id: str,
        session_id: str,
        fence: TaskFence | None = None,
    ) -> TaskRecord:
        # BEGIN IMMEDIATE so the guarded UPDATE and its readback share one
        # transaction; otherwise a competing terminal writer can slip between
        # them and the returned record describes a row this call never wrote.
        if fence is None:
            with self.task_lock(task_id) as held:
                return self.activate(
                    task_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    fence=held,
                )
        self._require_fence(task_id, fence)
        return self._transaction(
            expect_states=ACTIVATE_PREDECESSORS,
            task_id=task_id,
            expect_version=None,
            assign="state = 'active', workspace_id = ?, holder_session_id = ?",
            values=(str(workspace_id), str(session_id)),
            attempted="active",
        )

    def prepare_replacement(
        self,
        task_id: str,
        *,
        target: dict[str, Any] | None = None,
        charge_failure: bool = False,
        expect_version: int | None = None,
        fence: TaskFence | None = None,
    ) -> TaskRecord:
        if fence is None:
            with self.task_lock(task_id) as held:
                return self.prepare_replacement(
                    task_id,
                    target=target,
                    charge_failure=charge_failure,
                    expect_version=expect_version,
                    fence=held,
                )
        self._require_fence(task_id, fence)
        try:
            with self._database._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
                ).fetchone()
                if row is None:
                    raise TaskStoreError("Managed task not found")
                task = self._decode(row)
                if task.attempts >= task.max_attempts:
                    raise TaskStoreError(
                        f"Task {task.key!r} tripped its {task.max_attempts}-attempt "
                        "circuit breaker"
                    )
                spec = task.spec if target is None else {**task.spec, "target": target}
                prepared = self.transition(
                    task_id,
                    expect_states=REPLACE_PREDECESSORS,
                    expect_version=expect_version,
                    # A replacement is a fresh subject, so every observation
                    # about the predecessor is cleared in the same guarded
                    # transition that bumps the epoch. Inheriting them made a
                    # successor's own stall invisible forever (its episode was
                    # already open and already fully woken), reported a
                    # brand-new worker as over budget on its first tick, and
                    # dated the replacement's silence from its predecessor's.
                    assign=(
                        "state = 'replacing', epoch = epoch + 1, "
                        + ("attempts = attempts + 1, " if charge_failure else "")
                        + "spec_json = ?, "
                        "liveness = 'live', liveness_episode = 0, "
                        "liveness_since = NULL, liveness_wakes = 0, "
                        "liveness_evidence = NULL, over_budget = 0, "
                        "checkpoint_at = NULL"
                    ),
                    values=(json.dumps(spec, sort_keys=True, separators=(",", ":")),),
                    attempted="replacing",
                    fence=fence,
                    conn=conn,
                )
                conn.execute("COMMIT")
                return prepared
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot prepare task replacement: {exc}") from exc

    def checkpoint(self, task_id: str, checkpoint: str) -> TaskRecord:
        # Same atomicity as activate(): the readback must see this call's own
        # write, not a row a concurrent transition moved after the UPDATE.
        # ``checkpoint_at`` is stamped alongside because it is the one piece of
        # progress evidence the kernel owns rather than reads from the
        # executor; ``updated_at`` cannot stand in for it, since the detector's
        # own writes move that and a task would then look alive because it was
        # being watched.
        return self._transaction(
            expect_states=LIVE_STATES,
            task_id=task_id,
            expect_version=None,
            assign="checkpoint = ?, checkpoint_at = ?",
            values=(checkpoint, time.time()),
            attempted="checkpoint",
        )

    def record_liveness(
        self,
        task_id: str,
        liveness: str,
        *,
        evidence: str | None = None,
        now: float | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TaskRecord:
        """Open, continue, or close a stall episode with one guarded transition.

        **An episode boundary is a change of behaviour, never a change of
        opinion.** The spec is explicit: an episode opens when the condition
        first holds and closes when progress evidence resumes (liveness-spec.md
        line 46). So the boundary is computed over the *stretch* a task is in -
        working, parked, or silent - and not over the classification string.
        Re-labelling one unbroken silent stretch from ``stalled`` to ``limbo``
        and back, which one flaky snapshot read is enough to cause, updates the
        reason in place: same episode, same phase clock, same wake count.

        Treating any relabelling as a boundary minted a fresh episode on every
        tick, and because each new episode resets ``liveness_wakes`` to zero
        the escalation that fires at two wakes could never arrive: a wedged
        child produced one wake per tick, forever, and no human was ever told.

        ``parked`` is its own stretch rather than part of the silent one. It
        arms no stall predicate, and its phase clock is what the approval
        timeout is measured from, so it must start when the parking did.

        Two things this write deliberately does *not* do. It does not touch
        ``version``: an observation about a task is not a change to the task,
        and bumping the version would make every detector tick invalidate a
        manager's in-flight ``expect_version`` read - the detector would cause
        the conflicts it exists to report. It does not touch ``updated_at``
        either, for the same reason ``checkpoint_at`` exists: a task must not
        look recently active merely because something looked at it.

        ``LIVE_STATES`` guards the write, so a terminal task cannot acquire a
        new liveness finding and a late tick can never contradict a recorded
        outcome.
        """
        if liveness not in LIVENESS_STATES:
            raise ValueError(f"Unsupported liveness classification: {liveness}")
        moment = time.time() if now is None else now
        states = sorted(LIVE_STATES)
        placeholders = ", ".join("?" for _ in states)

        def apply(active: sqlite3.Connection) -> TaskRecord:
            row = active.execute(
                "SELECT liveness, liveness_episode FROM managed_tasks WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            if row is None:
                raise TaskStoreError("Managed task not found")
            current = str(row["liveness"])
            if liveness_stretch(current) == liveness_stretch(liveness):
                # Same stretch: the reason may be refined, but nothing about
                # the incident restarted.
                assign = "liveness = ?, liveness_evidence = ?"
                values = (liveness, evidence)
            else:
                closing = liveness == "live"
                assign = (
                    "liveness = ?, liveness_episode = ?, liveness_since = ?, "
                    "liveness_wakes = 0, liveness_evidence = ?"
                )
                values = (
                    liveness,
                    int(row["liveness_episode"]) + (0 if closing else 1),
                    None if closing else moment,
                    None if closing else evidence,
                )
            active.execute(
                f"UPDATE managed_tasks SET {assign} "
                f"WHERE task_id = ? AND state IN ({placeholders})",
                (*values, str(task_id), *states),
            )
            updated = active.execute(
                "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
            ).fetchone()
            return self._decode(updated)

        if conn is not None:
            return apply(conn)
        try:
            with self._database._connect() as owned:
                owned.execute("BEGIN IMMEDIATE")
                record = apply(owned)
                owned.execute("COMMIT")
                return record
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot record task liveness: {exc}") from exc

    def record_over_budget(
        self, task_id: str, *, conn: sqlite3.Connection | None = None
    ) -> bool:
        """Latch the soft budget flag; return whether this call set it.

        A latch, not a level: budgets are evidence and only ever cross once per
        epoch, so the ``0 -> 1`` edge is the whole signal and a manager is told
        about it exactly once without any episode bookkeeping. The flag never
        changes task state - an over-budget task stays active, as the contract
        requires - and, like every liveness write, it leaves ``version`` alone.

        Epoch scoping comes from two places, and it needs both:
        ``prepare_replacement`` clears the latch so a successor starts unflagged,
        and ``LIVE_STATES`` guards this write so a late tick cannot flag a task
        that has already finished. Without the guard a completed task could
        acquire a budget finding after its own terminal.
        """
        states = sorted(LIVE_STATES)
        placeholders = ", ".join("?" for _ in states)

        def apply(active: sqlite3.Connection) -> bool:
            cursor = active.execute(
                "UPDATE managed_tasks SET over_budget = 1 "
                f"WHERE task_id = ? AND over_budget = 0 AND state IN ({placeholders})",
                (str(task_id), *states),
            )
            return bool(cursor.rowcount)

        if conn is not None:
            return apply(conn)
        try:
            with self._database._connect() as owned:
                owned.execute("BEGIN IMMEDIATE")
                latched = apply(owned)
                owned.execute("COMMIT")
                return latched
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot record task budget: {exc}") from exc

    def _transaction(
        self,
        *,
        task_id: str,
        expect_states: frozenset[str],
        expect_version: int | None,
        assign: str,
        values: tuple[object, ...],
        attempted: str,
    ) -> TaskRecord:
        """Run one guarded transition inside its own ``BEGIN IMMEDIATE``."""
        try:
            with self._database._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                record = self._transition(
                    conn,
                    task_id,
                    expect_states,
                    expect_version,
                    assign,
                    values,
                    attempted,
                )
                conn.execute("COMMIT")
                return record
        except TaskStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot update managed task: {exc}") from exc

    def finish(
        self,
        task_id: str,
        state: str,
        result: str | None = None,
        *,
        expect_version: int | None = None,
        fence: TaskFence | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> TaskRecord:
        predecessors = FINISH_PREDECESSORS.get(state)
        if predecessors is None:
            raise ValueError(f"Unsupported task finish state: {state}")
        if fence is None:
            with self.task_lock(task_id) as held:
                return self.finish(
                    task_id,
                    state,
                    result,
                    expect_version=expect_version,
                    fence=held,
                    conn=conn,
                )
        if state == "lost":
            return self._record_loss(
                task_id, result, expect_version=expect_version, fence=fence, conn=conn
            )
        if state == "exhausted":
            return self._record_exhaustion(
                task_id, result, expect_version=expect_version, fence=fence, conn=conn
            )
        return self.transition(
            task_id,
            expect_states=predecessors,
            expect_version=expect_version,
            assign="state = ?, result = ?",
            values=(state, result),
            attempted=state,
            fence=fence,
            conn=conn,
        )

    def _record_loss(
        self,
        task_id: str,
        result: str | None,
        *,
        expect_version: int | None,
        fence: TaskFence,
        conn: sqlite3.Connection | None,
    ) -> TaskRecord:
        """Charge one durable loss, or expose the final one as exhausted.

        A loss is terminal for an epoch. The guarded transition therefore also
        makes repeated reconciliation of that loss a no-op rather than a second
        budget charge.
        """
        owned = conn is None
        if owned:
            with self._database._connect() as opened:
                opened.execute("BEGIN IMMEDIATE")
                record = self._record_loss(
                    task_id, result, expect_version=expect_version, fence=fence, conn=opened
                )
                opened.execute("COMMIT")
                return record
        assert conn is not None
        row = conn.execute(
            "SELECT * FROM managed_tasks WHERE task_id = ?", (str(task_id),)
        ).fetchone()
        if row is None:
            raise TaskStoreError("Managed task not found")
        task = self._decode(row)
        if task.attempts >= task.max_attempts:
            raise TaskStoreError(
                f"Task {task.key!r} tripped its {task.max_attempts}-attempt circuit breaker"
            )
        exhausted = task.attempts + 1 >= task.max_attempts
        return self.transition(
            task_id,
            expect_states=FINISH_PREDECESSORS["lost"],
            expect_version=expect_version,
            assign=(
                "state = 'exhausted', attempts = attempts + 1, result = ?"
                if exhausted
                else "state = 'lost', attempts = attempts + 1, result = ?"
            ),
            values=(f"exhausted: {result or 'lost'}" if exhausted else result,),
            attempted="exhausted" if exhausted else "lost",
            fence=fence,
            conn=conn,
        )

    def exhaust(
        self,
        task_id: str,
        reason: str,
        *,
        expect_version: int,
        fence: TaskFence,
    ) -> TaskRecord:
        """Persist the final failure as the immutable exhausted outcome."""
        self._require_fence(task_id, fence)
        return self.finish(
            task_id, "exhausted", reason, expect_version=expect_version, fence=fence
        )

    def _record_exhaustion(
        self,
        task_id: str,
        result: str | None,
        *,
        expect_version: int | None,
        fence: TaskFence,
        conn: sqlite3.Connection | None,
    ) -> TaskRecord:
        if conn is None:
            return self._transaction(
                task_id=task_id,
                expect_states=FINISH_PREDECESSORS["exhausted"],
                expect_version=expect_version,
                assign="state = 'exhausted', attempts = attempts + 1, result = ?",
                values=(f"exhausted: {result or 'failed'}",),
                attempted="exhausted",
            )
        return self.transition(
            task_id,
            expect_states=FINISH_PREDECESSORS["exhausted"],
            expect_version=expect_version,
            assign="state = 'exhausted', attempts = attempts + 1, result = ?",
            values=(f"exhausted: {result or 'failed'}",),
            attempted="exhausted",
            fence=fence,
            conn=conn,
        )

    def _one(self, where: str, params: tuple[object, ...]) -> TaskRecord | None:
        try:
            with self._database._connect() as conn:
                row = conn.execute(
                    f"SELECT * FROM managed_tasks WHERE {where}", params
                ).fetchone()
            return self._decode(row) if row is not None else None
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError(f"Cannot read managed task: {exc}") from exc

    @staticmethod
    def _decode(row: sqlite3.Row) -> TaskRecord:
        try:
            return TaskRecord(
                task_id=str(row["task_id"]),
                scope=str(row["scope"]),
                key=str(row["task_key"]),
                parent_task_id=row["parent_task_id"],
                state=str(row["state"]),
                epoch=int(row["epoch"]),
                attempts=int(row["attempts"]),
                max_attempts=int(row["max_attempts"]),
                child_limit=int(row["child_limit"]),
                spec=json.loads(row["spec_json"]),
                workspace_id=row["workspace_id"],
                holder_session_id=row["holder_session_id"],
                checkpoint=row["checkpoint"],
                result=row["result"],
                version=int(row["version"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
                liveness=str(row["liveness"]),
                liveness_episode=int(row["liveness_episode"]),
                liveness_since=(
                    float(row["liveness_since"])
                    if row["liveness_since"] is not None
                    else None
                ),
                liveness_wakes=int(row["liveness_wakes"]),
                liveness_evidence=row["liveness_evidence"],
                over_budget=bool(row["over_budget"]),
                checkpoint_at=(
                    float(row["checkpoint_at"])
                    if row["checkpoint_at"] is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStoreError(f"Corrupt managed task record: {exc}") from exc
