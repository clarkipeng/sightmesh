# Lane F adversarial review: subscription hot-swap

Date: 2026-08-18 America/Los_Angeles

Scope: read-only review of stable plans and stable heads. No source paths owned. No implementation files edited.

## Evidence inspected

- Plans:
  - `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-tonight-plan.md`
  - `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`
- SightMesh base: `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`
- Lane G draft PR #17 head: `e4f90f8c4745db911105b3b318f0a94d3aea16d0`
  - Diff: `src/sightmesh/leases.py`, `tests/test_leases.py`
  - Claimed focused check: `pytest tests/test_leases.py -q` = 16 passed
  - Refreshed with `gh-axi pr checks 17`: 4 passed, 0 failed, 2 pending
- Lane C checkpoint: `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`
  - Diff: `src/sightmesh/execution_routing.py`, `src/sightmesh/cli.py`, `tests/test_execution_routing.py`
  - Handoff evidence: 19 new routing tests; focused suite 51/51 passed; full suite 198 passed / 4 pre-existing failures
- cdesktop base for A0: `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`
- cdesktop A0 baseline: `5d2f132ff147a08f6879488eab2d6556e5a90dd3`
  - Diff: `.github/actions/cargo-checks-common-setup/action.yml`, `crates/db/src/models/execution_process.rs`, `crates/db/src/models/provider.rs`, `crates/db/src/provider_catalog.rs`
- cdesktop frontend baseline draft PR #5 head: `41d37b261ada0d03b73e82cfd59d1fa39140a61b`
  - Supplied stable risk: remote checks 3 passed / 4 failed. Local `gh-axi` in this workspace is scoped to SightMesh and could not refresh PR #5.

## Proven defects

### F-1: `exposeAccountAlias=false` still leaks account ids in selection traces

- Severity: Medium
- Owning lane: Lane C
- Evidence:
  - `src/sightmesh/execution_routing.py` at `e3cab92c`: `SelectedTarget.account_alias` honors `expose_account_alias` at lines 340-351.
  - The same selector always writes raw account ids into `trace` at lines 414, 417, and 435.
  - Tests cover the target shape and `auth_binding_id` (`tests/test_execution_routing.py` lines 189-223), but do not set `expose_account_alias=False` or assert trace redaction.
- Reproduction:
  - Build settings with `expose_account_alias=False`, one eligible account id such as `private-account-id`, and call `select_route(settings)`.
  - Expected: no operator-hidden alias/account id in public explanation trace.
  - Actual by code inspection: `result.target.account_alias is None`, but `result.trace` contains `private-account-id`.
- Release impact:
  - Violates the plan's safe visibility boundary when account aliases are disabled.
  - Does not expose credential bytes, but it can expose a stable local account identifier in CLI/UI traces.

### F-2: Selector resolves secret-bearing launch material during policy selection, including metered `ask`

- Severity: High for `ask` approval boundary; Medium for general selection boundary
- Owning lane: Lane C for selector behavior, Lane B for final auth-binding contract
- Evidence:
  - `src/sightmesh/execution_routing.py` at `e3cab92c` calls `_account_eligibility()` for every candidate before returning `approval_needed` (lines 410-431).
  - `_account_eligibility()` calls `pool_core.env_for(account)` at lines 319-337.
  - `src/sightmesh/pool/core.py` at `e3cab92c` resolves secret-bearing launch material in `env_for()`: Claude token read and returned at lines 387-395; Codex API key read and returned at lines 396-406.
  - The implementation plan says route selection should pass opaque auth bindings and cdesktop should resolve launch material immediately before executor launch; metered `ask` must not resolve or expose the secret while approval is pending.
- Reproduction:
  - Configure exhausted subscription route plus eligible metered API route and `metered_fallback="ask"`.
  - Monkeypatch `pool_core.read_token` to record calls, then call `select_route(settings)`.
  - Expected: returns `approval_needed` without reading/resolving the metered API key.
  - Actual by code inspection: `_account_eligibility()` reaches `env_for()` before the `ask` branch and reads the route account's token if present.
