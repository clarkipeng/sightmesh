# Lane O brief: one owner for stop/attempt accounting (issue #28)

Single writer. Read clarkipeng/sightmesh#28 first. Goal is NET-NEGATIVE code.

## Base
sightmesh `main` (must contain #34's squash). Verify before writing.

## Problem
`durable.py` keeps `recovery_attempt`/`recovery_state` bookkeeping via `NativeCommandQueue.recovery()` -> `client.update_command` - an endpoint no real cdesktop serves (hasattr-guarded, exercised only by test fakes). cdesktop now owns attempt state: persisted session command attempts (A1) and replay-safe keyed stops. Two accounting systems, one real.

## Scope
- Delete the sightmesh-side recovery-attempt bookkeeping: `recovery()`, `update_command` fake methods, `recovery_attempt`/`recovery_state` reads, and the stop dedupe key derives from the durable command id alone (cdesktop's keyed-stop contract already replays one outcome per key).
- Preserve every behavior the deleted state guarded: no duplicate stops across sweeps and restarts must still hold - prove it with the existing tests reworked to the real contract, not deleted. If a guarantee genuinely cannot be preserved without the state, STOP and report instead of keeping both systems.
- Owned paths: `src/sightmesh/durable.py`, `src/sightmesh/cdesktop.py`, `tests/test_durable.py`, `tests/test_succession.py` only.

## Proof
Focused tests with why-docstrings for exactly-once stop across repeated sweeps AND reconciler restart. Full suite green (`env -u CDESKTOP_SESSION_ID uv run --with pytest --with build pytest -q`). Report the diff's net line count.

## Delivery (lane policy C)
PR to clarkipeng/sightmesh main (explicit --repo). Self-mark ready the moment local gates pass; append "STATUS: branch, head, PR, tests, net-lines" to `.context/lane-o-status.md` AND `sightmesh parent --message`. Reviewer merges. No background processes; never message retired sessions.
