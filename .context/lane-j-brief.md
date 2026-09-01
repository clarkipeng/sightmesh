# Lane J brief: orphan-free failed workspace start

You are the single Lane J writer.
Objective: when a `/workspaces/start` request fails partway (including when `container_ref` is absent), no active orphan survives: no leaked session, execution process, workspace record, or worktree. Make cleanup correct by construction (atomic create-then-activate or compensating cleanup), not by edge-case patching.

## Base, exact and verified

- Repo: cdesktop. Branch off `main` at exact head `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Verify HEAD equals that SHA before writing anything. If it does not, stop and report.

## Ownership

- Yours alone: failed workspace-start cleanup and absent-container deletion paths in cdesktop.
- Not yours: teammate spawn validation (Lane I owns rejected-spawn atomicity; do not touch its paths), session command contract (Lane A1/B), UI (Lane E).
- If the fix genuinely requires SightMesh-side compensation code, do NOT write in the SightMesh repo; report the exact need to your parent and stop at the cdesktop boundary.

## Proof

- A regression test that drives a failed workspace start end to end and asserts zero surviving orphans (session rows, execution processes, container/worktree state). Report exact pass/fail counts.
- `cargo fmt` clean.

## Delivery and stop

- Small checkpoint commits, clean pushed branch, DRAFT PR against `main` on clarkipeng/cdesktop. Draft only: no merge, ready, publish, workflow dispatch, or secret mutation.
- No detached/background/polling processes, no `sleep` monitors. Never message retired or completed sessions.
- When done or blocked: report exact branch, head SHA, PR number, and test counts to your parent with `sightmesh parent --message "STATUS: ..."`, then stop.
