# Final subscription hot-swap adversarial rereview

Date: 2026-08-18 America/Los_Angeles
Scope: exact-head, read-only review of SightMesh and cdesktop draft PRs.

## Exact-head / PR matrix

All entries below were independently checked with explicit `gh-axi ... --repo clarkipeng/<repo>` calls. Every PR was `open`, `draft: yes`, and its head SHA matched the required SHA. PR #9's abbreviated input was resolved to the full SHA shown here.

| Lane | Repository / PR | Required and observed head | Base | PR state |
|---|---|---|---|---|
| C2 | sightmesh #19 | `be40617b0d232cfa02d11a59b3192b00a1591f11` | `cdt/5709-lane-c-settings` | open, draft |
| D | sightmesh #23 | `fdf12e0c6552d2dafd54b4e3893f6dd6a70b3ea2` | `cdt/1ebb-lane-c2-routing` | open, draft |
| K | sightmesh #22 | `fa2defe148e06e3e2f6ba4df45dc3b5b7b973f0d` | `main` | open, draft |
| L | sightmesh #24 | `8bd82e7c14c4b358da0cb1dfaa34417082500ae4` | `cdt/fb85-lane-k-parent-es` | open, draft |
| A1 | cdesktop #7 | `c2a9c2eaacfdd4b2dea066c95793faf755b834be` | `cdt/1f2c-lane-a-outcome-c` | open, draft |
| B | cdesktop #10 | `96960fbe4ab1ecc7feea22d6bc9b1ab7eee03a34` | `cdt/1879-lane-a-contract` | open, draft |
| E | cdesktop #6 | `fa9600cf34c67d89ff82287f76f1cd6cd35116ed` | `cdt/13da-cdesktop-format` | open, draft |
| I | cdesktop #8 | `6defea82b0436970f382ab7191679a8cafc55628` | `main` | open, draft |
| J | cdesktop #9 | `0ca04288e5cd988bf3a3776923715702ac87bd6d` | `main` | open, draft |

## Findings ordered by severity

### No confirmed blocker found

No source-level defect was confirmed in this rereview. Therefore no narrow fix worker is required by this review.

### Release-gate gaps and deferred evidence

1. B’s `163 passed / 0 failed` focused total is worker-reported, not independently rerun. The reconciliation records the manager independently verified head, ancestry, cleanliness, and contract shape, but did not rerun the tests because of the resource guard. This is an evidence limitation, not a source defect.

2. E has only independently observed `git diff --check`. `tsc` and `prettier` were unavailable (`spawn ENOENT`), web checks and formatting did not run, and GitHub has no CI for the stacked non-`main` base. E’s own handoff states that B has no outcome read route at `96960fbe`; the UI does not fabricate normalized outcome data. This remains a release integration gate for the eventual backend read surface.

3. I has a narrow independently rerun result of `1 passed, 0 failed, 48 filtered`. The PR also reports formatting passed. The broader cdesktop PR check summary is not a substitute for a full suite.

4. J’s focused regression, `cargo check -p server --lib`, and cargo formatting are worker-reported. The reconciliation also records the lost callback/transient stale-running harness evidence; it is relevant to release operations but did not establish a product defect in J’s cleanup patch.

5. A1’s recorded focused test results are worker/manager handoff evidence; the manager independently checked the exact head, cleanliness, ancestry, and contract shape, not a fresh test run.

6. C2’s PR body reports its focused routing suite as 22 passed; no independent rerun was performed in this rereview. D’s full suite was independently rerun at exact head: 217 passed, 0 failed. K’s focused escalation/CLI suites were independently rerun: 55 passed, 0 failed; K’s 199-test full-suite number remains worker-reported. L’s full suite was independently rerun at exact head: 204 passed, 0 failed.

## Attack verdicts

1. **Concurrent reconciler/dispatcher claims, stale completion, one logical command / one active attempt / one terminal winner — PASS at reviewed contract level.** A1’s `SessionCommand::claim_pending` checks for an existing claimed attempt and claims pending rows transactionally, increments `attempt_number`, and preserves the logical command/dedupe key. `ExecutionProcess::complete_running_attempt` updates only a running process, returning a single winner; B extends the same transaction to first-writer-wins outcome persistence. The exact tests are present in `crates/db/src/models/session_command.rs` and `crates/db/src/models/execution_process.rs` in cdesktop PRs #7/#10. Evidence caveat: A1/B test counts are not both independently rerun.

