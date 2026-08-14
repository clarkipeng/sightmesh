# Claude instructions

Use the `orchestrate-visible-agents` skill for every delegated assignment. Do not use native hidden subagents. Use the `reconcile-agent-work` skill before changing ownership, provider, or lifecycle.

Keep orchestration local. Do not add credential extraction, auth-header replay, or rate-limit evasion. Selecting among accounts the operator owns and has logged into normally is supported: observe quota and move to the next account, using each account's own credentials.

Prefer the smallest robust architecture. Replace edge-case branches and hardcoded fixes with invariants that make those cases correct by construction.

Keep skills and agent guidance short and semantic. Use a small example when helpful; add specifics only when correctness or safety depends on them.
