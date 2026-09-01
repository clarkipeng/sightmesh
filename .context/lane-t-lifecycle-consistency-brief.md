# Lane T brief: archive retires sessions + spawn resolves origin bases (Lane R finding 7, issue #37)

Single writer. Read `.context/lane-r-findings.md` item 7 and clarkipeng/sightmesh#37 first.

## Base
sightmesh `main` (must contain PR #35 and #36 squashes). Verify before writing.

## Scope
1. Finding 7: `workspace archive` (and the archiving path of `close`) stops and archives but never records the workspace's sessions in `OwnershipStore`, so quarantine has a race window. On archive, retire every session of the workspace in the ownership store (state `superseded` or `archived`, reason `archive`) BEFORE the archive call returns; reuse the existing retire machinery, no new store.
2. Issue #37: `spawn --base <branch>` resolves the stale local branch. When `origin/<base>` exists and differs from the local ref, use the origin ref for worktree creation; refuse with a clear message only when neither resolves. Add `--local-base` to force the local ref. Update the spawn error text to say which ref was used.
3. Owned paths: `src/sightmesh/cli.py`, `src/sightmesh/succession.py`, `src/sightmesh/leases.py` if worktree creation lives there, matching tests.

## Proof
Why-docstringed tests: archived workspace sessions reject message/steer/prompt-idle via quarantine immediately after archive; spawn base resolution picks origin when local is stale, honors --local-base, refuses cleanly when absent. Full suite green. 

## Delivery (lane policy C)
Draft PR then self-mark ready on green local gates, explicit `--repo clarkipeng/sightmesh`, base main. Append STATUS to `.context/lane-t-status.md` AND `sightmesh parent --message`. Reviewer merges. No background processes; never message retired sessions.
