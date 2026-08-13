# Claude instructions

Use the `orchestrate-visible-agents` skill for every delegated assignment. Do not use native hidden subagents. Use the `reconcile-agent-work` skill before changing ownership, provider, or lifecycle.

Keep orchestration local. Do not add credential extraction, auth-header replay, consumer-account rotation, or rate-limit evasion.

Prefer the smallest robust architecture. Replace edge-case branches and hardcoded fixes with invariants that make those cases correct by construction.

Keep skills and agent guidance short and semantic. Use a small example when helpful; add specifics only when correctness or safety depends on them.
