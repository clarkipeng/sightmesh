# Subscription hot-swap program ledger

Status: program kickoff.
Manager: `@hotswap-manager` (visible SightMesh session, workspace `release-candidate-integration`, executor CLAUDE_CODE, branch `cdt/0c5b-release-candidat`).
Date opened: 2026-08-18.

## Pre-existing draft PRs (unrelated composition work, keep draft, do not touch)

| Repo | PR | Branch | Head SHA | Status |
|---|---|---|---|---|
| sightmesh | #16 | `cdt/0c5b-release-candidat` | `4fec36b0f1e4073a0b9e350ecc060d63c67d7095` | draft, 10/10 checks passed |
| cdesktop | #4 | `cdt/6d94-cdesktop-026-rel` | `398668b54ff5f725575f660cc0bca62a240996af` | draft, release-distribution check passed |
| cdesktop | #5 | `cdt/13da-cdesktop-format` | `41d37b261ada0d03b73e82cfd59d1fa39140a61b` | draft, full frontend CI passed locally |

These belong to the release-candidate-integration program, not to hot-swap. Hot-swap lanes must branch from current `main` heads below, not from these composition branches, unless a lane explicitly depends on one of their contracts.

## Current canonical heads (verified 2026-08-18)

- sightmesh working checkout (`0c5b-release-candidat`): `4fec36b`.
- cdesktop `main` (`/Users/clarkpeng/Documents/Code/cdesktop`): `c3768a84` ("fix: run worktree setup scripts from workspace root (#1)").

## Paused backend-ci-baseline handoff

- Path: `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/backend-ci-baseline/cdesktop`
- Branch: `cp/backend-ci-baseline`, checked out at `62cbae3d` ("chore: bump version to 0.2.6"), confirmed merge-base with itself (i.e. HEAD == stated base `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`).
- Uncommitted, unstaged modifications (4 files, +104/-47): SQLx CLI pin + DB clippy fixes.
  - `.github/actions/cargo-checks-common-setup/action.yml`
  - `crates/db/src/models/execution_process.rs`
  - `crates/db/src/models/provider.rs`
  - `crates/db/src/provider_catalog.rs`
- Not yet reconciled to a visible owner. Do not edit directly (per launch instructions). Action: assign one visible cdesktop worker to pick this up, most naturally folded into the Lane A worker since it touches `execution_process.rs` and `provider.rs`, which Lane A will also extend. Decision pending Lane A grounding results.

## Implementation lanes (see plan `.context/subscription-hotswap-implementation-plan.md` section 15)

