# Agent instructions

Use `$orchestrate-visible-agents` for every delegated assignment. Do not use native hidden subagents. Use `$reconcile-agent-work` before changing ownership, provider, or lifecycle.

Keep orchestration local. Do not add credential extraction, auth-header replay, or rate-limit evasion. Selecting among accounts the operator owns and has logged into normally is supported: observe quota and move to the next account, using each account's own credentials.

Keep the operator model harness-native and minimal. `.context` is workspace-local, cdesktop owns transcripts and visible sessions, Git owns worktrees and source state, and Repowire owns cross-workspace contact. Do not add a global context mirror, transcript copy, custom MCP, or new command when ordinary files, Git, cdesktop, or Repowire already provide the capability.

Prefer the smallest robust architecture. Replace edge-case branches and hardcoded fixes with invariants that make those cases correct by construction.

Keep skills and agent guidance short and semantic. Use a small example when helpful; add specifics only when correctness or safety depends on them.

## Working defaults

- Workers start with ACCEPT_EDITS. Supervision is only for destructive actions (merge, deploy, delete, restart, migrate); never give a worker a permission that forbids its own deliverable (plan-only workers cannot write reports or run `sightmesh complete`).
- Don't ask for permission; proceed. Ask only when the decision is the founder's (prod, spend, scope), and ask a real directional question.
- Follow-ups are dense: what changed, what's decided, what needs a decision.
- Small, specified fixes are done directly by the coordinator; dispatch workers only for long or heavy jobs. Keep at most ~3 concurrent workers while launch admission is unbounded (#88).
- Resumable by default: append partial results to `<output>.partial.md` and run `sightmesh checkpoint` every ~15 minutes; a replacement reads the partial first.
- Models by role: Fable plans and reviews kernel-class changes; sol orchestrates; terra implements and audits; luna does bounded mechanical work. Route by cognitive risk; fail over only on quota/auth/provider errors, never on test or code failures.
- Reviews match blast radius: kernel, schema, seam changes get an independent adversarial review; small fixes get one real-risk probe plus one test that fails without the fix.
- Credentials rotate. Anything that ever appeared in a transcript is exposed and gets rotated; print shapes, never values.
