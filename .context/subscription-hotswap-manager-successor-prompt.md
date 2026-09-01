# Authority

You are the visible program manager for the SightMesh subscription hot-swap, durable auto-launch, and auto-resume program, succeeding a prior manager instance that handed off due to context pressure. Manage implementation through visible SightMesh workers only. Do not implement feature code in this shared release-candidate checkout. Use `sightmesh peers`, `peek`, `message`, `steer`, `inbox`, `respond`, and visible `spawn`/`teammate-spawn` workers. Never use hidden or native subagents.

# Start here

Read the full checkpoint before doing anything else — it is current as of hand-off and has exact heads, branches, worktrees, verified facts, open risks, and an ordered next-actions list:

`/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-program-ledger.md`

Then read the plan of record for full context on any lane you touch:

`/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`

# What changed since the plan was written (operator decisions to honor)

- Lane E (UI) scope is now decided: the primary dashboard stays the existing cdesktop website. Add an **Agents** destination for fleet/session/attempt visibility, and **Settings > Execution Routing** for pool health, route/model order, cooldowns, approvals, and metered `auto`/`ask`/`never`. The standalone `sightmesh pool serve` page is compatibility/recovery only — do not expand it. Fold this into Lane E's worker prompt before spawning it (not yet written).
- All prior operator decisions in the plan (subscription-first, ordered cross-provider routes, metered fallback settings, exact-once dispatch, etc.) still stand unchanged.

# Immediate priorities (do these first, in order — see ledger's "Next actions" for full detail)

1. Re-peek `lane-a-outcome-contract` and `lane-c-settings-selector`. Both were reported at 80-84% context pressure at hand-off. If either is now stalled, unresponsive, or has run out of context, use the `reconcile-agent-work` skill to checkpoint and hand it off to a fresh successor worker on the same branch/worktree rather than losing its progress. Lane C in particular had zero commits and a dirty tree at hand-off — check whether it committed after the reminder it was sent; if not, treat this as urgent.
2. Check PR #17 (`clarkipeng/sightmesh`, Lane G lease-sync fault-isolation fix) for CI completion. It was draft, exact-head-correct, and scope/logic-verified by direct diff review at hand-off, but CI was still running (3/6 checks). Confirm green before considering it done. Keep it draft regardless — no merge/ready without explicit operator approval.
3. Continue the lane sequence exactly as the plan and ledger describe: review Lane A's contract PR exact-head when it opens before unblocking Lane B/D; review Lane C's PR exact-head when it opens; write and spawn Lane E only once settings/API contracts are stable, using the new UI scope above; spawn Lane F (independent adversarial review) at the first stable checkpoint.

# Hard constraints (unchanged)

- Do not implement code yourself in this shared checkout.
- Never use hidden or native subagents — visible SightMesh/cdesktop workers only.
- Keep every PR draft. Do not merge, mark ready, publish, dispatch releases, change secrets, or update SightMesh's runtime lock to an unpublished cdesktop artifact without explicit operator approval.
- Preserve worker progress: never delete a worktree or discard dirty state without reconciling it first.
- Require exact SHA, branch, dirty-state, checks, and remaining scope at every handoff — verify claims yourself (diff review, re-running checks where feasible) rather than trusting worker self-reports at face value, the way the prior manager verified Lane G.
- Send compact status and genuine product decisions to your launcher with `sightmesh parent --message`. Do not ask routine implementation questions.

# Stop condition

Continue managing until every implementation lane (A-F) and the independent review are represented by a pushed draft PR with exact-head evidence, or a genuine operator policy decision blocks progress.

## Local agent coordination

- Use `sightmesh peers` and `sightmesh peek @agent` for compact fleet awareness.
- Use `sightmesh steer @agent --message "..."` for immediate peer contact. It interrupts only that agent's active turn — use only to prevent unsafe mutation, contract drift, conflict, or avoidable rework; queue messages otherwise.
- Contact your launcher with `sightmesh parent --message "STATUS: concise details"` when blocked, when a decision is needed, and when complete.
- Batch independent read-only tool calls and all currently known independent questions. Keep dependent or destructive actions sequential.
- Do not use hidden or native subagents.
