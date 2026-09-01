Final reconciliation for cdesktop-026-durable-proof:

- Exact HEAD and `origin/main`: `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- No source changes, commits, pushes, service changes, tag changes, or GitHub mutations.
- Focused disposable evidence passed: 7 session-command recovery tests, 7 replay-safe stop-operation tests, and 4 server route response-mapping tests.
- The proof establishes durable row/dedupe preservation, one-time process-scoped requeue, stable accepted/rejected/interrupted replay, bounded 425/409/424 outcomes, and no inferred success after orphaned stops.
- No concrete cdesktop defect was found. This is sufficient for the source-side cdesktop 0.2.6 recovery contract, but it does not replace the multi-week operational observation gate.
- The only untracked file is the intentional proof report; move it to the SightMesh repository `.context` handoff area before archiving this managed worktree.
