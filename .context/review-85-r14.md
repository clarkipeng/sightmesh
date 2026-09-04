# PR #85 review — round 14

Head reviewed: `faf5b2237849761ce9771f9a873b9aea31aeda16` (`cp/routing-wiring`), against `origin/main`.

## Verdict: APPROVE

No P-level findings remain. Rounds 7–14 have closed the routing surface.

## Verification

- `origin/main` is an ancestor of the reviewed head.
- `rg` over every `src/**/*.py` hit for `urlopen`, `websockets.connect`, `http.client`, `socket`, `requests`, and `urllib3` found only the guarded transport openings. The only `socket` use is `socket.gethostname()` in `leases.py`, not a transport opener. `open_transport()` rejects I/O while the task fence is held (`src/sightmesh/fence.py:15-26`). The structural test derives its module list from `Path(cdesktop.__file__).parent.rglob("*.py")`, so it scans all source modules rather than a hardcoded file list (`tests/test_cdesktop.py:35-81`).
- Default discovery is fenced: `service.is_healthy()` opens through the same guard (`src/sightmesh/service.py:118-125`), and its fence test passes (`tests/test_cdesktop.py:26-32`). As a negative control, I locally replaced that call with bare `urlopen`; the discovery test failed with `DID NOT RAISE FenceHeldError`; I restored the exact guarded source before the remaining checks.
- In a launch/cancel race with `managed_launch` paused in flight, the new workspace is durably marked superseded and stopped after `fence.external_io()` releases the task fence (`src/sightmesh/sdk.py:906-935`). I additionally made the fake client fail on that first stop: the persisted `workspace_id` remained, `reconcile_superseded()` retried the stop, and the record converged to `workspace_id is None` (`src/sightmesh/effects.py:211-283`). No `FenceHeldError` occurred.
- Focused coverage passed: S-D16, liveness-loss, in-flight cancellation, fenced probe/WebSocket, default discovery, and the source-wide transport structural test: `10 passed, 55 deselected`.
- Full suite passed: `682 passed` on Python 3.11, 3.12, and 3.13. PR #85 reports all 10 exact-head checks passing, including pinned cdesktop artifact, package-edge advisory, and test matrices for 3.11–3.13.

## Findings

None.
