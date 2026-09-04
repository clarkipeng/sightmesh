# SightMesh PR #109 — round 2 review

Reviewed `ec1432c58feaba5ad962d95db322ad7d9240bc22` against `origin/main` on
2026-09-03. `origin/main` is an ancestor of that exact commit.

## Verdict: APPROVE

No P0/P1/P2 findings. The round-1 blockers are fixed without introducing a
new dispatch race: real Cdesktop HTTP is outside a held task fence, and every
launch path that deliberately reopens the fence revalidates the durable task
row before it can issue its native managed launch.

## Path and fence audit

- `_advance_past_outcome_locked` has exactly one production caller:
  `_advance_past_outcome` (`src/sightmesh/sdk.py:566-586`), which performs the
  cold-instance contract probe before acquiring `task_lock`.
- `_resume_replacement` is reached only from that locked advance path
  (`src/sightmesh/sdk.py:588-604`), so it inherits the same pre-fence probe.
- `_launch_prepared` is reached from automatic advance, replacement resume,
  and manual `replace` (`src/sightmesh/sdk.py:331-384`, `664-695`). The two
  automatic paths have the preceding `_advance_past_outcome` probe; the manual
  path probes at `replace:334`, before its fence.
- The workspace arm of `_launch_prepared` opens the fence only for
  `_workspace_request` and rejects a changed `(epoch, version)` at
  `src/sightmesh/sdk.py:753-764`. `_start_reserved_locked` follows the same
  pattern at `860-882`, returning the reloaded row rather than launching it.
- The session replacement arm’s request builder is a static, in-process
  payload constructor (`src/sightmesh/cdesktop.py:912-940`). Its first real
  request, `session_commands`, is under `io=fence.external_io`, and
  `before_spawn` revalidates immediately before the native spawn
  (`src/sightmesh/succession.py:362-379`; `sdk.py:799-808`).
- A second grep/manual trace of every `self.client`/`client` call reachable
  while `task_lock` is held found HTTP calls wrapped in `fence.external_io`:
  process/queue observation, provider lookup, workspace request, managed
  launch, superseded stop, and ownership command read/send/cancel. The only
  unwrapped request-builder is the static session payload function above.

## Regression and mutation evidence

The shipped `test_a_cold_instance_probes_the_contract_before_taking_the_fence`
passes. Moving the `replace` probe back under the fence made it fail with
`FenceHeldError`; removing the request-build `external_io` made
`test_start_is_idempotent_for_one_semantic_key` fail with the same guard.

The shipped `test_cancel_during_request_build_never_issues_a_native_launch`
covers the reserved-start arm. I added two temporary, uncommitted scratch
tests for the uncovered requested arms, then removed them after execution:

- cancellation while automatic recovery’s `_launch_prepared` workspace request
  gate was open issued no epoch-2 native launch;
- cancellation while `_replace_prepared` was between `session_commands` and
  `spawn` issued no replacement native launch.

Removing `_launch_prepared`’s post-request `_require_unchanged` made the first
scratch test observe a second native launch. Removing
`transfer_ownership`’s `before_spawn` made the second scratch test fail after
the stale replacement reached native launch handling. The fake HTTP guards,
including `managed_effect`, are now present in both test doubles; the cold
instance mutation proves that the probe’s actual `info` and `managed_effect`
contract path is fence-checked.

## Verification

- Exact-head local suite: `684 passed` (the temporary two-arm probes made the
  audit run `686 passed`, then were removed).
- Simulator suite: `71 passed`.
- GitHub compatibility CI for the exact head: run `33835042905`, completed
  successfully; Python 3.11, 3.12, 3.13, pinned cdesktop artifact, and the
  advisory cdesktop-main edge all passed.
