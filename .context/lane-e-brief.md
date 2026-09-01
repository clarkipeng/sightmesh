# Lane E brief: dashboard API integration

You are the single Lane E writer, continuing the parked Lane E dashboard work in a fresh session (the prior E session is retired; never contact it).
Objective: adapt the fixture-backed cdesktop dashboard (Agents navigation/view, Settings > Execution Routing, approval UI) to Lane B's real API contract.

## Base, exact and verified

- Repo: cdesktop. Continue branch `cdt/964b-lane-e-dashboard` at exact head `ecc986b7736c233bf4cd7b25a273eddaf4b38a14` (implementation head `61f5f010` plus one docs handoff commit; read that in-repo handoff doc first).
- Verify HEAD equals `ecc986b7` before writing anything. If it does not, stop and report.
- Draft PR #6 (base `cdt/13da-cdesktop-format`) already tracks this branch; pushing updates it. Do not open a new PR.

## B API contract to integrate, frozen at cdesktop `96960fbe` (verified)

Full detail: `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-b-reconciliation.md`. Key TS types (in `shared/types.ts` at that head):
- `SessionCommandConfig.auth_binding_id?: string` - opaque; keep it OUT of the UI projection like the existing fixture does; never render binding or secret material.
- `MeteredApproval`, `MeteredApprovalPolicy` (auto|ask|never), `MeteredApprovalState` (pending|approved|denied|auto_started|blocked), `MeteredExecution`, `MeteredApprovalResponseRequest { approved, reason? }` - drive the approval UI (pending ask -> approve/deny with optional reason; show auto_started and blocked states).
- `ExecutionProcessOutcome` with `NormalizedExecutionOutcome` and `ExecutionOutcomeClass` (quota_exhausted, auth_expired, auth_invalid, model_unavailable, rate_limited_transient, network_transient, user_stopped, task_failed, unknown) - drive outcome display in the Agents view.
- Backend routes live in `crates/server/src/routes/metered_approvals.rs` at that head; read them for exact paths/shapes.
To see the real types, read `shared/types.ts` at commit `96960fbe` from branch `cdt/b514-lane-b-auth-appr` (fetch it read-only). Do not merge that branch; integrate against its types and keep fixtures consistent with them.

## Ownership, hard boundaries

- Yours alone: dashboard/navigation/settings UI paths, frontend fixtures.
- Forbidden: any file under `crates/`, any `.rs`, any migration, `shared/types.ts` (generated; consume, never edit). If a type or route seems missing or wrong, report to your parent and stop that sub-task; do not fix the backend.

## Proof

- Run the frontend checks the repo defines (typecheck, lint, tests - see package.json scripts / frontend-checks CI job). Report exact results honestly; if a tool is unavailable locally (for example prettier is not installed), say so rather than skipping silently.
- Also investigate and report WHY draft PR #6 shows no CI checks (path filtering vs missing frontend workflow trigger); do not modify workflows.

## Delivery and stop

- Small checkpoint commits, push `cdt/964b-lane-e-dashboard` (updates draft PR #6). Draft only: no merge, ready, publish, workflow dispatch, or secret mutation. No new PRs; explicit `--repo clarkipeng/cdesktop` on any gh call.
- No detached/background/polling processes, no `sleep` monitors. Never message retired or completed sessions.
- When done or blocked: report exact branch, head SHA, PR number, and check results via `sightmesh parent --message "STATUS: ..."`, then stop.
