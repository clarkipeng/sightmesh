# Taste

How we build. Every agent reads this before writing code. When a judgment call comes up, this document decides it. Briefs add context, never new taste.

## Architecture

- Prefer one path over branches. If a function grows if/else arms and edge-case handlers, use an invariant that makes the special cases impossible.
- Keep one implementation per capability. If two versions exist, keep the better one and delete the other in the same change.
- Build shared paths when duplication is real or clearly coming. An abstraction with one caller is waste.
- Measure real things: the actual process, stored row, and external result, not a stand-in, cached copy, or dashboard flag.
- Keep data on the thing it describes. A new variant declares its own facts where it is constructed; systems measure what exists.
- Delete finished one-off jobs. A completed migration script left in the tree is a loaded gun.
- Use the smallest architecture that is robust. No framework where a function does the job.
- Build constraints that make bad states impossible first: a unique index, check, transaction, or monotonic counter.
- Let mechanisms that interpret evidence wait until a real incident fixes their exact shape.
- Replacing a path includes deleting the old one in the same change.
- Ship the smallest releasable unit. Harden after activation unless the core is unsafe without it.

## Configuration

- Keep dials next to the rules they tune, in small modules.
- If a value is definitional, it is code, not configuration.
- Avoid configuration bureaucracy and standing style nags. On-demand audits and structural architecture gates are useful; taste lint is not.

## Process

- Fix real problems before building features. Reproduce bugs end to end as a user first.
- Before pushing, run the local checks that the change’s risk warrants. CI confirms; it does not discover.
- Squash-merge, never push main directly, and review pull requests to zero blockers.
- Refactors ship green with tests moved, never weakened.
- Every test says why it exists, especially regression guards.
- Report what actually happened. If a claim turns out wrong, correct the record.

## Orchestration

- Multi-slice work runs through SightMesh: the manager coordinates, workers execute, each worker has one program prefix and disjoint file ownership, and the manager reviews and merges.
- The coordinator writes briefs, gathers evidence, deliberates with the founder, oversees merges, and deploys development changes.

## SightMesh layer

- Prefer invariants over mechanisms: make bad states impossible instead of accumulating special-case handling.
- Run the simulator and an adversarial review before merging kernel or seam changes.
- Use typed outcomes; never infer state by scraping text.
- The executor owns process truth.
