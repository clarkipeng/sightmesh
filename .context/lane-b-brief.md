# Lane B brief: cdesktop auth bindings and metered approval backend

You are the single Lane B writer.
Objective: implement auth-binding resolution, secret redaction, normalized live adapter outcomes, and durable metered `auto`/`ask`/`never` approval with resume, in the cdesktop backend.

## Base, exact and verified

- Repo: cdesktop. Branch off `cdt/1879-lane-a-contract` at exact head `c2a9c2eaacfdd4b2dea066c95793faf755b834be` (Lane A1, stacked on A0 `5d2f132f`).
- Verify HEAD equals that SHA before writing anything. If it does not, stop and report.

## A1 consumer contract you build on (verified at that SHA, do not redesign it)

Full detail: `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-a1-reconciliation.md`.
Key facts: `SessionCommandConfig { executor_config, selected_provider_id: Option<Uuid>, auth_binding_id: Option<Uuid> }` exists.
`crates/server/src/routes/sessions/mod.rs:335` currently fills `auth_binding_id` from `payload.selected_provider_id`; `crates/services/src/services/container.rs:1500` threads it; `container.rs:417` still ignores it.
Resolving the binding to a real provider credential at launch time is YOUR scope.
`ExecutionProcess::complete_running_attempt` gives exact-once attempt completion; use it, do not fork a parallel mechanism.

## Ownership

- Yours alone: cdesktop provider/auth secret resolution, redaction, adapter outcome normalization, metered approval state machine and resume.
- Not yours: app navigation/settings UI (Lane E), teammate spawn validation (Lane I), workspace-start cleanup (Lane J), `crates/db` contract shape beyond additive migrations your feature needs.

## Security constraints, hard

- Secrets resolve only immediately before launch; never persisted, logged, traced, serialized into APIs or snapshots. Redact everywhere.
- No credential extraction, auth-header replay, or rate-limit evasion. Selecting among accounts the operator owns and has logged into normally is supported: observe quota, move to the next account, each account uses its own credentials.

## Proof

- Focused cargo tests: approval `auto`/`ask`/`never` transitions, durable resume across restart, redaction of secret material, normalized outcome mapping. Report exact pass/fail counts.
- `cargo fmt` clean; run `generate-types:check` if you touch exported types.

## Delivery and stop

- Small checkpoint commits, clean pushed branch, DRAFT PR against base `cdt/1879-lane-a-contract` on clarkipeng/cdesktop. Draft only: no merge, ready, publish, workflow dispatch, or secret mutation.
- No detached/background/polling processes, no `sleep` monitors. Never message retired or completed sessions.
- When done or blocked: report exact branch, head SHA, PR number, and test counts to your parent with `sightmesh parent --message "STATUS: ..."`, then stop.
