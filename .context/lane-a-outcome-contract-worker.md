# Lane A worker: cdesktop outcome/attempt contract

You are one visible cdesktop worker owning Lane A of the SightMesh subscription hot-swap program. Full plan (read section 15 Lane A, section 9, section 10, section 14 for exact scope):
`/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`

Do not read the rest of this program's other lane assignments. Do not implement UI, SightMesh Python code, or auth-adapter/approval resolution (Lane B) beyond adding the reference field described below.

## Base and delivery

- Repo: `cdesktop` (Rust workspace).
- Base: exact SHA `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2` (current `origin/main` tip).
- Work in an isolated worktree. Keep the PR draft. Do not merge, mark ready, or publish anything.

## Step 0: reconcile the paused backend-ci-baseline diff first

A paused lane left recoverable **uncommitted** work at `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/backend-ci-baseline/cdesktop` (branch `cp/backend-ci-baseline`, checked out exactly at your same base SHA). It contains an SQLx CLI pin fix and DB clippy fixes across exactly these 4 files, which are also files you will extend:

- `.github/actions/cargo-checks-common-setup/action.yml`
- `crates/db/src/models/execution_process.rs`
- `crates/db/src/models/provider.rs`
- `crates/db/src/provider_catalog.rs`

Read that diff (`git -C /Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/backend-ci-baseline/cdesktop diff`). Do **not** edit that worktree. In your own worktree, reapply the equivalent change as your first commit (cherry-pick, or manually reproduce it against your base — your base SHA matches theirs exactly, so a plain `git diff` from that worktree should apply cleanly with `git apply` or `git cherry-pick` of a manual commit there). Verify the CI/clippy fix works before building outcome/attempt work on top, since your later changes touch the same files.

## Step 1: normalized terminal outcome

Today `ExecutionProcess` (`crates/db/src/models/execution_process.rs:43-48,62-79`) has only a coarse `status`: `Running | Completed | Failed | Killed`. There is no outcome taxonomy. Add a normalized outcome classification with exactly these variants (plan section 9):

`quota_exhausted | auth_expired | auth_invalid | model_unavailable | rate_limited_transient | network_transient | user_stopped | task_failed | unknown`

Plus the safe structured fields from plan section 14's `cdesktop normalized execution outcome` JSON contract: `provider_code`, `retry_after_seconds`, `resets_at`, `binding_scope`, `safe_message`. Add a migration under `crates/db/migrations/` following the existing naming convention (most recent: `20260813000001_create_session_commands.sql`).

`ExecutorError` (`crates/executors/src/executors/mod.rs:93-94`) currently has only one differentiated variant, `AuthRequired`. Extend the executor error mapping (Claude: `crates/executors/src/executors/claude.rs`; Codex: `crates/executors/src/executors/codex.rs`, including `codex.rs:597`'s existing `AuthRequired` raise) so each executor maps its real failure surface to the new outcome enum. Do not invent providers you can't observe — where a real provider signal is missing, map to `unknown` rather than guessing, and cite the exact source field/text you matched on.

Never let raw provider text be the sole durable classifier when a stable provider code exists (plan section 9, last line).

## Step 2: logical command + attempt linkage

`SessionCommand` (`crates/db/src/models/session_command.rs`) is the existing durable "logical command" abstraction: dedupe via `ON CONFLICT(session_id, dedupe_key) ... DO NOTHING` (`:68-100`), claim via `claim_pending`/`ensure_claimed` (`:124,194`), attempt linkage via `execution_process_id` set in `release_execution`/`finish_execution` (`:180,221`).

Extend it (new columns + migration) to carry the fields from plan section 14's `Durable command attempt metadata` contract: `attempt` (increment on each requeue), `route_id`, `auth_binding_id` (opaque string, never a resolved secret — see Step 3), `account_alias`, `executor`, `model`, `billing_class`, `policy_digest`, `predecessor_execution_process_id`. Public projections must omit `auth_binding_id` unless the local caller needs the opaque identifier, and must never include resolved environment/headers (plan section 14, last paragraph).

## Step 3: auth-binding reference, not resolved secrets

Add an `auth_binding_id` field to the launch configuration path (`crates/executors/src/env.rs` `ExecutionEnv`, and/or `SessionCommand.config`) that is just an opaque local identifier. Do **not** change how `crates/db/src/models/provider.rs` resolves API keys today (`resolved_api_key()` at `:539-546`) beyond adding this reference — full secret redaction and binding resolution is Lane B's scope, which starts once your contract lands. Your job here is only to make the *shape* of the contract correct: an opaque reference travels through `SessionCommand`/`ExecutionEnv`; no plan JSON field for secrets exists in your contract.

Note for your PR description: today there is no secret redaction utility anywhere in `crates/executors`, `crates/db`, `crates/services`, or `crates/server`. Flag this explicitly as a known gap for Lane B — do not attempt to fix it yourself, it's out of scope for the contract.

## Step 4: exact-once claim and stale-attempt guards

`session_command.rs` already has ON-CONFLICT dedupe and `claim_pending`/`ensure_claimed`. Extend so that:

- Completion of any attempt closes the logical command and prevents a later stale attempt (e.g. a slow predecessor process) from being treated as authoritative (plan section 10, "Completion of any attempt closes the logical command and prevents later stale attempts from launching").
- A `claimed` command with an uncertain/still-running process is observed on restart, never relaunched blindly (plan section 10 crash recovery rules) — check current restart-recovery behavior at `crates/services/src/services/container.rs:524-530` ("interrupted execution returns to pending") and make sure your attempt/outcome fields don't break that path; add the missing stale-attempt guard if it isn't there today.

## Tests

Follow the existing inline test convention in `session_command.rs` (`#[cfg(test)] mod tests` with an in-memory SQLite `pool()` helper, e.g. `:266-303`, and descriptive test names like `interrupted_execution_returns_to_pending` at `:417`). Add focused tests for:

- Each new outcome variant classifies correctly from its source signal.
- Attempt increments on requeue; dedupe key stays stable across the whole logical command.
- A completed attempt blocks a later stale attempt from claiming/starting.
- `auth_binding_id` round-trips as an opaque string; no secret value is ever stored in `SessionCommand`/`ExecutionEnv` types you touch.

## Proof and delivery

Run `cargo test -p db -p executors -p services` (adjust to whatever crate names your changes touch) and `cargo clippy -p db -p executors` before calling this done. Push your branch and open one draft PR against cdesktop `main` with the exact head SHA, the two-part diff (reconciled clippy/SQLx fix, then the new contract), and a summary of what Lane B/D need from you (the auth-binding shape, the outcome enum, the attempt-metadata contract).

Report back your worktree path, branch name, exact head SHA, and check results before you consider this checkpoint stable.
