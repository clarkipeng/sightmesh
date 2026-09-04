# PR #85 round 13 review — BLOCK

Reviewed `3497b112e58cb0d3f02b8e2016dc90bb2310d218` (`cp/routing-wiring`) against
`origin/main` `a46d669db594b6180ea0219614ad09a97b90f9fc` on 2026-09-03.
`origin/main` is an ancestor of the exact reviewed head.

## P1 — default client discovery bypasses the task-fence transport boundary

`src/sightmesh/cdesktop.py:250-257` constructs an unconfigured `CdesktopClient`
by calling `_discover_url()`, which calls `service.is_healthy(DEFAULT_PORT)`.
`src/sightmesh/service.py:117-122` opens the localhost health connection directly
with `urlopen`, rather than through `CdesktopClient._open_transport`.

Concrete failure path: code holding `store.task_lock("task-a")` constructs
`CdesktopClient()` without a supplied URL or `SIGHTMESH_CDESKTOP_URL`. The
constructor reaches `service.urlopen` while `HELD_TASK_FENCE == "task-a"`, but
does not raise `FenceHeldError`; the one-second I/O can therefore block a task
lifecycle fence. A direct reproduction monkeypatched `service.urlopen` to
record the ContextVar and raise `URLError`: construction raised its ordinary
`CdesktopError` and recorded `"discovery-task"`, proving the transport escaped
the guard.

Smallest robust fix: make discovery’s health probe pass through the same
fence-aware opener (for example, put the health check on `CdesktopClient` and
call `self._open_transport(urlopen, ...)`), or make `service.is_healthy` itself
use the shared guard. Keep a regression that constructs an unconfigured client
under `task_lock` and expects `FenceHeldError`; extend the structural transport
test to cover this indirect caller.

## P1 — superseded-launch cleanup calls cdesktop after reacquiring the fence

`src/sightmesh/sdk.py:907-910` correctly releases the fence for
`managed_launch`, then `src/sightmesh/sdk.py:928-932` compares the captured
epoch/version after reacquiring it. On a mismatch, however, line 931 calls
`self.client.stop_workspace(workspace_id)` while the fence is held. The same
pattern is in reservation expiry at `src/sightmesh/effects.py:289-300`.

Concrete failure path: cancel a task while its managed launch is in flight.
After the launch returns, the version check identifies it as superseded; the
real `CdesktopClient.stop_workspace()` enters `_open_transport` and raises
`FenceHeldError` instead of stopping the orphaned native workspace. The
existing in-flight-cancellation test passes because its fake client does not
enforce the real transport guard.

Smallest robust fix: after publishing the `superseded` journal outcome, release
the fence around `stop_workspace` (or perform the cleanup after leaving the
lock). Do not add a guard exception: keep the version/epoch recheck before the
cleanup and add a real-`CdesktopClient` or guard-enforcing-double regression
for both launch and expired-reservation cleanup.

## Verified

- `request`, `probe_connectivity`, and the approval-stream WebSocket now call
  `CdesktopClient._open_transport`; held-fence calls to the latter two correctly
  raise `FenceHeldError`.
- Source scan found no `http.client`, `requests`, `httpx`, `aiohttp`, or
  `urllib3` openers. The remaining `urlopen`/`websockets.connect` uses outside
  `cdesktop.py` belong to service discovery (finding above), updater/service
  administration, Repowire, and the bridge—not cdesktop request transport.
- ContextVar behavior was checked directly: a new OS thread sees no held fence;
  an asyncio child inherits its parent task context; `external_io()` clears the
  current context during I/O and restores it afterwards. The launch and
  reservation paths do compare the captured epoch/version after reacquiring the
  fence, but the two cleanup calls above still violate that reacquired boundary.
- Targeted S-D16, liveness-loss, in-flight cancellation, structural fence, and
  transport tests: 7 passed. Full suite: 679 passed.
- Local mutation: removing `assert_external_io_allowed()` from `_open_transport`
  made both fence transport tests fail; the guard was immediately restored and
  the worktree was clean before this report.
- Editable install, package smoke, and pinned runtime check passed. GitHub
  reports all 10 checks green for PR #85’s exact head.

The round-12 direct probe and WebSocket escapes are fixed, but the default
discovery and post-reacquire cleanup escapes mean rounds 7–13 have not yet
closed the routing surface.
