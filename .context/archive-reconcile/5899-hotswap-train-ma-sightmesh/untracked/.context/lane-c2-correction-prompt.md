You are Lane C2, a visible isolated correction worker for the subscription hot-swap train.

Base and delivery: start from the exact Lane C PR #18 head `e3cab92cee77a4dbc2dde0fe522d518d6b9986da` (`cdt/5709-lane-c-settings`). Keep that PR as your stack base. Produce a clean, pushed draft successor head; do not merge, ready a PR, publish, dispatch workflows, mutate secrets, or update runtime locks.

Objective: correct only the three proven Lane F defects in Lane C, without changing the settings shape.

Owned paths only:
- `src/sightmesh/execution_routing.py`
- `src/sightmesh/cli.py` (routing validation only)
- `tests/test_execution_routing.py`

Required corrections:
1. When `exposeAccountAlias=false`, selection traces must not expose account ids or aliases.
2. Route selection—including a metered `ask` result—must not resolve secret-bearing launch material. Use only non-secret eligibility/presence facts during selection; actual binding resolution stays at executor launch.
3. `routing validate` must truthfully report routes with no eligible account, covering disabled, cooling, missing-credential, zero-quota, and incompatible subscription API-key candidates. Align CLI text/output with behavior.

Product constraints: preserve subscription-first ordering, opaque `auth_binding_id` only, `auto`/`ask`/`never` semantics, and the frozen public settings schema. Do not touch pool implementation, docs, settings shape, or any cdesktop code.

Proof: add focused regressions for all three defects, run the focused routing suite, and report exact commit SHA, changed paths, test command/result, and any remaining concern. Stop after the clean pushed checkpoint.
