# Lane C worker: SightMesh routing policy and settings

You are one visible SightMesh worker owning Lane C of the subscription hot-swap program. Full plan (read section 15 Lane C, section 6, section 7 "New authentication entries", section 8, section 13 "CLI" for exact scope):
`/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`

Do not read the rest of this program's other lane assignments. Do not implement cdesktop changes, UI, or actual dispatch/reconciler wiring (Lane D will consume your selector later) — your selector function must be correct and importable, but you are not responsible for calling it from `spawn`/`teammate spawn`/failover paths yet.

## Base and delivery

- Repo: `sightmesh` (Python, `src/sightmesh/`).
- Base: exact SHA `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` (current `origin/main` tip). Do **not** base on branch `cdt/0c5b-release-candidat` — that is an unrelated release-candidate composition PR (#16) that must stay untouched.
- Work in an isolated worktree. Keep the PR draft.

## Current state (already surveyed, act on this directly)

- Account pool: `src/sightmesh/pool/core.py` — `pool.json` (`~/.config/agent-pool/pool.json`, `AGENT_POOL_HOME` overridable) is an ordered account list, position = priority. `select(provider, verify=True)` at `core.py:560-602` walks `accounts_for(pool, provider)` in order, skips cooling/no-credential/zero-quota, probes, returns first workable account. This is single-provider only — it does not know about routes, models, or billing class.
- Nearest existing "versioned settings" pattern: `src/sightmesh/profiles.py` — `Profile` dataclass + `ProfileStore` with `PROFILE_VERSION = 1`, JSON at `~/.config/sightmesh/profiles.json`. Mirror this pattern for your new settings store.
- **Naming collision to avoid**: `src/sightmesh/routing.py` already exists but is unrelated (bridge/peer routing JSON at `~/.config/sightmesh/bridge.json`, `enabled_workspaces`/`peer_ids`). Do not edit or rename it. Name your new module something distinct, e.g. `src/sightmesh/execution_routing.py`. The CLI command group name `routing` (plan section 13: `sightmesh routing show|validate|set-metered|routes ...|explain`) is currently unused as a top-level subcommand — confirmed free by directly checking current subcommands in `cli.py`, so you may still use `sightmesh routing ...` as the CLI group name even though the module file is named differently.
- **Key gap you're filling**: `_profile_selection` (`cli.py:517-548`), used by `_spawn_workspace`/`cmd_spawn`, resolves a named `Profile` (executor + provider_id + model) and never consults the pool at all. `pool_core.select()` is only ever called from the `pool` CLI subcommands themselves (`pool which`/`pool exec`/`pool status`, `cli.py:2384,2401,2410`). These two systems are disconnected today — there is no code that currently picks `max-a` vs `codex-sub1` at actual launch time. Your selector is the first thing that bridges them; do not modify `_profile_selection` or the `pool` CLI subcommands themselves, just build the new selector on top of `pool_core`'s existing account-level primitives (`accounts_for`, cooldown/quota checks, `select`/probe machinery) so Lane D can swap it in later without you having touched dispatch code.

## What to build

1. **Settings model** (new module, e.g. `execution_routing.py`): versioned JSON store matching plan section 6's schema exactly — `executionRouting.enabled`, `routes` (ordered list of `{id, executor, model, billingClass, accountPool|account}`), `meteredFallback` (`auto`|`ask`|`never`, default `auto`), `sameRouteRetries` (0-3, default 2), `transientBackoffSeconds` (bounded int list, default `[5, 20]`), `approvalTimeoutMinutes` (default 0), `allRoutesExhausted` (`block` only), `notifyOnSwap` (default true), `exposeAccountAlias` (default true). Validate before mutation; reject invalid values with a clear error rather than silently coercing (plan section 6, "Invalid values are rejected before mutation"). Never store tokens, headers, expanded credential paths, or provider response bodies in this file (plan section 6, last line).

2. **Route selector**: implement plan section 8's algorithm for a new logical command:
   - Load/validate settings.
   - Enumerate routes in configured order; for each, enumerate its `accountPool` in authoritative pool order (call into `pool_core`, do not reimplement account iteration).
   - Exclude missing credentials, cooldowns, zero quota, disabled providers, incompatible executor/model pairs, and (when you're given one) the prior failed binding if its failure was binding-specific.
   - If the first eligible route is metered, apply `meteredFallback` (do not resolve/expose secrets here — you only ever produce a safe target descriptor, never a resolved credential; that boundary belongs to cdesktop, per plan section 7).
   - Return a resolved target plus a safe, human-readable selection trace (why this route/account, what was skipped and why) — this backs `sightmesh routing explain`.
   - New auth entries must participate without any code change: derive candidates from `pool_core` live each call, never a hardcoded/mirrored account list (plan section 7 "New authentication entries", operator decision "new owned authentication entries must work from authoritative inventory without hardcoded mirrors").

3. **CLI**: add a `routing` subcommand group in `cli.py` per plan section 13 — `show`, `validate`, `set-metered auto|ask|never`, `routes list|add|remove|order`, `explain --workspace <id>`. Follow the existing `profile`/`pool` subcommand structure for argparse wiring conventions. Do not touch other subcommand groups.

## Tests

Follow existing conventions: flat `tests/*.py`, local `monkeypatch`+`tmp_path` fixtures redirecting storage root (see `tests/test_pool.py`'s `pool_root` fixture, `tests/test_profiles.py`'s `tmp_path`-constructed `ProfileStore`), long descriptive test names (e.g. `tests/test_pool.py`'s `test_selection_skips_the_exhausted_account_and_takes_the_next`). Add `tests/test_execution_routing.py` (or your module's matching test name) covering plan section 17's "Selection" and "Metered policy" test matrix at minimum:

- First healthy subscription route wins.
- Cooling/missing/disabled/zero-quota accounts skipped in stable order.
- A new auth entry (added to `pool.json` directly, not through your code) is discovered without any code change.
- Preferred model unavailable on a route advances to the next route.
- Cross-provider fallback (e.g. Codex route to Claude route) preserves the same logical command shape.
- `auto` selects the first eligible metered route once subscriptions are exhausted.
- `ask` produces a request-for-approval outcome rather than a resolved target (Lane B/D will wire the actual approval; you just need the selector to stop and say "approval needed" rather than silently proceeding).
- `never` returns a blocked/`routes_exhausted` outcome without touching any metered account.
- Invalid settings (e.g. `meteredFallback: "sometimes"`) are rejected at load/validate time.

## Proof and delivery

Run `pytest tests/test_execution_routing.py tests/test_pool.py tests/test_profiles.py -q` before calling this done — the last two are regression checks that you haven't broken existing behavior since you're calling into `pool_core`. Push your branch and open one draft PR against sightmesh `main` with the exact head SHA and a summary of the settings schema and selector return shape for Lane D to consume.

Report back your worktree path, branch name, exact head SHA, and check results before you consider this checkpoint stable.