| Lane | Scope | Owner | Status |
|---|---|---|---|
| A | cdesktop outcome/attempt contract (normalized outcomes, logical command/attempt linkage, auth-binding ref, exact-once claim) | `@lane-a-outcome-c2` (successor, was `@lane-a-outcome-contract`), workspace `1f2c8bcc-ee2e-42a5-a085-cba3b10a97a2` (unchanged), session `7c136726-fd09-486a-82b4-672a14df5d98`, branch `cdt/1f2c-lane-a-outcome-c`, worktree unchanged, base `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2` | **Rotated 2026-08-18 21:5x PT via `sightmesh failover` (profile `codex-terra-high`, the only operator-approved automatic-failover profile — `claude-default` was rejected) after the prior instance self-checkpointed at 84% context.** Verified independently before rotation: HEAD `5d2f132ff147a08f6879488eab2d6556e5a90dd3` (A0, the Step-0 reconciliation commit only) matches `origin/cdt/1f2c-lane-a-outcome-c` exactly, worktree clean, `stash@{0}` present with an untested draft `crates/executors/src/outcome.rs` (explicitly not to be applied blindly). Full design (file map, proposed outcome-enum/migration/SessionCommand-attempt-column/auth-binding shape, focused test commands, exclusions) is in `.context/lane-a-contract-handoff.md` — treat that as the authoritative brief for this lane, more detailed than the original assignment. Successor confirmed started in the same workspace/worktree; no tool activity observed yet (just spawned). No PR opened yet — still only A0 landed. |
| B | cdesktop auth adapters + metered approval | TBD | blocked on Lane A contract |
| C | SightMesh settings model + route selector + CLI | workspace `57093eb6-d0e2-4199-b070-9036ee0f788c` (now **archived**, reconciled and closed by a prior successor manager instance), branch `cdt/5709-lane-c-settings` | **Delivered and verified. PR #18 open, draft, exact head `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`, GitHub checks 6/6 passed.** I independently verified: diff scope is exactly 3 new/additive files (`src/sightmesh/execution_routing.py` +439, `src/sightmesh/cli.py` +160, `tests/test_execution_routing.py` +496; +1095/-0 total, no edits to `pool/core.py` or `profiles.py`), worktree clean, remote head matches. Spot-checked (not full line read): `execution_routing.py` imports `pool_core` and calls `load_pool()`/`accounts_for()` live inside `select_route()`/`route_warnings()` (no hardcoded/mirrored account list, as required); CLI registers a clean, isolated `routing` subparser group (`show`/`validate`/`set-metered`/`routes list|add|remove|order`/`explain`, matching plan §13 exactly) with no collision with other subcommands. This lane's assigned scope (settings model, selector, CLI, tests) reads as fully delivered — still draft, not merged/ready. |
| D | SightMesh auto-launch/reconciler integration | TBD | blocked on Lane A contract review |
| E | UI/observability | TBD | blocked on settings/API contract stabilization. **Scope now decided by operator, see "Product decision" below — update the Lane E worker prompt with this before spawning.** |
| F | Independent adversarial review | TBD | blocked on first stable checkpoint |
| G (infra, not a feature lane) | Lease-sync fault isolation: one malformed workspace must not abort `sync_active_workspaces` for everyone | `@lane-g-lease-sync-resilience`, workspace `174fcab5-8276-493a-8f87-7e52d11b3d2d`, branch `cdt/174f-lane-g-lease-syn`, worktree `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/174f-lane-g-lease-syn/sightmesh`, base `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`, scoped to `src/sightmesh/leases.py` + `tests/test_leases.py` only | **Implementation complete.** HEAD `e4f90f8` ("fix: isolate per-workspace failures in sync_active_workspaces"). PR **#17** open, draft, exact head matches. I independently verified: diff scope is exactly `src/sightmesh/leases.py` (+15/-3) and `tests/test_leases.py` (+39) — no scope creep; reviewed the `leases.py` diff directly and confirmed the fix logic is correct (previously `raise`d the first `LeaseError` when no `on_error` callback was given, aborting the whole batch; now logs a warning via a new module `LOGGER` and continues the loop, still surfacing via `on_error` when one is provided). Worker reported `pytest tests/test_leases.py -q`: 16 passed; I was not able to independently re-execute this (project declares no `pytest`/test deps in `pyproject.toml`/`uv.lock`, and building an ad hoc venv was interrupted/deprioritized to prioritize this handoff) — **this is the one unverified claim, otherwise the PR is confirmed sound by direct diff review.** GitHub checks were at 3/6 passed, 0 failed, still running as of last check — **successor should watch this to completion before treating Lane G as fully done.** Still draft; not merged/ready. |

## Grounding notes (provenance)

Lane A and Lane C worker prompts were grounded using two internal Explore-subagent lookups (file:line references into `cdesktop`/`sightmesh` source) launched before the operator's standing instruction to avoid hidden/native subagents for this program. That grounding is **unverified** until a visible worker or my own direct read confirms it — treat file:line citations in the two worker prompts as leads, not settled fact, until each worker's own inspection confirms or corrects them. No further hidden subagents will be used for this program; all remaining grounding/investigation happens via direct read-only inspection or visible workers.

## Fleet infrastructure incident (resolved, follow-up fix in flight)

`sightmesh spawn` was fleet-wide broken (affected me and peer `el-paso-codex`) because a zombie workspace (`534ff918-f265-4ef3-9456-565aaa77ec41`, "env-plan-w0", zero sessions, `use_worktree: true`, null `container_ref`) caused `leases.sync_active_workspaces` to hard-fail for every spawn call (`leases.py:423-446`, called from `cli.py:684`). Root-caused via direct source read; did not force-bypass the CLI's own archive safety gate. Workspace 534ff918 was independently read-only reconciled (zero sessions, no lease, clean worktree) and reversibly archived; branch/worktree remain recoverable. I re-verified its raw record directly: `archived: true`, `container_ref` populated. Note: the reconciliation evidence referenced branch `origin/cdt/c8c8-env-plan-manager`, but 534ff918's own `branch` field reads `cdt/534f-env-plan-w0` (a different workspace, `c8c85b44-...`, holds the `cdt/c8c8-env-plan-manager` branch) — flagging this naming mismatch for the record; it doesn't change the operational fact that 534ff918 is now safely archived.

