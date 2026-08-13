# Trace efficiency audit

The read-only Catapult fleet audit on 2026-08-12 inspected the latest coding turn for every active session attached to `/Users/clarkpeng/Documents/Code/catapult-games`. It did not steer workers or mutate their repositories.

## Findings

- The seven inspected turns used 51,014 to 152,937 tokens. `sightmesh peek` reports both token usage and context pressure so a manager can checkpoint or replace a worker before quality degrades.
- Completed turns usually produced about 1.0 to 1.1 normalized patches per final semantic entry. Actively streaming turns produced 5.2 to 10.4 patches per final entry because token deltas repeatedly replace one entry. The cdesktop fork's normalized snapshot endpoint applies those patches server-side and returns one compact state document.
- Four implementation workers batched most shell inspections. One older reviewer issued 45 separate shell calls with no batching. New SightMesh spawn prompts and the shared skill now require independent read-only inspections to be batched while dependent writes, approvals, and destructive actions remain sequential.
- No inspected active turn contained a structured question request. A provider can only batch questions it includes in one request because execution pauses on the first request. New worker prompts require all currently known independent questions to be collected first, and the cdesktop form renders and submits the entire request as one interaction.
- UUID-only routing and workspace-scoped interruption imposed unnecessary discovery and collateral-stop costs. Exact agent selectors, compact fleet discovery, and per-session steering remove both costs.

## Deliberate boundaries

- `sightmesh steer` interrupts only the selected session's non-dev-server execution. It refuses self-steering and any target with a pending approval or question.
- `sightmesh message` remains available for a non-interrupting follow-up. Repowire remains the durable ask/reply transport when interruption is not appropriate.
- Independent network or filesystem reads may run together. Commands whose output determines a later target, writes to the same files, Git mutations, approvals, and destructive actions must remain ordered.
- Snapshot coalescing is bounded by time and patch count. It reports whether the execution was complete and how many patches could not be applied instead of pretending a partial snapshot is complete.
