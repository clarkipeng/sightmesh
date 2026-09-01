# Lane G worker: spawn reconciliation resilience (lease sync fault isolation)

You are one visible SightMesh worker owning a narrow, independently-shipped regression fix. This is not part of the subscription hot-swap feature lanes (A-F) — do not read that plan, do not touch settings/routing/pool code. This is an infra hardening fix uncovered while operating the fleet.

## The bug

`sync_active_workspaces()` in `src/sightmesh/leases.py:450` iterates every non-archived cdesktop workspace and calls `_sync_active_workspace()` (`leases.py:407`) on each. That inner function raises `LeaseError` for legitimate reasons (e.g. `leases.py:437` "Active workspace has no repository", `leases.py:444` "Active worktree workspace has no container"). Look at the loop in `sync_active_workspaces` (`leases.py:450+`): confirm whether a `LeaseError` from one workspace propagates uncaught and aborts the whole loop, or whether it's already caught per-item (there is an `on_error` callback parameter — check whether every call site actually uses it, and whether the exception is actually swallowed or just logged-then-reraised).

We observed this directly in production: a single malformed workspace (valid `use_worktree=true`, zero sessions, `container_ref` temporarily null — i.e. a workspace mid-creation or from a partially-failed spawn) caused `sightmesh spawn` to fail for every other, unrelated workspace and session on the machine, because `spawn` calls `leases.sync_active_workspaces(client)` (`src/sightmesh/cli.py:684`) before doing anything else. This is a single point of failure: one bad workspace record blocks all unrelated spawn/lease reconciliation fleet-wide.

## Fix

Make `sync_active_workspaces` fault-isolated per workspace: a `LeaseError` (or any exception) syncing one workspace must not prevent the others from syncing. Every call site (`cli.py:684`, `cli.py:1654`, `bridge.py:246`) must still get correct results for all the *other* workspaces even when one is malformed.

Concretely:
- In the loop at `leases.py:450+`, wrap each `_sync_active_workspace()` call so a failure for workspace X is recorded/reported (via the existing `on_error` callback, or by ensuring callers passing no callback still don't lose the whole batch) and the loop continues to workspace X+1.
- Do not swallow the error silently — the caller must still be able to see that workspace X failed to sync (check what `on_error` is used for today at each of the three call sites; if a call site currently has no `on_error` and therefore currently loses failures silently, that's a separate finding — flag it in your PR description, but keep this fix's actual behavior change minimal: isolate failures, don't hide them).
- Do not change the meaning of `LeaseError` for a caller that's asking about *one specific* workspace directly (e.g. anywhere `_sync_active_workspace` is called outside the batch loop, if it exists) — this fix is scoped to the batch/fleet-wide path only.

## Tests

Add to the existing `tests/test_leases.py`. Follow its existing fixture/mocking conventions (read the file first to match style). At minimum:

- A regression test with 3+ fake workspaces where one has `use_worktree=True` and no `container_ref` (reproducing exactly what we hit): assert `sync_active_workspaces` still returns successful leases for the other, healthy workspaces, and does not raise.
- A test that the failing workspace's error is still observable (via `on_error` or an equivalent surfaced result), not silently dropped.
- Confirm no existing test regresses (`pytest tests/test_leases.py -q`).

## Base and delivery

- Repo: `sightmesh`.
- Base: exact SHA `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` (current `origin/main` tip).
- Owned paths: `src/sightmesh/leases.py` and `tests/test_leases.py` only. Do not touch `cli.py`, `bridge.py`, or any routing/settings/pool code — this fix should need zero changes there since the isolation belongs entirely inside `sync_active_workspaces`.
- Work in an isolated worktree. Push your branch and open one draft PR against sightmesh `main`. Keep it draft — do not merge, mark ready, or publish.
- Run `pytest tests/test_leases.py -q` before calling this done.

Report back your worktree path, branch name, exact head SHA, and check results before you consider this checkpoint stable.

## Local agent coordination

- Use `sightmesh peers` and `sightmesh peek @agent` for compact fleet awareness.
- Contact your launcher with `sightmesh parent --message "STATUS: concise details"` when blocked, when a decision is needed, and when complete.
- Do not use hidden or native subagents.
