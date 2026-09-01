# Lane D brief: SightMesh auto-launch and durable reconciler

You are the single Lane D writer.
Objective: SightMesh autolaunch/reconciler integration - spawn/teammate routing through the routing selector, quota cooldown and durable requeue, restart recovery, and cross-executor successor linkage that preserves one logical command.

## Base, exact and verified

- Repo: sightmesh. Branch off `cdt/1ebb-lane-c2-routing` at exact head `be40617b0d232cfa02d11a59b3192b00a1591f11` (Lane C2, stacked on Lane C `cdt/5709-lane-c-settings`).
- Verify HEAD equals that SHA before writing anything. If it does not, stop and report.

## Frozen upstream contracts (verified; consume, do not redesign)

- Lane C/C2 routing selector and settings on your base branch: use the selector interface as-is; `routing validate` semantics were hardened by C2 and are authoritative.
- Lane A1 cdesktop consumer contract at `c2a9c2ea`, detail in `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-a1-reconciliation.md`. Key facts: durable `SessionCommand` with `dedupe_key` idempotent enqueue, `intent` continue|replace, `state` pending|claimed|done|failed|cancelled, `attempt_number`, and `SessionCommandConfig { executor_config, selected_provider_id?, auth_binding_id? }` (both opaque UUIDs). `ExecutionProcess::complete_running_attempt` is exact-once. Treat this as a frozen fixture; do not invent additional fields.
- Lane K escalation contract at `fa2defe1` (draft PR #22, base main), detail in `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-k-reconciliation.md`: `src/sightmesh/escalation.py` durable `EscalationStore` + `escalate()` that never delivers into archived/retired sessions. If you need it in code, merge `origin/cdt/fb85-lane-k-parent-es` into your branch as one explicit merge commit and record the merge SHA in your report; do NOT rewrite `escalation.py`.

## Required invariant, proven defect

Ownership transfer must QUARANTINE an explicitly retired or superseded session: retirement atomically records the terminal ownership state, cancels pending commands, and rejects later message/steer/prompt delivery before a successor launches. Process completion or failure alone is not retirement: an active manager whose turn completed must remain resumable for callbacks and recovery. Prove both sides so the fix does not break normal manager wake-up.

## Ownership

- Yours alone: SightMesh spawn/failover durable reconciler integration, cooldown/requeue, restart recovery, cross-executor successor linkage, superseded-session quarantine.
- Not yours: routing selector/settings/CLI internals (Lane C/C2), `escalation.py` internals (Lane K), docs (Lane H), any cdesktop Rust code (Lanes A/B/I/J).

## Security constraints, hard

- No credential extraction, auth-header replay, or rate-limit evasion. Quota-driven selection among operator-owned, normally logged-in accounts is supported; each account uses its own credentials. Secrets resolve only immediately before launch and never persist in logs, state, or transcripts.

## Proof

- Focused pytest: deterministic quota exhaustion cools a binding and selects the next route; durable requeue survives restart; exactly one logical command survives a cross-executor handoff; an explicitly retired session cannot resume while a non-retired manager can resume after an ordinary completed turn. Run with `env -u CDESKTOP_SESSION_ID uv run pytest -q` (a known leak of CDESKTOP_SESSION_ID from the surrounding session breaks 4 pre-existing spawn tests otherwise). Report exact pass/fail counts.

## Delivery and stop

- Small checkpoint commits, clean pushed branch, DRAFT PR against `cdt/1ebb-lane-c2-routing` on clarkipeng/sightmesh. Draft only: no merge, ready, publish, workflow dispatch, or secret mutation.
- No detached/background/polling processes, no `sleep` monitors. Never message retired or completed sessions.
- When done or blocked: report exact branch, head SHA, merge SHAs if any, PR number, and test counts to your parent with `sightmesh parent --message "STATUS: ..."`, then stop.
