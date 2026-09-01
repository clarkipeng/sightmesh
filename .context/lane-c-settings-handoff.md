# Lane C: SightMesh routing policy and settings — checkpoint handoff

Status: **checkpointed, stable, not merged.** Stopping here per manager checkpoint authority; no further implementation this session.

## Repo state

- Repo: `sightmesh`
- Worktree: `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/5709-lane-c-settings/sightmesh`
- Branch: `cdt/5709-lane-c-settings`
- Base SHA: `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` (exact, as assigned)
- Head SHA: `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`
- Working tree: clean, no stash entries, branch up to date with `origin/cdt/5709-lane-c-settings` (pushed)
- PR: [#18](https://github.com/clarkipeng/sightmesh/pull/18) — draft, open, not merged, not marked ready

## Delivered

- `src/sightmesh/execution_routing.py` (new) — versioned settings store (`~/.config/sightmesh/execution_routing.json`, schema version 1) matching plan section 6 exactly, plus `select_route()` implementing the section 8 selection algorithm.
- `src/sightmesh/cli.py` (modified, +160 lines, additive only) — `sightmesh routing show|validate|set-metered auto|ask|never|routes list|add|remove|order|explain --workspace <id> [--model <preferred>]`. Did not touch `_profile_selection`, the `pool` CLI subcommands, or `src/sightmesh/routing.py` (unrelated bridge/peer module).
- `tests/test_execution_routing.py` (new, 19 tests) — covers plan section 17 Selection + Metered policy matrix: first healthy subscription wins; cooling/missing-credential/disabled/zero-quota accounts skipped in stable order; new pool.json entry discovered live with no code change; preferred-model mismatch advances to next route; cross-provider (Codex→Claude) fallback preserves target shape; `auto`/`ask`/`never` metered policies (including proof that `never` never calls `pool_core.find` for the metered account); invalid settings rejected at construction and at load; Route validation (subscription requires `accountPool` not `account`, metered the reverse); secret-free persistence + 0600 file mode; 2 CLI-level wiring tests.

### Interface for Lane D

```python
from sightmesh.execution_routing import ExecutionRoutingStore, select_route

settings = ExecutionRoutingStore().load()
result = select_route(settings, preferred_model=None, exclude_account_ids=frozenset())
# result.status: "resolved" | "approval_needed" | "blocked"
# result.target: SelectedTarget(route_id, executor, model, billing_class,
#                                auth_binding_id, account_alias) | None
# result.trace: tuple[str, ...]  — safe human-readable, backs `routing explain`
# result.reason: "routing_disabled" | "routes_exhausted" | "approval_needed" | None
```

`auth_binding_id` is always the opaque `pool_core` account id — never a resolved credential (field naming matches the section 14 attempt-metadata contract so Lane D can consume directly). `select_route` reads `pool_core` live on every call (no caching/mirroring) so newly added or newly cooled accounts participate immediately. It is a pure reader — it does not set cooldowns or mutate pool/state; that stays `pool_core`'s job elsewhere.

## Checks run

**Focused (required by assignment):**
```
pytest tests/test_execution_routing.py tests/test_pool.py tests/test_profiles.py -q
```
→ 51 passed, 0 failed.

**Full suite** (`pytest -q`, run at head `e3cab92`):
→ 198 passed, 4 failed:
- `test_cli.py::test_batch_response_answers_questions_and_approves_plan`
- `test_cli.py::test_spawn_direct_acquires_workspace_lease`
- `test_cli.py::test_spawn_worktree_acquires_container_lease`
- `test_cli.py::test_unattended_worktree_selects_bypass`

All four fail with `AttributeError: 'WorktreeClient'/'FakeSpawnClient' object has no attribute 'workspace_summaries'` inside `_fleet_sessions` (`cli.py`), unrelated to anything touched in this lane. **Confirmed pre-existing**: re-ran the identical 4 tests after `git checkout --detach 5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` (exact base SHA) — same 4 failures, same traceback, before this lane's diff existed. Then returned cleanly to `cdt/5709-lane-c-settings` at `e3cab92` (verified clean tree, correct HEAD). Not fixed here — out of Lane C's owned scope (test fixture gap in `test_cli.py`'s fake spawn clients, unrelated to routing/settings).

Also ran: `ast.parse` syntax check on all three changed/new files (pass); manual CLI smoke test of every `routing` subcommand in an isolated `$HOME` (settings persist, 0600 permissions, invalid `meteredFallback` value rejected).

## Remaining scope (not started, per original assignment boundaries)

- Wiring `select_route()` into `spawn` / teammate spawn / queued dispatch / durable recovery — explicitly Lane D's job, not attempted.
- UI surfaces — Lane E.
- cdesktop-side auth binding resolution, approval payload, normalized outcome contract — Lanes A/B (separate repo/worker).
- The 4 pre-existing `test_cli.py` spawn-fixture failures above — not owned by this lane, flagged for whichever lane/worker owns `test_cli.py`'s fake spawn client fixtures.

## Exclusions honored

- Did not read other lanes' assignments.
- Did not implement cdesktop changes, UI, or dispatch/reconciler wiring.
- Did not modify `_profile_selection`, `pool` CLI subcommands, or `src/sightmesh/routing.py`.
- Did not merge, mark ready, or take any release action. PR #18 remains draft.