- Release impact:
  - Approval-gated metered fallback can touch the metered credential before approval.
  - This is not a proven persisted leak in the inspected diff, but it breaks the planned ownership boundary and narrows the secret-safety margin.

### F-3: `routing validate` does not actually validate eligibility promised by CLI help

- Severity: Medium
- Owning lane: Lane C
- Evidence:
  - CLI help says `routing validate` will "report routes with no eligible account" (`src/sightmesh/cli.py` at `e3cab92c`, lines 2731-2734).
  - `cmd_routing(validate)` always emits `{"valid": True, ...}` (`src/sightmesh/cli.py` lines 1757-1763).
  - `route_warnings()` only checks whether subscription pools have any account and whether fixed metered accounts exist (`src/sightmesh/execution_routing.py` lines 271-284). It does not apply `_account_eligibility()` filters for disabled, cooling, missing credential, zero quota, or subscription route with only `kind="apikey"` accounts.
  - Tests cover selection skipping disabled/cooling/missing/zero-quota accounts (`tests/test_execution_routing.py` lines 101-136), but no test covers `routing validate` against those same states.
- Reproduction:
  - Put only disabled or credential-less accounts in a configured subscription pool.
  - Run `sightmesh routing validate --json`.
  - Expected: warning that the route has no eligible account, or `valid=false` if release policy treats it as invalid.
  - Actual by code inspection: no warning when the pool is non-empty; `valid` is always true.
- Release impact:
  - Operators can receive a green validation result for a configuration that `select_route()` will block.

## Insufficient proof at stable heads

### F-4: Exact-once hot-swap command ownership is not yet proven end to end

- Severity: Release blocker until later-lane proof exists
- Owning lane: Lane A1 for cdesktop contract, Lane D for SightMesh integration
- Evidence:
  - cdesktop A0 `5d2f132f` has existing native `session_commands` dedupe and claim machinery: enqueue dedupe at `crates/db/src/models/session_command.rs` lines 68-100, claim transaction at lines 137-181, requeue at lines 207-220, finish at lines 268-282.
  - Existing tests cover ordered claims, config boundary, interrupted requeue, dedupe-key preservation, and process-scoped duplicate-safe requeue at lines 404-557.
  - `git grep` at cdesktop A0 found no `logical_command`, `auth_binding`, `meteredFallback`, or `approval_needed` fields in `crates`/`packages`.
  - SightMesh Lane C returns a selected target but has no command claim or dispatch fence (`src/sightmesh/execution_routing.py` lines 354-439).
- Missing proof:
  - Two concurrent supervisors selecting and dispatching the same logical hot-swap command produce one active attempt.
  - Requeue after quota/auth failure preserves one logical command id across executor/model/account change.
  - A stale predecessor completion cannot close or mutate the successor logical command.

### F-5: Restart/reconciliation behavior is not proven for hot-swap states

- Severity: Release blocker until later-lane proof exists
- Owning lane: Lane A1 for durable attempt metadata, Lane D for reconciler recovery
- Evidence:
  - The stable SightMesh Lane G change isolates per-workspace lease sync failures and continues after `LeaseError` (`src/sightmesh/leases.py` at `e4f90f8`, lines 453-476).
  - Lane G does not touch command attempts, routing state, approvals, or hot-swap recovery.
  - Lane C selector is stateless and reloads pool/settings each call, but it does not persist selecting/claimed/approval/retry-wait state.
- Missing proof:
  - SightMesh restart during selection.
  - cdesktop restart after claim but before process start.
  - Machine restart during metered approval.
  - Stale predecessor completion after successor starts.

### F-6: Metered `auto`/`ask`/`never` is only proven at selector level

- Severity: High release risk until integrated backend proof exists
- Owning lane: Lane B for cdesktop approval/resume, Lane D for SightMesh dispatch/requeue
- Evidence:
  - Lane C tests cover selector outputs for `auto`, `ask`, `never`, and policy change from `never` to `auto` (`tests/test_execution_routing.py` lines 239-331).
  - The inspected stable heads contain no cdesktop metered-fallback approval payload/resume contract (`git grep` at cdesktop A0 found no hot-swap policy terms).