2. **Restart recovery and durable resume, including approval `ask` exactly once and `auto`/`never` — PASS with evidence caveat.** D’s reconciler persists requeue/quarantine state and preserves the dedupe key across restart. B’s `metered_approvals` schema has a partial unique pending index; `MeteredApproval::gate` creates/holds one `ask` approval, `respond` is `WHERE state = 'pending'`, `consume_approval` stamps one approved row to one execution process, `auto` records an `auto_started` row, and `never` creates a durable blocked record. These paths are covered in cdesktop PR #10 tests and D tests. No duplicate approval or launch defect was confirmed; B’s broad counts remain worker-reported.

3. **Atomic rejected teammate spawn — PASS.** I’s focused test was independently rerun at exact head: 1 passed, 0 failed, 48 filtered. The regression asserts rejected cross-executor spawn creates neither a session nor an execution process.

4. **Retirement quarantine and successor race — PASS at exact D head.** D persists retired/superseded ownership before successor side effects, cancels rather than requeues quarantined commands, rejects later message/steer/prompt-idle/bridge delivery, links one successor, and forwards the original dedupe key. Ordinary completed turns remain resumable. D’s full suite was independently observed at 217 passed, 0 failed. The relevant implementation/test evidence is `src/sightmesh/succession.py`, `src/sightmesh/durable.py`, and `tests/test_succession.py` at `fdf12e0c`.

5. **Parent escalation and intents — PASS; L blocker fix confirmed.** K durably records launcher identity and parks delivery when no confirmed live, non-archived parent exists. L’s `src/sightmesh/escalation.py` adds `classify_escalation`: explicit leading `BLOCKED`/`DECISION` tags map to `kind=interrupt`, `intent=replace`; all other STATUS/completion messages map to `kind=routine`, `intent=continue`. Delivery records a durable acknowledgment keyed by the escalation dedupe key. The focused tests cover routine non-interruption, replace behavior, restart persistence, and idempotence. L’s full suite was independently observed at 204 passed, 0 failed.

6. **Secret leakage — PASS for the reviewed contract, with normal integration caution.** B persists opaque `auth_binding_id` references and safe aliases only. `ExecutorAction::without_provider_bindings` recursively strips provider env and Codex injection before persistence; `Redacted<T>` has redacting Debug/Display and no serialization implementation; `redact_text` scrubs known secret values from error/log text. E’s UI uses aliases and approval state, not auth-binding identifiers or secrets. No persisted-action, serialization, Debug/Display, error, log, API, UI, snapshot, or transcript leak was confirmed in the exact diffs. Auth-binding identifiers remain opaque and UI-hidden.

7. **E API truthfulness — PASS.** Approval types/routes use the B contract, including `approved: boolean` and optional `reason`. E’s handoff explicitly records that B exposes no normalized-outcome read route and does not display a fabricated outcome. This is the correct gap behavior; adding a backend outcome read route remains a deferred integration gate.

## Explicit escalation-intents / L verdict

L `8bd82e7c14c4b358da0cb1dfaa34417082500ae4` fixes the manager-5 auto-resume evidence and the blocker in `.context/release-blocker-escalation-intents.md`: routine callbacks now queue with `intent=continue` and durable acknowledgment, while explicit BLOCKED/DECISION messages retain `intent=replace`. The manager-5 record’s observed auto-resume was a real prior defect; L’s exact-head source and independent 204-test rerun provide evidence that the fix is present. The fix remains stacked on K and must travel with K/D in release order.

## Confirmed defects requiring a narrow fix worker

None confirmed by this rereview.

## Gaps / deferred release gates

- Independently rerun or otherwise re-establish B’s focused test evidence when resources permit.
- Run E’s unavailable TypeScript/format/web checks and integrate a truthful backend outcome read route before claiming outcome display coverage.
- Complete the ordinary full-suite/CI evidence appropriate to the stacked branch topology, preserving the worker-reported versus independently observed distinction.
- Resolve the recorded harness callback/stale-running operational evidence before release sign-off; it is not a confirmed implementation defect in these heads.

## Final recommendation

The implementation heads are reviewable while remaining draft. No PR should be marked ready or merged solely on this rereview: retain draft status until the evidence gaps and deferred backend/UI integration gates above are closed, and merge L with K/D in the stated stack order.
