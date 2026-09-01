# Authority

The user explicitly asked SightMesh to execute its own release-readiness plan.

# Exact base

Use commit `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` from `origin/main`. Do not rebase onto a later head without manager direction.

# Objective

Harden SightMesh's release trust boundary with the smallest robust changes.

1. Stop default and diagnostic views from exposing actionable lease tokens. Add a sanitized public lease representation that preserves useful identity and age. Do not edit `src/sightmesh/cli.py`; the later integrator owns that shared composition file.
2. Remove the blanket `uv tool uninstall agent-deck` behavior. It was legacy rename cleanup and can delete the separate upstream Agent Deck tool. Preserve explicit migration of SightMesh's former owned labels and paths only.
3. Add an idempotent local uninstall script or equally small reversible mechanism that removes only documented SightMesh-owned links, tool installation, and LaunchAgents. It must refuse ambiguous or unrelated targets.
4. Harden the loopback pool server at its existing boundary: bounded request bodies, supported content type, Host and Origin behavior, and secret-safe errors. Add focused tests.
5. Keep account selection limited to accounts the operator owns and authenticated normally. Do not add credential extraction, header replay, or limit evasion.

# Owned paths

- `scripts/install-local.sh`
- a new narrowly named uninstall script if needed
- `src/sightmesh/leases.py`
- `src/sightmesh/pool/server.py`
- `tests/test_leases.py`
- a new pool-server-specific test file
- new narrowly scoped tests for installer behavior

Do not edit `README.md`, `docs/**`, `.github/**`, `src/sightmesh/cli.py`, durable execution, bridge, stalls, or cdesktop adapters.

# Proof

Run only focused tests for changed behavior, then the full test suite and package smoke if those focused checks pass. Inspect the final diff for secrets and unrelated cleanup.

# Delivery

Commit and push a short checkpoint branch. Open a draft PR against `main` with `~/.local/bin/gh-axi`. State exactly what was hardened, the checks run, and any remaining integration step. Do not add agent co-authors.

# Stop condition

Stop after the draft PR is pushed and report its URL and exact head SHA. If a required change belongs to a shared excluded file, leave a concise integration note in `.context/release-trust-integration.md` and do not edit that file.
