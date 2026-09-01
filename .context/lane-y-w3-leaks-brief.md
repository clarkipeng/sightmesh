# Lane Y - cdesktop W3: make the fourth harness truly adapter-only

Base: cdesktop origin/main (post #16, includes opencode executor and session-scoped approvals).

## Goal
Eliminate the four harness-specific leaks Lane W recorded outside adapters, so adding harness N+1 requires exactly one adapter module plus profile registration. Refactor only; no behavior change beyond what each item implies.

## The four recorded leaks, worst first
1. `Provider::prefix_opencode_model_id` (crates/db/src/models/provider.rs:539) encodes opencode model-id conventions in the db layer; called from server/services at provider_injection.rs:37, routes/teammates.rs:247, routes/sessions/mod.rs:337, services/auth_binding.rs:88. Move the convention behind the executor adapter surface; call sites use a shared trait method.
2. `ExecutionEnv.provider_codex: Option<CodexProviderInjection>` (crates/executors/src/env.rs:104,127): harness-specific field on the shared spawn-env type. Generalize so structured injection config crosses the boundary without naming a harness (e.g., typed per-executor payload keyed by executor enum, or a capability the adapter declares).
3. `Provider::build_agent_injection` (provider.rs:1004): hand-written match plus per-harness builders. Replace with a registry the adapters register into, so db code never matches on harness variants.
4. Approval bridge allowlist (container.rs:1252): falls through unknown executors to Noop silently. Invert: ask the adapter whether it brokers approvals; compile-time enforcement preferred (trait method with default = no broker is acceptable if honest).

## Guards
This is a refactor lane: existing tests must pass unchanged except where assertions encode the leaked structure; add one regression test per item proving the leak is gone (e.g., a hypothetical fourth executor resolves injection without touching db-layer harness code - a compile-level proof is better than a runtime one where possible).
Bloat rules apply: net diff should be near zero or negative; delete dead arms rather than keeping compatibility shims.
Policy C: fmt, clippy workspace qa-mode -D warnings, cargo test -p executors -p db -p services, generated-types check, draft PRs, self-ready on green, durable completion signal with exact heads. Report BLOCKED before ending your turn. Stop condition: PR delivered.