Spawns for Lane A/C succeeded on retry with no further blocker. **Follow-up:** Lane G (below) now owns a permanent fix so a single malformed workspace can never again abort fleet-wide spawn reconciliation — this was a systemic single-point-of-failure, not just a one-off bad record.

## Product decision (operator, 2026-08-18 21:41 PT) — Lane E UI scope

The primary dashboard remains the existing cdesktop website (do not build a competing standalone dashboard). Add two things to it:

- An **Agents** destination for fleet/session/attempt visibility (the plan's section 13 "Fleet and session visibility" — active route, swapping, approval-required, blocked states, reset/retry timing).
- **Settings > Execution Routing**: pool health, route/model order, cooldowns, approvals, and metered fallback `auto`/`ask`/`never` (plan section 13 "Built-in settings").

The standalone `sightmesh pool serve` page (`src/sightmesh/pool/server.py`) becomes **compatibility/recovery only** — do not expand it into the primary UI surface. This decision must be folded into the Lane E worker prompt (not yet written) before Lane E is spawned.

## Handoff checkpoint — program manager succession (2026-08-18 21:43 PT)

Handing off due to context pressure. This section is the compact state a successor needs; the tables above have the full per-lane detail.

**Verified this session (by direct read/diff, not trusted self-report):**
- PR #17 (Lane G): draft, head `e4f90f8`, scope-correct, logic-correct by direct review, CI in progress (3/6 passed at last check, 0 failed). Not independently re-run locally (see Lane G row above for why).
- Lane A: HEAD `5d2f132f`, Step-0 reconciliation commit confirmed clean and correctly isolated. Contract work (outcome enum, attempt linkage, auth-binding ref, exact-once guards) still in progress, no PR yet.
- Lane C: zero commits, dirty tree (`cli.py` modified, `execution_routing.py` new, untracked). No PR yet.
- Workspace `534ff918` (the earlier fleet-wide spawn blocker): independently re-confirmed `archived: true`, `container_ref` populated. Safe.

**Open risks for the successor to own:**
1. **Lane A (84% context) and Lane C (80% context, zero commits) are both close to their own context limits.** I sent both a non-steering reminder to checkpoint/commit now; the successor should check whether that happened and be ready to hand each of them off (same reconcile pattern) if they run out before finishing.
2. **PR #17 CI was not yet complete** (3/6 checks) — confirm it goes green before treating Lane G as closed; if it fails, it's a fast, narrow fix (single file), reassignable to a new small worker.
3. **Lane E scope changed** (see product decision above) — do not spawn Lane E with the old undifferentiated "UI and observability" framing; write its prompt around the Agents destination + Settings > Execution Routing split, with the pool page staying compatibility-only.
4. Lanes B and D are still correctly blocked behind Lane A's contract stabilizing (per plan section 15 dependency ordering) — do not start them early even though Lane A already has one commit; the *contract* (outcome enum, attempt/auth-binding shape) isn't reviewable yet.
5. Minor record-keeping mismatch already resolved and low-risk: workspace `534ff918`'s own `branch` field is `cdt/534f-env-plan-w0`, not `cdt/c8c8-env-plan-manager` as referenced in one operator message — doesn't affect the archived/safe outcome, just flagging for accuracy.

**Next actions, in order:**
1. Re-peek Lane A and Lane C; if either is at/near its context ceiling or unresponsive, run the reconcile-agent-work pattern on it (checkpoint, hand off to a successor worker on the same branch/worktree) rather than losing the work.
2. Watch PR #17 to green; confirm before considering Lane G done.
3. When Lane A opens its cdesktop PR with the outcome/attempt/auth-binding contract, review it exact-head (scope, diff, tests) before unblocking Lane B/D.
4. When Lane C opens its sightmesh PR, review it exact-head (scope, diff, tests, and that the `routing` CLI group doesn't collide with anything else touching `cli.py`).
5. Write and spawn Lane E once settings/API contracts are stable, using the product decision above (Agents destination + Settings > Execution Routing; pool page compatibility-only).
6. Spawn Lane F (independent adversarial review) at the first stable cdesktop/SightMesh checkpoint.
7. Keep every PR draft. No merge, ready, publish, dispatch, secret change, or unpublished cdesktop runtime-lock update without explicit operator approval — this applies to PR #17 too, despite it being complete.
8. Report to the operator (`sightmesh parent --message`) at the next genuine decision point or completion, not for routine progress.

## Decisions / operator inputs still needed

- None yet — all current plan decisions (subscription-first, auto/ask/never, exact-once, Lane E UI placement, etc.) are already settled in the plan of record or this checkpoint.
