# PR #85 review — round 12

Reviewed `origin/cp/routing-wiring` at immutable head
`b4ebab2b23033c94d4b67b1ba73420eb59fabdfe` against `origin/main`.

## Verdict: BLOCK

### P1 — the claimed universal runtime HTTP fence has unguarded cdesktop transports

`src/sightmesh/cdesktop.py:461-467` implements `probe_connectivity()` with a
direct `urlopen`, bypassing the guard at `CdesktopClient.request()` (`:274-297`).
`src/sightmesh/cdesktop.py:543-583` likewise opens the approval-stream
websocket directly. Concrete failure path: while any task holds
`TaskStore.task_lock(task_id)`, a call to `client.probe_connectivity()` performs
up to one second of cdesktop I/O while retaining the lifecycle fence; it returns
`True` rather than raising `FenceHeldError`. The review scratch test reproduced
this at `r12-bypass`. This means the class is not closed by construction: a
current or future fenced caller can reintroduce the round-11 liveness stall by
using either public client path. Smallest robust fix: invoke
`assert_external_io_allowed()` at every direct cdesktop transport boundary
(currently `probe_connectivity()` and `_pending_approvals_websocket()`), or
centralize those transports behind the already guarded request boundary; then
extend `tests/test_cdesktop.py` to cover each bypass path under `task_lock`.

Apart from that P1, the originally fenced task paths are correctly structured:
`TaskStore.task_lock()` records the held task in a `ContextVar`
(`src/sightmesh/task_store.py:323-344`), `TaskFence.external_io()` drops that
state exactly while releasing the advisory lock (`:217-237`), and the
production `CdesktopClient.request()` rejects every HTTP request before
constructing transport (`src/sightmesh/cdesktop.py:274-297`). This is not a
test-only hook: the checked-in boundary test is
`tests/test_cdesktop.py:14-24`. An uncommitted scratch test independently
confirmed that a fenced `client.info()` raises `FenceHeldError` before
transport, while locally monkeypatching only the guard reaches the transport
assertion.

The fence-holding source scan found no direct `client.*` call outside an
`external_io()` phase. The only client-shaped calls retained under a fence are
the pure request builders `session_launch_request` and
`workspace_launch_request` (`src/sightmesh/sdk.py:795-804,841-856`); neither
uses the HTTP boundary. The four former fenced-I/O paths have an unlocked
phase and a post-I/O epoch/version recheck:

* Observation: `src/sightmesh/sdk.py:525-534`.
* Failover authorization: `src/sightmesh/sdk.py:655-664`.
* Replacement handoff: `src/sightmesh/sdk.py:816-831`.
* Late launch cleanup: `src/sightmesh/sdk.py:962-967`.

The raced fake-client coverage passes: S-D16’s paused replacement/cancel and
manual replacement races show the stale launch effect becomes `superseded`
and is never activated (`tests/simulator/test_routing_scenarios.py:1003-1034,
1122-1191`); the initial in-flight cancellation test has the same assertion
(`tests/test_sdk.py:198-224`); the liveness-loss race confirms no successor is
launched after a competing loss (`tests/simulator/test_routing_scenarios.py:1037-1092`).
For observation and pre-reservation failover authorization, a stale result is
discarded by the version recheck before it can create an effect or transition;
there is therefore no native result to terminalize, while native late results
are explicitly journaled `superseded`.

Verification run at exact head (the existing suite does not expose the P1):

* `uv pip install --python .venv/bin/python -e .` — pass.
* Targeted fence, structural caller guard, S-D16/liveness, and in-flight
  cancellation tests — `7 passed`.
* Full suite — `678 passed`.
* `PYTHON_BIN="$PWD/.venv/bin/python" ./scripts/package-smoke.sh` — pass
  (wheel/sdist build, isolated install, CLI, metadata).
* `git merge-base --is-ancestor origin/main b4ebab2` — pass.
