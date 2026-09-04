# PR #106 adversarial review, round 1

Reviewed `a659a4d3f3907d52a7654fca80bd3c81bfa967a7` against its `main` parent
`e73c1f0ca8989c37fd99cb08f3b8bd6ccbd5890b` and issue #55.

## Verdict: BLOCK

The implementation correctly keeps external process control out of SightMesh,
uses one subscription-derived dedupe key, makes the lease/subscription database
inserts one SQLite transaction, fences state-changing transitions, and preserves
receipt/loss before releasing the lease.  However, two P1 holes violate the
durable lease and unreachable-parent acceptance criteria.  The test suite is
green but does not exercise either path.

### P1 — a released output-root lease can never be acquired again

- Location: `src/sightmesh/escalation.py:371-386`; release at
  `src/sightmesh/external_runs.py:301-304`.
- Failure path: run A completes, `mark_notified()` changes its lease to
  `released`, and the runner cleans its root to an empty directory.  Run B then
  subscribes with that root.  Both `external_run_leases.output_root` (the table
  primary key) and `external_run_subscriptions.output_root` (`UNIQUE`) retain
  A forever, so B's insert fails even though the code declared the lease
  released.  The same behavior occurs after a reconciled `lost/unknown`.
- Impact: output roots are permanently consumed, contradicting the output-root
  *lease* lifecycle and preventing a later legitimate writer from obtaining the
  released root.
- Smallest robust fix: model claims as lease instances and enforce uniqueness
  only for active claims (for example a partial unique index on active
  `output_root`), while retaining subscription history without a unique output
  root.  Make the migration rebuild/replace the new tables safely if their
  first released row may already exist.  Add a test that reclaims the same root
  after both terminal receipt delivery and reconciled loss.

### P1 — temporary parent unreachability becomes a permanently undelivered wake

- Location: `src/sightmesh/external_runs.py:416-419`; parked branch at
  `src/sightmesh/escalation.py:946-954`.
- Failure path: terminal evidence is preserved while `client.session(parent)`
  transiently fails.  `escalate()` inserts a local parked escalation and returns
  `parked`; `reconcile_one()` immediately calls `mark_notified()`.  Future
  reconciles omit the subscription because `pending()` selects only states
  other than `notified` (`external_runs.py:222-227`).  `EscalationStore.resolve`
  merely marks the parked record resolved (`escalation.py:611-635`); it does not
  issue the cdesktop follow-up.  When the same parent becomes reachable, no
  durable command is ever sent.
- Impact: this fails issue #55's "unreachable-parent recovery" and the required
  at-least-once wake through cdesktop's durable-command path.  Parking proves
  the observation was retained, not that the recipient received it.
- Smallest robust fix: separate evidence/lease closeout from delivery state.
  Keep a parked delivery retryable until cdesktop confirms the stable dedupe key
  is queued, then atomically record delivery; or give the parked escalation an
  explicit retry/retarget operation that issues that same keyed cdesktop
  command when a valid destination is available.  Test unreachable -> parked ->
  parent reachable -> one queued command, including repeated reconciles.

### P2 — required adversarial coverage is absent

- Location: `tests/test_external_runs.py:63-185` and
  `tests/simulator/test_external_run_scenarios.py:51-90`.
- The simulator supplies only S18--S21 (restart/receipt, PID reuse, duplicate
  receipt delivery, and initial parking).  It has no scenarios for the named
  lease/subscription crash window, competing root claims, stale version actor,
  different-writer bind, launch failure, or parked recovery.  The unit tests
  also do not prove an existing escalation store can be opened/migrated twice,
  nor that a stale actor must provide an observed version: `bind()` accepts
  `expect_version=None` at `external_runs.py:229-256`.
- Impact: the acceptance checklist's races and migration proof are not
  demonstrated; the two P1 failures above therefore pass the current suite.
- Smallest robust fix: add one fault-injected simulator scenario per named race
  and explicit existing-store/twice migration coverage.  Require observed
  version for non-idempotent runner transitions, or derive a fenced writer epoch
  in the subscription so a restarted stale actor cannot make an unfenced write.

## Evidence and checks

- Ownership/non-goal inspection: `external_runs.py` only observes `ps`, reads a
  receipt, stores state, and sends a wake; it contains no external launch, kill,
  restart, retry, or domain outcome interpretation path.
- Atomicity inspection: lease and subscription inserts execute after one
  `BEGIN IMMEDIATE` and before its `COMMIT` (`external_runs.py:158-189`).
- Tests: `uv run --with pytest pytest tests/test_external_runs.py -q` — 10
  passed; `uv run --with pytest pytest tests/simulator -q` — 34 passed; full
  `uv run --with pytest pytest -q` — 533 passed.
- Negative control: temporarily removed the fingerprint equality at
  `external_runs.py:426`; the unit PID-reuse test and S19 each failed by leaving
  the outcome unset.  The guard was restored and the worktree was clean before
  the final runs.
