# Agent instructions

Use `$orchestrate-visible-agents` for every delegated assignment. Do not use native hidden subagents. Use `$reconcile-agent-work` before changing ownership, provider, or lifecycle.

Keep orchestration local. Do not add credential extraction, auth-header replay, or rate-limit evasion. Selecting among accounts the operator owns and has logged into normally is supported: observe quota and move to the next account, using each account's own credentials.

Keep the operator model harness-native and minimal. `.context` is workspace-local, cdesktop owns transcripts and visible sessions, Git owns worktrees and source state, and Repowire owns cross-workspace contact. Do not add a global context mirror, transcript copy, custom MCP, or new command when ordinary files, Git, cdesktop, or Repowire already provide the capability.

Prefer the smallest robust architecture. Replace edge-case branches and hardcoded fixes with invariants that make those cases correct by construction.

Keep skills and agent guidance short and semantic. Use a small example when helpful; add specifics only when correctness or safety depends on them.
