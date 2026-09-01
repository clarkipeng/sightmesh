# Subscription hot-swap tonight execution and merge plan

Date: 2026-08-18, America/Los_Angeles  
Objective: finish the implementation and review-ready draft PR stack tonight without shared-file races.  
Authority boundary: no merge, ready transition, release publication, workflow dispatch, secret change, or runtime-lock update to an unpublished artifact without explicit operator approval.

## Operating constraints

- One visible manager with compact durable ledger state.
- Visible SightMesh workers only. No hidden or native subagents.
- One writer for each shared contract, schema, app navigation, settings composition, or migration hotspot.
- Replace implementation workers at about 70 percent context after a pushed checkpoint.
- Review stable commits while non-overlapping implementation continues.
- Every handoff includes exact base, head, dirty state, owned paths, focused checks, remaining scope, and exclusions.
- Draft PRs are the delivery boundary. Checkpoints should be small and independently reviewable.
- Retirement is atomic: mark the session terminal, cancel every pending command, and make the scheduler reject later follow-ups before a successor can launch.
- Successor launch uses one durable handoff idempotency key. A delayed spawn response must resolve to the existing successor, never create a second manager workspace.

## Live failure evidence to preserve as fixtures

- At 2026-08-19 05:31 UTC, a delayed manager launch created workspace `dd768b81-ceb9-4188-b87f-cd5d063b87a0` after predecessor `829a06d5-1c46-4c21-a0b3-9b165aad97b2` had completed.
- The predecessor still held queued commands. Without manual cancellation, the scheduler could have resumed it beside the successor.
- Retired A1 session `6246357a-bd49-46c9-975e-2018bceb8937` did auto-resume at 2026-08-19 05:33 UTC from a pending command and occupied a global execution slot until its exact process was stopped.
- A replacement command remained queued while the global agent pool was full, then dispatched when a slot opened. Retirement correctness therefore cannot depend on prompt timing, process completion callbacks, or pool availability.
- Lane J session `fee3b91c-fcd4-4a61-97d5-8b3be360b1ed` and later Lane L session `5bf38c00-e97d-425d-a69d-f0d8137fa36c` were created without their manager parent while sibling B, K, and D sessions recorded the same manager correctly. Both links required repair through the typed session API. Spawn success must atomically persist launcher identity before returning.
- After Lane J delivered clean pushed head `0ca04288`, its child process exited while cdesktop temporarily kept execution `50de5d4f-d69c-4e55-af3d-8b8a73a6f195` in `running`, so no callback ran. Recovery later marked it completed, but a subsequent stop returned 500 `Child process not found for execution`. Stop must be idempotent for an already terminal or missing-child execution, and terminalization must dispatch eligible pending commands and preserve the completion callback.
- Lane D's routine completion `STATUS` callback used parent intent `replace` and killed the manager's in-progress Lane J reconciliation turn. Routine status/completion delivery must queue with acknowledgment and preserve the manager turn; only an explicit blocker or decision escalation may interrupt with `replace`.

## Tonight lanes

| Lane | Repository | Base/dependency | Ownership | Parallel status | Delivery |
|---|---|---|---|---|---|
| G: lease-sync resilience | SightMesh | `origin/main` `5622486f` | lease reconciliation and regression tests only | Complete, review now | Draft PR #17 |
| C: settings and selector | SightMesh | `origin/main` `5622486f` | versioned routing settings, selector, CLI, tests | Running | Draft PR, independent of cdesktop |
| A0: backend baseline | cdesktop | `origin/main` `62cbae3d` | SQLx CLI pin and exact DB clippy fixes | Checkpoint `5d2f132f` | Preserve as cherry-pickable commit and separate draft PR boundary |
| A1: outcome contract | cdesktop | A0 exact head | normalized outcome, logical command/attempt metadata, exact-once stale-attempt guards | Replace current high-context owner, then run | Draft stacked PR |
| B: auth bindings and approval backend | cdesktop | A1 reviewed head | auth-binding resolution, redaction, metered approval and resume | Launch after A1 contract commit | Draft stacked PR |
| D: auto-launch and reconciler | SightMesh | C reviewed head plus A1 API contract | spawn/teammate routing, cooldown, durable requeue, cross-executor handoff | Launch after C checkpoint and A1 contract fixture | Draft stacked PR |
| E: one-site dashboard | cdesktop | PR #5 head plus reviewed A1/B API fixtures | Agents navigation/view, Settings > Execution Routing, approval UI | Launch UI shell/fixtures tonight with no backend owner overlap | Draft stacked PR |
| F: adversarial review | both | each stable checkpoint | exact-once, restart, concurrent reconciliation, secret leakage, metered policy | Start immediately on plan/test fixtures, rereview exact heads later | Review report and requested fixes |
| H: docs and release integration | SightMesh | C/D stable heads | README, architecture, security, compatibility, release docs | Start after C contract freezes | Draft stacked PR or integration commit |
| I: atomic teammate spawn validation | cdesktop | `origin/main` | cross-executor request validation before session/process creation, regression test for zero side effects on rejection | Launch independently | Draft PR |
| J: atomic workspace start cleanup | SightMesh + cdesktop | current main heads | failed `/workspaces/start` cleanup when `container_ref` is absent; no active orphan survives | Launch independently with one writer per repo path | Draft PR stack |
| K: external launcher escalation | SightMesh | current main | capture Conductor launcher identity; durable parent fallback and decision inbox when no cdesktop parent exists | Launch independently before D | Draft PR |

## Critical path

