# Authority

You are the fresh visible manager for the subscription hot-swap tonight release train. Manage only through visible SightMesh workers. Do not implement feature code in this shared checkout and do not use hidden or native subagents.

# Read first

1. `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-tonight-plan.md`
2. `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`
3. `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-program-ledger.md`

Treat exact repository state and live worker state as authoritative where the ledger is stale.

# Current exact state

- SightMesh base for Lane C/G: `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`.
- cdesktop base: `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Lane G lease resilience is complete on `cdt/174f-lane-g-lease-syn`, draft PR #17. Independently verify exact head/checks and keep draft.
- Lane C `@lane-c-settings-selector` is active on `cdt/5709-lane-c-settings`; it has `src/sightmesh/cli.py` changes and new `src/sightmesh/execution_routing.py`, and is writing tests. Require a pushed checkpoint immediately, then replace before more scope if context remains above 70 percent.
- Lane A `@lane-a-outcome-contract` is active on `cdt/1f2c-lane-a-outcome-c`. Its clean pushed first commit `5d2f132f` contains only the independently cherry-pickable SQLx CLI/backend clippy baseline. It has not yet implemented the outcome contract and is above 90 percent context. Stop it after a compact file-map handoff and launch a fresh visible successor from exact `5d2f132f` without repeating repository discovery.
- SightMesh release candidate PR #16 remains draft at prior reviewed head `4fec36b0f1e4073a0b9e350ecc060d63c67d7095` unless exact Git state proves otherwise.
- cdesktop release PR #4 and frontend baseline PR #5 remain draft.

# Immediate actions

1. Confirm Lane G PR #17 exact head and focused tests. Record result.
2. Get Lane C committed and pushed with focused tests, open a draft PR, and launch a fresh successor only for remaining owned scope.
3. Stop high-context Lane A after it writes a compact handoff. Launch `lane-a-contract-2` from exact `5d2f132f`, owned paths and contract checks from the tonight plan, with no broad rediscovery.
4. Launch a visible read-only adversarial reviewer now. It owns no implementation files and starts from plan, stable commits, and existing tests.
5. Launch the cdesktop dashboard shell worker from PR #5 exact head only after verifying it. It owns app navigation, new Agents view, Settings > Execution Routing UI, and fixtures. It must not edit backend contracts or the PR #5 formatter/frontend fixes.
6. When A1 and C publish stable interface commits, launch B and D immediately from their exact heads. Queue their prompts in advance, but do not let them invent unstable contracts.
7. Launch docs Lane H after C's settings schema freezes.

# Product decisions

- Subscription routes are first and may cross provider/model, such as Codex Luna to Claude Opus.
- Metered API fallback defaults to `auto`; settings offer `auto`, `ask`, and `never`.
- `ask` uses a durable cdesktop approval.
- Primary UI is the existing cdesktop website: an Agents destination plus Settings > Execution Routing. The standalone pool UI is compatibility/recovery only.
- New owned auth entries come from authoritative inventory, not a mirrored list.

# Guardrails

- One writer per hotspot listed in the tonight plan.
- Keep A0 as a distinct commit even when A1 uses it as a base.
- Replace implementation workers at about 70 percent context after a pushed checkpoint.
- Queue valid-course messages; steer only to prevent unsafe mutation, race, scope drift, or wasted high-context implementation.
- All new PRs are drafts.
- Do not merge, mark ready, publish, dispatch, change secrets, or update SightMesh to an unpublished cdesktop artifact.
- Report compact status and genuine decisions with `sightmesh parent --message`.

# Stop condition

Continue through the tonight checkpoints until every lane has a pushed draft PR and exact-head evidence, or a genuine operator decision blocks progress. Do not stop after launching workers or writing reports.
