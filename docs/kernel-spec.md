# Kernel v1 implementation spec

Implements `kernel-contract.md` in one PR against cdesktop 0.2.7's existing managed-launch capability.
Seam v2 (expiring native leases, owner fencing, capability probes beyond lookup) is Phase 4 and out of scope here.
Scope is the SightMesh repository only.

## Ground rules

- Keep the existing state enum (`reserved, active, replacing, blocked, completed, cancelled, lost`) to bound migration blast radius; the contract's names map onto it.
- The public CLI and SDK surface does not change: `start_all`, `send_all`, `show`, `complete`, `blocked`, `checkpoint`, `replace`, `cancel`, `archive`, `WorkerSpec`.
- Every schema change is a forward-only idempotent migration (SQLite table rebuild where CHECK constraints are added).
- No `git stash` anywhere; workers operate in their own worktrees.

## Schema (migration in `migration.py`)

Rebuild `managed_tasks` with these additions and the existing columns unchanged:

```sql
version INTEGER NOT NULL DEFAULT 0,
child_event_seq INTEGER NOT NULL DEFAULT 0,
last_woken_seq INTEGER NOT NULL DEFAULT 0,
CHECK (parent_task_id IS NULL OR parent_task_id != task_id)
```

`child_event_seq` counts child terminal/blocked events a parent has seen; `last_woken_seq` records how far its manager has been woken.
Both carry a constant `DEFAULT 0`, so they are added with `ALTER TABLE ADD COLUMN` (re-reading `table_info` under the same `BEGIN IMMEDIATE` lock the rest of the migration holds) rather than a table rebuild.

New tables:

```sql
CREATE TABLE IF NOT EXISTS task_effects (
    task_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'launched', 'terminal')),
    workspace_id TEXT,
    session_id TEXT,
    outcome TEXT,
    owner_instance TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (task_id, epoch)
);

CREATE TABLE IF NOT EXISTS task_wakes (
    wake_id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL,
    predicate TEXT NOT NULL CHECK (predicate IN ('all_children_terminal', 'any_child_blocked')),
    dedupe_key TEXT NOT NULL,
    event_seq INTEGER,
    state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'delivered', 'resolved')),
    claim_expires_at REAL,
    payload TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_wakes_pending ON task_wakes(state, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_wakes_live
    ON task_wakes(dedupe_key) WHERE state IN ('pending', 'claimed');
```

`dedupe_key` is `{parent_task_id}:{predicate}`; `INSERT OR IGNORE` makes wake creation idempotent.
Uniqueness binds only *live* wakes (the partial index), so a delivered or resolved wake's key can arm again for the next cohort transition; a manager waits once per cohort event, not once per predicate per epoch for the row's lifetime.
`event_seq` is the parent's `child_event_seq` at wake creation, and a wake arms only while `child_event_seq > last_woken_seq`.
Each child terminal/blocked transition bumps `child_event_seq` in the same transaction as the child's finish; a successful delivery advances `last_woken_seq` to the wake's `event_seq`, while a resolved (suppressed) wake leaves it untouched.
A genuinely new child event therefore outruns the watermark and re-arms exactly once, a resolved wake re-arms on the next pass because the watermark never moved, and a reconciler re-scanning an unchanged, already-delivered cohort finds `child_event_seq == last_woken_seq` and creates nothing.

## Guarded transitions (`task_store.py`)

Replace `_update` with one helper; `finish`, `checkpoint`, `activate`, and `replace` route through it:

```python
class StaleTransition(TaskStoreError):
    def __init__(self, current: TaskRecord, attempted: str): ...

def transition(
    self, task_id: str, *,
    expect_states: frozenset[str],
    expect_version: int | None,
    assign: str, values: tuple,
) -> TaskRecord:
    # UPDATE managed_tasks SET {assign}, version = version + 1, updated_at = ?
    # WHERE task_id = ? AND state IN {expect_states}
    #   AND (? IS NULL OR version = ?)
    # rowcount 0 -> SELECT row -> raise StaleTransition(current, attempted)
```

Legal `finish` predecessors:

| target | allowed current states |
|---|---|
| completed | active, replacing, blocked |
| blocked | active, replacing |
| cancelled | reserved, active, replacing, blocked |
| lost | reserved, active, replacing |

`completed`, `cancelled`, `lost` are immutable terminals: no transition lists them as a predecessor.
A rejected duplicate `complete()` raises `StaleTransition`; the SDK converts a duplicate with an identical target state into a no-op success (idempotent completion), and anything else into a visible error.
`replace` requires `expect_states={'active','blocked','lost'}` plus the caller's observed `version`, so two racing managers produce one winner and one `StaleTransition`.

## Effects journal (new `effects.py`, ~120 lines)

```python
@dataclass(frozen=True)
class Effect:
    task_id: str; epoch: int; request_hash: str; state: str
    workspace_id: str | None; session_id: str | None
    owner_instance: str; lease_expires_at: float

class EffectJournal:
    def reserve(self, task_id, epoch, request_hash, owner, ttl=120.0) -> tuple[Effect, bool]:
        # INSERT; on conflict return existing row.
        # Existing 'reserved' with expired lease: UPDATE owner/lease (fenced takeover), return (effect, True).
        # Existing 'reserved' with live lease and different owner: raise EffectBusy.
        # Existing with different request_hash: raise EffectConflict (spec drift, never silently relaunch).
    def mark_launched(self, task_id, epoch, workspace_id, session_id) -> Effect
    def mark_terminal(self, task_id, epoch, outcome) -> Effect
    def get(self, task_id, epoch) -> Effect | None
```

`request_hash` is sha256 over the canonical JSON of the launch spec (sorted keys).
`owner_instance` is a per-process UUID generated at SDK construction.

