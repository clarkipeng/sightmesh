# Liveness spec (kernel v1.1): stalls, limbo, loss, and silence

Extends `kernel-contract.md` and `kernel-spec.md`.
Principle: the kernel never kills and never respawns.
Every liveness failure becomes a typed observation that satisfies a wake predicate; the owning manager holds all replace/cancel authority, under the existing epoch fencing.
Wall-clock kill timers are banned: the incident record shows every automatic reaper caused damage (30-minute approval SIGKILLs, restart deaths misread as worker deaths, infrastructure failure recorded as result failure).

## Solutions per stall cause

| # | cause | detection signal | resulting state | action |
|---|---|---|---|---|
| 1 | turn ends, no lifecycle call | typed `turn_ended` event from executor with no `complete/blocked/checkpoint` in that turn and no pending queued mail | `idle_unreported` after a 2-minute grace | early wake to manager with last transcript tail |
| 2 | parked on approval | typed `parked(approval)` from executor (Phase 4 seam) | `parked` - excluded from stall detection | after `approval_timeout`: `blocked(approval)` + attention item; never a kill |
| 3 | provider limbo | executor reconciles stream vs process; live process emitting output bytes = progress; stream dead + process alive = `limbo` | `limbo` | executor re-attaches via durable execution handle; if unattachable, early wake with evidence |
| 4 | killed without terminal | executor lease-bound process truth; service restarts write a restart marker | `lost:restart` / `lost:oom` / `lost:killed` (terminal) | early wake (do not wait for siblings) + attention item |
| 5 | grinding without converging | resource budgets: turns, tokens, cost per task | `over_budget` (soft flag, task stays active) | early wake with usage evidence; manager judges - no heuristic content analysis, no auto-kill |
| 6 | waiting on unanswered mail | ack contract TTL (contract mailbox section): ask past its deadline unacknowledged | `awaiting_reply` flag on the asker | wake the counterparty's manager + attention item; escalation store rows carry deadlines |

Cause 1 nuance: a manager ending its turn with a live wait predicate is the *designed* behavior and is never `idle_unreported`.
The rule applies to tasks whose predicate set is empty (leaf workers) or fully satisfied.

## Progress evidence

Typed, executor-supplied, cheap to read:

- last transcript append timestamp
- last tool/command execution start or output-byte growth
- last `checkpoint()`
- parked/limbo/turn-ended markers above

`stalled` means: none of the above changed for `progress_timeout`, and the task is not `parked`, not `limbo`-attached, and its process is alive (dead process is `lost`, not `stalled`).

## Predicates (contract amendment)

Early-wake predicates join the existing two:

```text
all_children_terminal        (existing - consolidated cohort wake)
any_child_blocked            (existing)
any_child_lost               (new - terminal, but wakes immediately, not with the cohort)
any_child_stalled            (new - covers idle_unreported, limbo-unattachable, stalled)
any_child_over_budget        (new - soft)
```

All wakes are `intent="continue"`, deduped per stall *episode*: `{child_task_id}:{epoch}:{reason}:{episode}`.
An episode opens when the condition first holds and closes when progress evidence resumes; the same silent child cannot wake its manager twice in one episode.
If a manager's intervention does not restore progress within one further `progress_timeout`, the wake escalates to the attention queue for a human.

## WorkerSpec additions

```python
progress_timeout: float = 1500.0   # seconds without progress evidence; ~25 min default
approval_timeout: float | None     # None = inherit profile policy
budget: Budget | None              # max_turns, max_tokens, max_cost - evidence, not enforcement
```

Defaults live in trusted manager profiles; workers cannot weaken their own detection (same rule as child budgets).

## Detector mechanics

The stall detector is one additional pass in the existing `durable.py` reconciler - no new daemon:

1. For each `active` task, read progress evidence timestamps (executor metadata endpoints; metadata-only, zero Git).
2. Classify per the table above; write the typed flag on the task row via a guarded transition.
3. Evaluate the new predicates; `INSERT OR IGNORE` into `task_wakes` with the episode dedupe key.
4. The existing `WakeDelivery.pump()` delivers - one pipe, no second delivery path.

Executor prerequisites (Phase 4 seam, tracked in the seam contract):

- typed `turn_ended`, `parked`, `limbo` markers
- durable execution handles for long commands (re-attach instead of re-run)
- restart markers so `lost` attribution is honest
- lease-bound child processes so kills surface as typed terminals

Until Phase 4 lands, the detector runs in degraded mode on what 0.2.7 exposes (transcript timestamps, process liveness, command states) and marks its own confidence in the wake payload rather than guessing.

### Degraded mode today

Detectable now: `stalled` (no process, snapshot, or checkpoint timestamp advanced for `progress_timeout`), `limbo` (snapshot reports a dead stream over a running process), `over_budget` on turns and tokens, and `lost` when a process row carries an explicit `exit_reason` or a `killed` status.

Not detectable without the Phase 4 seam: `idle_unreported` and `parked`, which need the typed `turn_ended` and `parked(approval)` markers; `lost:restart` versus `lost:oom` versus `lost:killed`, which needs restart markers; cost budgets, which the client does not report.

A process that has merely vanished from a list read is `unknown`, never `lost` - a partial read looks identical, and no attribution is better than a wrong one.
Every finding carries `confidence` (`typed` or `degraded`) and its evidence sources into the wake payload.

## Simulator scenarios (v1.1 additions)

| id | scenario | must hold |
|---|---|---|
| S13 | leaf worker ends turn without lifecycle call | one `any_child_stalled` wake after grace; no kill; no respawn |
| S14 | child parked on approval past timeout | `blocked(approval)`; process alive; attention item; excluded from stall |
| S15 | process killed mid-turn (simulated restart) | `lost:restart` terminal; immediate wake; retry adopts, never duplicates |
| S16 | long command with live output for 2x progress_timeout | never flagged; output bytes are progress |
| S17 | same child stalls, manager nudges, child stays silent | exactly two wakes total: episode wake + escalation; then human attention |

## Rollout

1. Land the kernel v1 single PR first (in flight).
2. v1.1 lane: this spec + detector + predicates + S13-S17, one PR, same simulator gate.
3. Phase 4 seam adds the typed executor markers; detector leaves degraded mode.
