# PR #106 round 4 review

Reviewed exact head `59a4303a30a814edfbc2903c3fa3cd81b0188cf8` against
`origin/main` `eee10a462989cc3ee414b9aa41490e06a7cd865b`.

## Verdict

**APPROVE.** No P0, P1, or P2 findings.

The merge head contains `origin/main` (`git merge-base --is-ancestor` exited
zero). The external-run schema has one owner and one initialization path:
`EscalationStore._initialize()` holds `BEGIN IMMEDIATE` through external-run
migration/schema creation and its dependent indexes, then commits
([`src/sightmesh/escalation.py:414`](../src/sightmesh/escalation.py#L414)).
External-run subscription fields and diagnostics use the shared structural
redactor before they become durable
([`src/sightmesh/external_runs.py:145`](../src/sightmesh/external_runs.py#L145),
[`src/sightmesh/external_runs.py:333`](../src/sightmesh/external_runs.py#L333));
parked and acknowledged wake messages use that same redactor
([`src/sightmesh/escalation.py:737`](../src/sightmesh/escalation.py#L737),
[`src/sightmesh/escalation.py:835`](../src/sightmesh/escalation.py#L835)).

## Verification

- `origin/main` is an ancestor of the reviewed head; the #83 initialization,
  redaction, and schema ownership behavior is present.
- Python 3.13 isolated CI-equivalent install: `pytest -q` — **599 passed**.
- Focused round-1/2 external-run regressions and simulator:
  `tests/test_external_runs.py tests/simulator/test_external_run_scenarios.py`
  — **22 passed**. This covers released-root reacquire, parked-parent
  recovery, restart version fencing, and lost/unknown PID reuse.
- 12-way initialization boundary check passes normally. I removed only
  `BEGIN IMMEDIATE` locally, ran that test, and it failed (`cannot commit - no
  transaction is active`); I restored the exact source and re-ran it passing.
  The test also asserts every hook observes `conn.in_transaction`
  ([`tests/test_external_runs.py:183`](../tests/test_external_runs.py#L183)).
- Direct SQLite probe: supplied a bearer token and URL-userinfo token in the
  subscription/return path, parked its terminal wake, and queried both
  `external_run_subscriptions` and `escalations`. Neither secret was stored;
  the durable fields contained `[REDACTED]` and `***@` respectively.
- With the Python 3.13 environment on `PATH`, `./scripts/package-smoke.sh`
  and `./scripts/check-cdesktop-runtime.sh` both passed.
- GitHub checks for PR #106 at the reviewed head: **10 passed, 0 failed**
  (pinned runtime, advisory edge, and the 3.11/3.12/3.13 test matrix).
