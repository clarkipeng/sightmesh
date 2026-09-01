# Lane AA - workspace lifecycle and sidebar grouping

Base: cdesktop origin/main (post #19), sightmesh origin/main (post #51).

Three defects surfaced by real use during the soak. One writer, two repos.

## 1. Archive guard catch-22 (sightmesh)
The CLI's pre-flight dirty check refuses to archive a workspace whose worktree directory is missing ("repository path is missing" treated as dirty state), while `workspace delete` refuses because the workspace is not archived. Workspaces whose directory does not exist on disk are structurally unremovable. Live evidence: 170 workspaces hit this; the operator had to bypass the CLI and call cdesktop's `PUT /api/workspaces/{id}` directly, which handles missing worktrees correctly.
Fix: in the dirty-state check, a missing worktree directory is reconciled by definition - there is nothing on disk to lose. Archive and delete must both proceed. Keep the guard strict for directories that EXIST with uncommitted changes. Add a regression test with a workspace row whose directory is gone.

## 2. Auto-archive policy (cdesktop, with sightmesh wiring if needed)
Workspaces accumulate forever because archiving is a manual captain ritual with no enforcement; 400+ accumulated in a week of fleet use.
Fix: an automatic archive for workspaces that are (a) not running, (b) have no pending approvals, and (c) have been idle beyond a threshold. Default threshold 7 days; configurable via the existing settings surfaces; never auto-archive pinned workspaces. Ride the existing reconciler sweep - no new daemons. The sidebar should make auto-archived workspaces discoverable (existing archived filter is fine). State clearly in the PR what happens to unmerged work in an auto-archived workspace: the worktree is preserved per the existing one-hour retention, and the decision to keep the default at 7 days is called out for founder review.

## 3. Sidebar duplicate repo groups (cdesktop frontend)
The sidebar groups sessions by repo NAME, so two different local checkouts of the same repo render two identical "catapult-games" headers. Live evidence: two catapult-games groups visible simultaneously.
Fix: group by repo path (the identity that actually differs), and when displaying, show the name plus a disambiguating suffix only when two groups would otherwise render the same label. Data-driven: the backend already knows each workspace's repo path; attach it to the grouping payload rather than inferring in the frontend.

## Guards
Bloat rules apply; smallest robust diffs; reuse the reconciler and existing settings surfaces. Tests: one per defect, from real captured state where possible (the catch-22 test can construct the exact live condition). Policy C: full local gates per repo, draft PRs, self-ready on green, durable completion signal with exact heads and evidence. Report BLOCKED before ending your turn. Stop condition: PRs delivered on both repos.
