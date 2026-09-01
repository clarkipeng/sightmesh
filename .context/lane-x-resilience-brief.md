# Lane X - sightmesh resilience: no silent failures

Base: sightmesh origin/main (post #49).

## Goal
Convert every remaining silent-failure path into a visible or deterministic one. No new daemons; reuse reconciler and existing stores.

## Scope
1. Free-route failure semantics: today a `free` billing-class route owns no account by construction. When a free route fails terminally (model unavailable, provider rejects), the launcher must escalate visibly to the parent/decision inbox instead of leaving the worker blocked without signal. Design the minimal shape: an escalation carrying route id + outcome class; do NOT auto-degrade onto paid accounts unless a routing-policy field explicitly opts in (`fallback_on_free_failure`, default false). Tests from real captured failure output where possible.
2. Fix the flaky test `test_succession.py::test_concurrent_retirement_keeps_the_first_terminal_record` (flakes ~1/5 on clean main). Root-cause it: if it is a genuine race in succession bookkeeping, fix the code; if purely a test race, serialize deterministically. State which in the PR.
3. Sweep for any other write-path that swallows errors into logs-only. Each finding becomes either a visible flag reusing existing surfaces (order-expectation flags, escalations, inbox) or an explicit documented exception. Findings list goes in the PR body even where no change is made.

## Guards
Bloat rules apply. Policy C: full local gates, draft PR, self-ready on green, durable completion signal with exact head and evidence. Report BLOCKED before ending your turn. Stop condition: PR delivered with findings list.