```text
A0 backend baseline
  -> A1 cdesktop outcome/attempt contract
       -> B auth bindings + approvals
       -> D SightMesh auto-launch/reconciler (also depends C)
       -> E dashboard API integration (also depends PR #5)

C SightMesh settings/selector
  -> D auto-launch/reconciler
  -> E dashboard settings contract
  -> H docs

G lease resilience is independent and should land before further spawn-heavy work.
F reviews every stable head without owning implementation files.
I is independent and must land before relying on automatic cross-executor hot-swap.
J and K are independent foundations that must land before unattended manager recovery is considered durable.
```

## Checkpoints due tonight

### Checkpoint 1: foundations

- G PR #17 exact-head review complete.
- C settings implementation committed, tests passing, draft PR open.
- A0 remains a distinct cherry-pickable commit.
- A1 successor starts from `5d2f132f` with a compact file map and no repeated discovery.
- F reviewer starts with the plan, current tests, and stable heads.

### Checkpoint 2: contracts

- A1 exposes normalized outcomes and durable attempt metadata with focused Rust/API tests.
- C exposes validated routing settings, stable selector interface, and safe explanation output.
- Contract fixtures are copied into dependent prompts by exact commit SHA, not by prose memory.
- B and D start from those exact reviewed heads.
- E starts from PR #5 exact head and owns only dashboard/navigation/settings UI paths.

### Checkpoint 3: vertical slice

- Visible spawn selects the first healthy subscription binding.
- Deterministic quota exhaustion cools that binding and selects the next subscription route.
- Cross-executor route change preserves one logical command and workspace checkpoint.
- All subscriptions exhausted follows metered `auto`, `ask`, and `never` correctly.
- cdesktop Agents view and Settings > Execution Routing show fixture-backed real API data.

### Checkpoint 4: closeout

- Restart and concurrent-reconciler tests prove no duplicate or lost logical command.
- Retirement tests prove pending and later follow-ups cannot restart a terminal session, including when dispatch was delayed by a full global pool.
- Handoff tests prove retries and delayed spawn responses create one successor workspace, session, and logical command for one idempotency key.
- Callback tests prove a routine child status queues without interrupting the manager, while an explicit blocker/decision can interrupt and both receive durable acknowledgment.
- Secret scans cover logs, APIs, CLI, UI, snapshots, exceptions, and transcripts.
- Every branch is clean, pushed, draft, and independently reviewed at exact head.
- Integration order and conflicts are documented.
- Release/publish remains held for explicit approval.

## Merge and stacking plan

This is the intended order after explicit approval. No step is authorized merely by this document.

### SightMesh train

1. PR #17, lease-sync resilience, onto current SightMesh `main`.
2. Lane C, settings and selector, rebased onto the new `main`.
3. Lane D, auto-launch and reconciler, stacked on Lane C and then rebased after Lane C lands.
4. Lane H, docs and release integration, stacked on Lane D or folded into the final release-candidate integration PR if its diff remains isolated.
5. Rebase SightMesh release-candidate PR #16 onto the resulting `main`, compare its tree to both the previously reviewed head and new base, and retain only the runtime-lock/release-polish delta.

### cdesktop train

1. PR #5, frontend/format baseline, after exact-head CI and review.
2. Lane I atomic spawn validation from current main.
3. Lane J cdesktop workspace-start atomicity, if source changes are required beyond SightMesh compensation.
4. A0 backend baseline as its own PR or cherry-picked first commit, after exact clippy/schema checks.
5. A1 normalized outcome and attempt contract, stacked on A0.
6. B auth bindings and metered approval backend, stacked on A1.
7. E Agents/settings dashboard, rebased onto PR #5 plus the final A1/B API contract.
8. PR #4 release distribution, rebased last onto the complete cdesktop tree. Compare candidate tree against the current base tip so it cannot revert later work.

### Cross-repository release gate

1. All cdesktop implementation and UI PRs reviewed and merged only after approval.
2. Rebase and validate cdesktop release-distribution PR #4.
3. Explicit operator approval for `version_type=none` cdesktop prerelease dispatch.
4. Verify release assets, manifest, package digest, and exact cdesktop compatibility behavior.
5. Update SightMesh runtime lock once with the real cdesktop tag, URL, SHA-256, and compatibility floors.
6. Run pinned-artifact CI and the full deterministic hot-swap scenario.
7. Exact-head independent review of both repositories.
8. Explicit operator decision to mark ready, merge, or publish SightMesh.

## Conflict ownership

- `crates/db/src/models/execution_process.rs` and provider launch metadata: A1 only after A0 checkpoint.
- cdesktop executor/container dispatch and normalized outcome mapping: A1 only.
- cdesktop provider/auth secret resolution: B only after A1 interface freezes.
- cdesktop app navigation/settings composition: E only, based on PR #5.
- cdesktop teammate spawn validation and rejected-request atomicity: I only.
- failed workspace-start cleanup and absent-container deletion: J only.
- external launcher identity, durable parent fallback, and decision inbox: K only.
- `src/sightmesh/execution_routing.py`, routing settings, and CLI parser: C only.
- SightMesh spawn/failover/durable reconciler integration: D only after C checkpoint.
- README, architecture, security, compatibility, and release docs: H only after contracts freeze.
- F reviewer is read-only and files findings through messages/review artifacts.

## Token-efficiency policy

- Each implementation successor receives a bounded file map and exact prior commit instead of full program history.
- Do not rerun discovery already captured in the ledger or plan.
- Run only checks that can catch plausible regressions in the owned behavior.
- Use one checkpoint commit per coherent invariant.
- Replace workers before their judgment degrades; do not spend the final context window writing implementation.
- Manager messages contain authority, exact SHA, owner, required action, exclusions, and stop condition only.
