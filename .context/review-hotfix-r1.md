# PR #109 independent review, round 1

Reviewed exact head `dc5fd78` against `main` (`190b88a`). Verdict: **BLOCK**.

## Findings

### P1 — a fresh replacement or failover still performs HTTP under the task fence

- `src/sightmesh/sdk.py:349`, `src/sightmesh/sdk.py:661`, and `src/sightmesh/sdk.py:692` call `_require_contract()` while `task_lock()` is held. `_require_contract()` calls `client.info()` at `sdk.py:1231`, then `client.managed_effect()` through `_probe_managed_launch()` at `sdk.py:1251`. Both are HTTP in the real client (`src/sightmesh/cdesktop.py:953-957` and the shared `request()` transport).
- Concrete failure: after a manager restart, `replace()` has an empty `contract_probe`; it acquires the task fence then GETs `/info`, tripping `FenceHeldError`. The same cold-instance path blocks automatic failover and recovery-resume. `tests/test_sdk.py:33` guards `info`, but the normal tests warm the cache during `start()`; both test fakes omit the guard on `managed_effect` (`tests/test_sdk.py:125`, `tests/simulator/fake_cdesktop.py:306`).
- Smallest robust fix: make the contract probe a pre-fence prerequisite for every public/reconciler entry path, or put the entire probe in `fence.external_io()` and revalidate the task version before its result informs a transition. Guard `managed_effect` in both HTTP fakes and add a cold-instance replace/failover regression.

### P1 — reopening the gate for request construction permits a stale task to issue a new native launch

- `src/sightmesh/sdk.py:754-762` and `src/sightmesh/sdk.py:843-851` build a request in `external_io()` and immediately call `_journaled_launch()` using the pre-I/O `TaskRecord`. `_journaled_launch()` only notices staleness after `managed_launch()` has already issued its PUT (`sdk.py:919-947`).
- Concrete failure: pause `workspace_launch_request`, cancel the reserved task while its gate is open, then release it. The head sends one native `managed_launch`, subsequently marks it superseded and stops it. A stale task has therefore acted, rather than being rejected before dispatch; the same missing pre-dispatch freshness check covers the no-workspace arm of `_launch_prepared`.
- Smallest robust fix: under the reacquired fence, centrally reload and compare `(epoch, version, state)` before reserving/issuing a native launch. `_replace_prepared` also needs this revalidation after its first `transfer_ownership()` I/O boundary, before ownership mutation/spawn.

## Required verification

- Guard-revert probes correctly failed for: checkpoint read (`test_checkpoint_content_stays_in_the_task_worktree`), initial launch-request build (`test_start_is_idempotent_for_one_semantic_key`), failure observation process read and queue read (`test_a_code_failure_blocks_visibly_and_never_reroutes`), failover provider lookup (`test_a_typed_rate_limit_moves_once_to_the_next_hop`), and transfer ownership (`test_replacements_keep_workspace_and_trip_circuit_breaker`). The `_launch_prepared` workspace-only hunk has no existing failing regression: reverting it while running the failover test remained green because that path replaces an existing workspace with a session request. Add the missing rejected-before-activation then reroute test.
- `rg` review of every `self.client.`/`client.` call under `src/` found the intended guarded calls, plus the unguarded `_require_contract()` calls above. Calls outside task-lock scopes are not affected.
- Staleness: `_advance_past_outcome_locked`'s `prepare_replacement(expect_version=...)` fences the provider lookup result, but `_launch_prepared` and initial start do not fence before native dispatch. The existing post-launch cleanup is correct but too late.
- Initial real-task path: **yes**, a cold `start()` can launch. `start_all()` calls `_require_contract()` before reservation/fence; `_start_reserved_locked()` opens `external_io()` around `_workspace_request()`; `CdesktopClient.workspace_launch_request()` calls `register_repo() -> repos() -> request()` inside that open gate; and `managed_launch()` is also gated. No request on this initial start path occurs while `HELD_TASK_FENCE` is set.

## Commands run

- `uv run --with pytest pytest -q tests/test_cdesktop.py tests/test_sdk.py tests/test_succession.py tests/simulator -m 'simulator or not simulator'` — 191 passed.
- `uv run --with pytest pytest -q` — completed cleanly during review.
- `uv run --with pytest pytest -q tests/simulator -m simulator` — 64 passed, 7 deselected in 8.01s.
- Cold-instance guarded lookup reproduction produced `FenceHeldError`; stale request-build/cancel reproduction produced one native launch followed by cleanup.
