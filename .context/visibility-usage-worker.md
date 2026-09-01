# Authority

The user explicitly asked SightMesh to execute its own release-readiness plan.

# Exact base

Use commit `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` from `origin/main`. Do not rebase onto a later head without manager direction.

# Objective

Build a small, testable visibility and usage view model without taking over native owners.

1. Create a pure fleet overview model that accepts current native workspace, execution, approval, relationship, pool, and delivery facts.
2. Group output into Needs attention, Running, and Done since view. Define deterministic urgency, age, stable unique selectors, one reason, and one safe next action.
3. Include model, provider, owned account assignment when available, quota/reset window, last meaningful event, token usage, context window pressure, parent, branch, and PR or CI reference when supplied.
4. Never guess price. Separate model-reported tokens from optional externally supplied monetary cost, and carry provenance.
5. Do not persist facts reconstructable from cdesktop, Git, Repowire, pool state, or GitHub. If a new durable record is truly required, state the unique question it answers and why it cannot be reconstructed, but do not add storage in this lane.

# Owned paths

- new `src/sightmesh/fleet.py` and/or `src/sightmesh/usage.py`
- new directly corresponding test files
- `.context/visibility-integration.md`

Do not edit `src/sightmesh/cli.py`, existing pool files or UI, cdesktop adapter, durable execution, bridge, stalls, installers, README, docs, or GitHub files. A later integrator owns CLI and UI composition.

# Proof

Test deterministic grouping, duplicate selector disambiguation, attention ordering, missing optional facts, token provenance, quota reset display, and privacy-safe serialization. Run focused tests and the full suite.

# Delivery

Commit and push a short checkpoint branch. Open a draft PR against `main` with `~/.local/bin/gh-axi`. Include a small example of the target default overview. Do not add agent co-authors.

# Stop condition

Stop after the draft PR is pushed and report its URL and exact head SHA. Put the exact CLI and UI wiring contract in `.context/visibility-integration.md` without editing shared composition files.
