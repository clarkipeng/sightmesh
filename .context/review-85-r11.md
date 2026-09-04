# PR #85 review — round 11

Reviewed `6eb17deb29be951258991c24083cf4d1c12c5645` against `origin/main` on
2026-09-03. `origin/main` is an ancestor of this exact head. GitHub reports
all ten checks green (the three supported Python versions plus the pinned
cdesktop/source-package checks), and the local suite passes: `655 passed`.

## Verdict: BLOCK

The reported `managed_launch` release is real and its late-result handling is
correct: the durable reservation precedes the PUT; cancellation can commit
during the paused PUT; re-entry sees the changed row; the native workspace is
stopped; and the epoch's effect is recorded `terminal/superseded`, never
activated. `tests/test_sdk.py::test_cancel_can_win_while_managed_launch_is_in_flight`
passes. Temporarily removing `fence.external_io()` at `sdk.py:907` makes that
test time out waiting for `cancel`, and the exact source was restored.

S-D16 (including the liveness-loss race) and the existing transition-caller
structural guard pass. The latter only proves that lifecycle-store writers pass
a fence token; it does not inspect cdesktop I/O while that token is held.

### P1 — cdesktop requests still execute while the task fence is held

* `src/sightmesh/sdk.py:527` calls `execution_processes()` under the fence
  acquired at `sdk.py:508`; its `session_commands()` follow-up at `sdk.py:558`
  is in the same fenced path. A stalled cdesktop GET during liveness/provider
  observation prevents `cancel`, `complete`, loss recording, or replacement
  for that task from acquiring the lifecycle gate.
* `src/sightmesh/sdk.py:655` calls `_require_contract()` under the fence from
  `sdk.py:575`; its uncached path calls `client.info()` (`sdk.py:1216`) and
  `client.managed_effect()` (`sdk.py:1236`). This first failover can therefore
  block the same lifecycle writers on a slow probe. The same locked path calls
  `_default_provider_id()` at `sdk.py:647`, which calls `client.providers()` at
  `sdk.py:1103`.
* `src/sightmesh/sdk.py:790` invokes `transfer_ownership()` while `replace()`
  still owns the fence (`sdk.py:333`). Its cdesktop operations are
  `src/sightmesh/succession.py:355` (`session_commands`), `:376` (`send`), and
  `:382`/`:385` (`interrupt_command`/`stop_execution`).

Failure path: a cdesktop request pauses in any of these paths; an operator
cancels or completes the same task (or the liveness detector records loss),
and waits behind an unbounded HTTP request—the exact liveness regression this
round is intended to close. Smallest robust fix: make every such I/O phase an
explicit `fence.external_io()` boundary, capture the task epoch/version before
it, and reload/check it before making a lifecycle decision from the response.
For replacement handoff, split the durable ownership decision from the
external transfer/delivery phase and apply the same re-entry check before
activating or forwarding to a successor.

### P1 — late-result cleanup stops a workspace under the fence

* `src/sightmesh/sdk.py:931` calls `client.stop_workspace()` after the
  `managed_launch` re-entry detects supersession, but has reacquired the task
  fence.
* `src/sightmesh/effects.py:299` does the same after an expired-reservation
  `managed_effect` lookup re-enters and discovers the row changed.

Failure path: the stale launch/lookup result is correctly fenced and marked
`superseded`, but a slow workspace stop then holds the task gate; terminal
writes and replacement are stalled until that HTTP call returns. Smallest
robust fix: retain the durable supersession decision under the fence, release
it with `external_io()` for `stop_workspace`, and do not make later activation
from that old result.

## Requested checks run

* `uv run --with pytest pytest -q tests/test_sdk.py::test_cancel_can_win_while_managed_launch_is_in_flight tests/simulator/test_routing_scenarios.py -k sd16 tests/test_task_store.py::test_every_transition_caller_passes_the_fence_capability` — `7 passed`.
* `uv run --with pytest pytest -q` — `655 passed in 20.38s`.
* The `managed_launch` release mutation caused the focused cancellation test
  to fail with `TimeoutError`; it was restored before this review commit.
* `gh-axi pr checks 85` — `10 passed, 0 failed` at PR head `6eb17de`.

The in-flight test directly verifies cancellation only. There is no regression
test that blocks the same cdesktop call and proves `complete`, loss, and
replacement return without waiting; the existing S-D16 tests still encode
waiting for a recovery launch in two scenarios. Add one parameterized
in-flight lifecycle test (cancel/complete/loss/replace) plus an AST or
instrumented fence-I/O guard so a new client call under a task fence fails
structurally.