## Launch path (`sdk.py` `_start_reserved` rewrite)

```text
1. effect, took_over = journal.reserve(task.task_id, task.epoch, request_hash, owner)
2. if effect.state == 'launched': adopt -> store.activate(idempotent) -> return
3. client.managed_launch(task_id, epoch, request)   # existing cdesktop endpoint, idempotent on (task, epoch)
4. journal.mark_launched(ids)
5. store.activate(task_id, ...)  # guarded transition reserved|active -> active, idempotent re-run
```

A crash at any step leaves a row that step 1 or 2 resolves on retry; no path can create a second native session for the same `(task, epoch)`.
`_effect_ids` `lost` handling now also calls `journal.mark_terminal`.

## Wake outbox (new `wakes.py`, ~150 lines) and `sdk.py` lifecycle

`complete()` / `blocked()` become one transaction on one connection:

```text
BEGIN IMMEDIATE
  transition child row to terminal/blocked (guarded)
  evaluate parent predicate with SQL over child rows:
    all_children_terminal: no child of parent in ('reserved','active','replacing','blocked')
    any_child_blocked: this transition's target is 'blocked'
  if satisfied: INSERT OR IGNORE INTO task_wakes (pending, dedupe_key)
COMMIT
then: WakeDelivery.pump()   # best effort; reconciler is the safety net
```

Delete `_notify_parent` and its per-child mail entirely.
The known live bug (`intent="replace"` on blocked) disappears with it; delivery always uses `intent="continue"`.

```python
class WakeDelivery:
    def pump(self) -> int:
        # claim: UPDATE task_wakes SET state='claimed', claim_expires_at=now+60
        #        WHERE state='pending' OR (state='claimed' AND claim_expires_at < now)
        # payload: consolidated summary of ALL child rows of the parent (key, state, result)
        # client.send(parent.holder_session_id, payload, dedupe_key=wake_id, intent='continue')
        # mark 'delivered'
```

Suppressed deliveries (parent gone, no holder, holder == child session) mark the wake `resolved` with a reason in `payload`; never a silent return.

## Reconciler (`durable.py` extension, ~60 lines)

Add one pass alongside the existing command reconciliation:

- deliver `task_wakes` rows pending or claim-expired (crash between commit and pump);
- re-arm any parent whose watermark still trails its child events (`child_event_seq > last_woken_seq`) and whose predicate holds, so a wake resolved while the parent was undeliverable arms again on the next pass;
- expire `task_effects` reservations past their lease with no native session (mark terminal `lost:reservation-expired`).

## Capability check (`sdk.py` `_require_contract`)

Replace the advertised-int read with a probe: call the managed-launch lookup for a reserved sentinel task id and require a well-formed not-found response.
A runtime that advertises the contract but exposes no lookup to probe fails closed: there is no "take it at its word" answer, because that is exactly the state that lets a launch path fail late.

## Simulator (`tests/simulator/`, standalone, `-m simulator`)

`FakeCdesktop` implements the client seam in-process with fault injection: `kill_after(step)`, `duplicate_call(step)`, latency, and `reject_after(step, status, retry_after)` for any typed rejection.
Scenarios, each one test with a docstring naming the historical incident it replays:

| id | scenario | must hold |
|---|---|---|
| S1 | duplicate late `complete()` on a blocked task | terminal state unchanged; idempotent no-op or StaleTransition |
| S2 | crash between state change and notify | impossible by construction; wake row committed atomically, reconciler delivers |
| S3 | kill between native launch and activation | retry adopts the same workspace/session |
| S4 | 100 concurrent `start` on one key | exactly one native launch |
| S5 | 100 duplicate child-completion events | exactly one parent wake |
| S6 | one child blocks mid-cohort | parent turn survives (`intent=continue`); one wake |
| S7 | self-parent insert | IntegrityError from schema |
| S8 | stale-epoch writer after transfer | StaleTransition |
| S9 | child budget exceeded | rejected at reserve |
| S10 | typed 429 terminal | recorded as typed outcome on the effect, never inferred from text |
| S11 | 1,000 terminal tasks present | `show(key)` performs zero fleet scans (assert on fake call log) |
| S12 | two concurrent `replace` on one task | one winner, one StaleTransition |

Routing failover scenarios (`tests/simulator/test_routing_scenarios.py`), over the real pool, settings, store, and journal:

| id | scenario | must hold |
|---|---|---|
| S-D1 | typed 429 | one binding cooled, exactly one new epoch, none without a further outcome |
| S-D2 | second account of the same model | next account before next model; next model only once both are cooled |
| S-D3 | cooldown from `retry_at` | skipped until the reset, eligible again after it |
| S-D4 | failing test suite naming "429" | no reroute, no cooldown, epoch unchanged |
| S-D5 | typed 401 | short cooldown, not the capacity default; chain advances |
| S-D6 | typed 503 | whole pool cooled together; chain skips the rest of that provider |
| S-D7 | class with no usable hop | `validate` names it, dispatch refused before any epoch or effect row |
| S-D8 | explicit profile override | still advances; `automatic_failover` off blocks with a reason |

Red-first requirement: the suite must first run against pre-transplant `main` (77f66eb) with S1, S2, S3, S5, S6, S7, S12 failing, and the red log committed under `tests/simulator/RED-BASELINE.md`.

## Single-PR release checklist

- Full existing suite green (3.11-3.13) plus simulator green.
- Migration applied twice in a row on a copy of the real store (idempotency proof against `~/Documents/sightmesh-forensics-2026-09-01/sightmesh-db.v2.sqlite` schema).
- Version bump to 0.12.0 with release notes naming the six fixed defect classes.
- Diff check: `_notify_parent` and the blind `_update` are gone, not deprecated.
