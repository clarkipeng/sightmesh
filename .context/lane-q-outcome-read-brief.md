# Lane Q brief: normalized-outcome read surface + honest dashboard data (sightmesh issue #29, cdesktop half)

Single writer. Read clarkipeng/sightmesh#29 first.

## Base
cdesktop fork `main` (must contain fc1ea4c6). Verify before writing. Repo for PRs: clarkipeng/cdesktop, explicit --repo.

## Problem
Lane B persists normalized execution outcomes but nothing can read them: no route, so the table is write-only and the dashboard's ExecutionRoutingSummary renders labeled fixtures.

## Scope
- Add a read route for normalized outcomes scoped by session (e.g. GET /sessions/{id}/outcomes or on execution-processes; follow the existing route idioms in `crates/server/src/routes/`), returning the existing `ExecutionProcessOutcome` shape. No new shapes; regenerate exported types via the existing generator.
- Replace fixture data in the dashboard's outcome display with the real route's data. Keep the routing-SETTINGS section fixture-labeled and untouched - that half is blocked on a sightmesh settings bridge and stays out of scope.
- Never expose `auth_binding_id` or secret-adjacent fields in the projection.
- Owned paths: `crates/server/src/routes/` (new/extended route), `crates/db` read query only (no schema change), `shared/types.ts` via generator, `packages/web-core` routing components.

## Proof
Focused cargo test for the route (empty, populated, unknown session). `cargo fmt`, workspace clippy `-D warnings` on touched crates, `generate-types:check`, frontend package checks for touched packages. Full local gate before ready.

## Delivery (lane policy C)
PR to clarkipeng/cdesktop main. Self-mark ready on green local gates; append STATUS to `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-q-status.md` AND `sightmesh parent --message`. Reviewer merges. No background processes; never message retired sessions.