- Missing proof:
  - `ask` creates one durable native approval and resumes exactly once after approval.
  - Denial leaves the same command blocked with checkpoint intact.
  - `never` does not resolve API credentials and does not start metered work in the integrated dispatcher.
  - `auto` emits durable notification and starts exactly one metered attempt after subscription routes exhaust.

### F-7: Secret-surface proof is incomplete

- Severity: High release risk until scans and UI/API fixtures exist
- Owning lane: Lane B for secret resolution/redaction, Lane E for UI/API projection, Lane F rereview at exact heads
- Evidence:
  - Lane C settings persistence test checks routing settings for common secret substrings and `0600` mode (`tests/test_execution_routing.py` lines 417-436).
  - Lane C selector returns only `auth_binding_id` and `account_alias` in target (`src/sightmesh/execution_routing.py` lines 290-316, 340-351).
  - cdesktop A0 provider model intentionally stores API keys in the provider DB and resolves them into executor env/config (`crates/db/src/models/provider.rs` lines 93-104, 557-575, 610-616, 676-690, 749-780, 848-862, 902-918, 961-990).
- Missing proof:
  - No stable Lane B redaction diff was available.
  - No stable UI/API projection diff proves omission of `auth_binding_id`, credential paths, header values, token fingerprints, or env values.
  - No scan evidence for logs, exceptions, snapshots, transcripts, API responses, and CLI traces.

### F-8: cdesktop frontend baseline PR #5 is a release risk outside implementation scope

- Severity: Release gate risk
- Owning lane: Lane E / frontend baseline owner
- Evidence:
  - Supplied stable evidence says draft PR #5 head `41d37b261ada0d03b73e82cfd59d1fa39140a61b` has 3 passed / 4 failed remote checks.
  - Local cdesktop repository contains that commit, but local `gh-axi pr checks 5` was not usable from this workspace.
- Required proof:
  - Exact-head CI for PR #5 must be green or the failures must be explicitly waived before using it as a dashboard baseline.

## Non-issues from inspected evidence

- Lane G's lease-sync change does not create a duplicate-lease defect in the inspected code. It only changes batch behavior so one `LeaseError` logs or calls `on_error` and later workspaces continue (`src/sightmesh/leases.py` at `e4f90f8`, lines 453-476). Direct single-workspace `_sync_active_workspace()` still raises `LeaseError` on malformed records (lines 410-450).
- Lane C correctly excludes `kind="apikey"` accounts from subscription routes before candidate eligibility (`src/sightmesh/execution_routing.py` at `e3cab92c`, lines 393-401), so a subscription route does not silently select a metered API key from the same provider pool.
- Lane C `meteredFallback=never` avoids fixed metered account lookup before returning blocked (`src/sightmesh/execution_routing.py` lines 386-391; test at `tests/test_execution_routing.py` lines 283-311). This is selector-level evidence only; integrated dispatch remains unproven under F-6.
- cdesktop A0 is narrow and mostly CI/clippy repair. Its SQLx CLI pin is explicit (`.github/actions/cargo-checks-common-setup/action.yml` at `5d2f132f`, lines 23-26 and 62-68), and the execution-process change boxes `ExecutorAction` while preserving JSON shape with a focused unit test (`crates/db/src/models/execution_process.rs` lines 39-68 and 136-141). It should not be treated as hot-swap implementation proof.

## Recommended release gates before ready/merge

1. Lane C should fix F-1 and F-3, and either stop resolving secret-bearing env in selection or explicitly split non-secret credential-presence checks from launch-material resolution for F-2.
2. Lane A1 must produce exact-head tests for logical command id, attempt number, stale attempt completion, concurrent claim, and restart after claim.
3. Lane B must produce exact-head tests for auth-binding resolution only at launch, metered `ask` durable approval/resume exactly once, denial/blocking, and redaction.
4. Lane D must produce exact-head tests for two SightMesh reconcilers, restart during selection, cooldown/requeue, policy-change wake, and cross-executor successor linkage.
5. Lane E/PR #5 must not be used as release baseline until its failed checks are resolved or explicitly documented as non-release-blocking.
