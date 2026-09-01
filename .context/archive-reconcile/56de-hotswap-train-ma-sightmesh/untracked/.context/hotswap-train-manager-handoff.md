# Subscription hot-swap train: manager handoff

Date: 2026-08-18 America/Los_Angeles
From: @hotswap-train-manager-4, session eac5e7ee (retiring at ~67.5% context)
To: single successor manager. You are the sole visible manager. Prior manager sessions are non-authoritative and must never be resumed.

## Hard rules carried forward

Never launch `sleep`, detached/background commands, `nohup`, shell `&`, or polling processes.
Never use a long-running terminal command as a monitor.
Use short foreground `sightmesh peers`, `sightmesh peek`, Git reads, and worker callbacks only.
Two prior managers were killed immediately after creating a detached polling shell.

Queuing `sightmesh message`/`steer` to a killed or completed agent AUTO-RESUMES it.
That already resurrected a retired A1 writer into a shared worktree once.
NEVER message, steer, or prompt any superseded session.
Only these session IDs are addressable: A1 `6246357a`, Lane I `c26b7986`, and yourself.

One writer per hotspot. All PRs stay draft.
No merge, ready, publish, workflow dispatch, secret mutation, or runtime-lock update.
Use the `orchestrate-visible-agents` skill to delegate and `reconcile-agent-work` before any lifecycle change.
No hidden or native subagents.

## Do this first, it is cleanup and not a merge action

Lane I mistakenly opened draft PR #11 against UPSTREAM `cdesktop-ai/cdesktop` at head `6defea82b0436970f382ab7191679a8cafc55628`.
Close it with a concise mistaken-target note.
The correct-repo equivalent ALREADY EXISTS and needs no re-push: `clarkipeng/cdesktop` draft PR #8, same exact head `6defea82`, base `main`.
I verified both heads match, so do not re-push or open a second PR.
Record on PR #8 that the focused Cargo test was BLOCKED BY SHARED BUILD LOCKS while formatting passed, so its test evidence is incomplete and must be re-run before any ready/merge decision.

## Exact state

### SightMesh, repo clarkipeng/sightmesh

- PR #17 lane G lease resilience, draft, head `e4f90f8c4745db911105b3b318f0a94d3aea16d0`, base main.
- PR #18 lane C settings/selector, draft, head `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`, base main, was green 6/6.
- PR #19 lane C2 selection safety, draft, head `be40617b0d232cfa02d11a59b3192b00a1591f11`, base `cdt/5709-lane-c-settings`, CI 6/6 green.
  I independently inspected the diff and re-ran its focused suite: 22 passed.
  It genuinely fixes Lane F F-1 trace redaction, F-2 non-secret selection including metered ask, F-3 truthful `routing validate`.
- PR #20 lane H docs, draft, head `4e5de2d95b971808b7b24711142a0a20156bf10c`, base `cdt/5709-lane-c-settings`, docs-only, marks B/D integration explicitly pending.
- PR #21 was a duplicate C2 from a rogue worker; I marked it superseded and it is now closed.
- PR #16 release candidate and PRs #12 to #15 remain draft and are out of tonight's implementation scope.

Do NOT relaunch H or C2. Both are done.

### cdesktop, repo clarkipeng/cdesktop

- PR #4 release distribution, draft. Must be rebased LAST, after all implementation, per plan.
- PR #5 frontend/format baseline, draft, head `41d37b261ada0d03b73e82cfd59d1fa39140a61b`.
  KNOWN GATE: 4 failing backend checks (backend-clippy, backend-remote-checks, backend-schema-checks, tauri-checks); frontend-checks passes.
  Structural cause: A0 `5d2f132f` is exactly the fix for those checks but sits on a separate branch off main, not stacked under PR #5.
  Per the merge plan this resolves when A0 lands after PR #5, but PR #5 can never be green alone. This is Lane F F-8 and is an OPERATOR decision: restack, waive, or accept.
- PR #6 lane E dashboard, draft, head `61f5f010d345a6fb5d55a6065c09ce6f37f733d6`, base `cdt/13da-cdesktop-format`.
  I reviewed it: frontend-only, zero files under `crates/`, zero `.rs`, zero migrations, secret scan clean, fixture correctly omits `auth_binding_id` from the UI projection.
  It reports NO CI checks at all; confirm whether that is path-filtering or missing frontend CI.
  E is parked deliberately and must not adapt until B's API fixture stabilizes.
- PR #7 lane A1, draft, head `c2a9c2eaacfdd4b2dea066c95793faf755b834be`, base `cdt/1f2c-lane-a-outcome-c`.
  A0 baseline is `5d2f132ff147a08f6879488eab2d6556e5a90dd3` on branch `cdt/1f2c-lane-a-outcome-c`; A0 base was `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
  Two commits on A0: `e2d6c966` single-winner execution attempts and opaque persisted actions, then `c2a9c2ea` persist session command attempts.
  Worktree is CLEAN and pushed. Workspace `18799b7e`, branch `cdt/1879-lane-a-contract`.
- PR #8 lane I, draft, head `6defea82`, base main. See cleanup above.

## Your queue, in order

1. Do the Lane I upstream cleanup above.
2. Reconcile A1 at exact head `c2a9c2ea`: read its report, verify its focused tests actually ran and passed with real counts, and extract its COMPACT CONSUMER CONTRACT.
   That contract is the blocking gate for B, D, and E. Do not accept an aspirational contract; it must be accurate at `c2a9c2ea`.
   Specifically confirm whether an opaque `auth_binding_id` field actually exists, or whether A1 did not add one. B needs the truth.
3. Launch B ONCE on A1 exact head `c2a9c2ea`. B owns cdesktop auth-binding resolution, redaction, normalized live adapter outcomes, and durable metered auto/ask/never approval and resume. Secrets resolve only immediately before launch.
4. Launch D ONCE from C2 exact head `be40617b` plus A1's frozen consumer fixture. D owns SightMesh autolaunch/reconciler, cooldown/requeue, restart recovery, and cross-executor successor linkage.
5. After B's API fixture stabilizes, adapt E from its exact head `61f5f010`. E must not edit backend contracts.
6. Launch a final READ-ONLY rereviewer against exact A1/B/C2/D/E heads for concurrency, restart, approval exactly-once, and secret leakage.

Add this to the Lane D and release gate scope: ownership transfer must QUARANTINE superseded sessions so queued delivery cannot auto-resume them into a shared worktree. That defect is proven, not hypothetical.

## Lane I finding to carry into the release gate

`crates/server/src/routes/teammates.rs` on cdesktop main validates cross-executor provider/model at line ~225, BEFORE `Session::create` at ~256 and `start_execution` at ~316.
So main's ordering is correct.
The observed defect, a 400 `EXECUTOR_REQUIRES_PROVIDER` that still created session `76494346` and running process `97194369`, came from the INSTALLED RUNTIME `cdesktop/0.2.5`, so it is an unshipped-runtime regression rather than missing main logic.
This reinforces that PR #4 and the runtime lock are LAST, after implementation, and that the final runtime artifact must contain Lane I's regression.

## Reference docs

- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-tonight-plan.md`
- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`
- `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/5509-lane-f-adversari/sightmesh/.context/lane-f-adversarial-review.md`

Stop condition: continue until every implementation lane has a clean pushed draft head with exact-head evidence, or a genuine operator choice is required.
