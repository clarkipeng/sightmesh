# Lane R brief: adversarial framework-consistency sweep (read-only)

You are a READ-ONLY finder. You own no implementation files and change nothing. Your product is a findings report.

## Repos
sightmesh `main` and the cdesktop fork `main` (source at /Users/clarkpeng/Documents/Code/cdesktop, read-only).

## The framework doctrine to audit against
1. No phantom seams: every sightmesh call into cdesktop must target a route the real server serves (check `crates/server/src/routes/` registrations against `src/sightmesh/cdesktop.py`), and test fakes must model the real surface, not an imagined one.
2. Provenance and state are proven cryptographically or from authoritative sources - never by string matching, mirrored lists, or version cosmetics.
3. Events are scripts, models only act on durable wakes: no model-side polling loops, no fire-and-forget signals where loss matters, every signal dedupe-keyed and durable.
4. No harness machinery compensating for model misbehavior: visibility and single bounded nudges only; no retry/renudge loops, no semantic compliance detection.
5. Retirement quarantine: no path may deliver into an explicitly retired/archived session; completed-but-live sessions wake by design.
6. Secrets resolve only immediately before launch; never persisted, logged, serialized, or displayed - including in new surfaces (fleet overview, policy records, escalation rows, outcome projections).
7. Smallest robust architecture: flag dead code, duplicate mechanisms (two stores, two accounting systems, mirrored inventories), and any write-only data.
8. Docs tell the truth about what is shipped versus pending (README, docs/, PR-visible claims).

## Method
For each doctrine item, actively try to find a violation with file:line evidence and a one-line failure scenario. Confirmed > plausible; say which. Do not report style nits.

## Delivery
Write findings to `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-r-findings.md` ranked by severity with file:line and scenario, then append "STATUS: N confirmed, M plausible" to `.context/lane-r-status.md` AND `sightmesh parent --message`. No PRs, no edits, no background processes.
