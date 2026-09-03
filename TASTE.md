# Taste

How we build. Every agent reads this before writing code.
When a judgment call comes up, this doc decides it. Briefs add context, never new taste.
The Architecture, Configuration, Process, and Orchestration sections are shared word for word with the other repos; the SightMesh layer is this repo's own.

## Architecture

- One path, not branches. If a function is growing if/else arms and edge-case handlers, the fix is one path with an invariant that makes the special cases impossible.
- One implementation per capability. If two versions of the same thing exist, keep the better one and delete the other in the same change.
- Build shared paths when duplication is real or clearly coming. Two call sites that repeat logic get a shared path. A "reusable" abstraction with one caller is waste.
- Measure real things. Read the actual process, the actual stored row, the actual external result. Not a stand-in, a cached copy, or a dashboard flag. To prove something dead, check the databases and dashboards too; grep alone proves nothing.
- Data lives on the thing it describes. A new variant declares its own facts where it's constructed. Systems measure what exists instead of reading a central switch.
- Delete finished one-off jobs. A completed migration script left in the tree is a loaded gun.
- Smallest architecture that's robust. No framework where a function does the job.
- Constraints that make bad states impossible (a unique index, a CHECK, one transaction, a counter that only goes up) get built first. They're the floor, not speculation.
- Mechanisms that interpret evidence (classifiers, heuristics, taxonomies) wait until a real incident fixes their exact shape.
- Replacing a path includes deleting the old one in the same change, or the change isn't done.
- Ship the smallest releasable unit. Hardening comes after activation unless the core is unsafe without it.

## Configuration

- Dials live next to the rules they tune, in small modules.
- If a value is definitional (0, 1, "always"), it's code, not config.
- No config bureaucracy and no standing style nags. On-demand audit scripts are good. Structural gates that enforce real architecture are good. Lint tripwires about taste are not.

## Process

- Small, specified fixes get done directly. A worker costs 30-45 minutes for any size change; only dispatch work that is genuinely long or heavy.
- Don't ask for permission. Default is to proceed; ask only when a decision is genuinely the founder's (prod, spend, scope), and then ask a real directional question, not "should I continue?".
- Follow-ups are dense: what changed, what's decided, what needs a decision. No narration, no re-asking what's already answered.
- Reviews match blast radius: kernel, schema, and seam changes get an independent adversarial review; small fixes get one probe of the real risk plus one test that fails without the fix.
- Batch the work: read everything needed once, then write code, tests, and run the suite in one go.
- Models by role: Fable plans and reviews kernel-class changes; sol orchestrates; terra implements and audits; luna does bounded, mechanical work. GPT (codex) and Claude accounts both exist - route by the task's cognitive risk, fail over on quota or auth errors only, never on test failures.
- Credentials rotate. Anything that landed in a transcript is considered exposed and gets rotated; secrets never print, only their shape.
- Fix real problems before building features. Reproduce bugs end-to-end as a user first.
- Before pushing, run the local checks the change's risk actually warrants, not every check on everything. CI confirms; it never discovers.
- Squash-merge, never push main directly, review PRs to zero blockers.
- Refactors ship green with tests moved, never weakened.
- Every test says why it exists, especially regression guards.
- Report what actually happened. If a claim turns out wrong, correct the record.

## Orchestration

- Multi-slice work runs through SightMesh: sol manages, terra/luna execute, one program prefix per worker, disjoint file ownership, the manager reviews and merges. The coordinator writes briefs, gathers evidence, deliberates with the founder, oversees merges, and deploys dev.

## SightMesh layer

- The task is the durable thing, never the session. One task, one identity, one owner; a retry repeats the request, never the effect.
- Workers run unattended in their own worktree. Nothing in the mesh waits on a human approval; supervised policies are opt-in for destructive work.
- Typed outcomes only. Never infer state by scraping text or exit codes.
- The executor owns process truth; the kernel owns task truth. Neither guesses the other's.
- Kernel and seam changes merge only after the simulator is green and an independent adversarial review is clean.
