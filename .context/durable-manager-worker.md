# Authority

The user explicitly asked SightMesh to execute its own release-readiness plan.

# Exact base

Use commit `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` from `origin/main`. Do not rebase onto a later head without manager direction.

# Objective

Close the manager liveness and delivery gaps using the existing durable command owner.

1. Inspect the current durable reconciler and cdesktop 0.2.5/0.2.6 API boundary before writing.
2. Make manager wake-up derive from durable child state transitions. Ordinary child completion, failure, stall escalation, or resolved delivery should wake the recorded parent at most once when unresolved owned work remains.
3. Define and implement as much as this repository can own of a single delivery lifecycle: queued, claimed, observed or running, terminal, rejected, and expired. Reuse native cdesktop command and execution records rather than creating a mirror.
4. Ensure restart and stream-death reconciliation cannot create duplicate turns or wake loops. Bound retry and preserve evidence.
5. Treat external watchdogs as observers only. Do not add polling hacks, caffeinate requirements, a second scheduler, or another database.

# Owned paths

- `src/sightmesh/durable.py`
- `src/sightmesh/stalls.py`
- `src/sightmesh/bridge.py`
- `src/sightmesh/cdesktop.py`
- the directly corresponding test files
- new focused tests for parent wake and delivery lifecycle

Do not edit `src/sightmesh/cli.py`, pool code, install scripts, README, docs, GitHub files, leases, or profiles.

# Proof

Add focused tests that prove dedupe, restart recovery, terminal transitions, and no wake loop. Run those tests, then the full suite. If cdesktop 0.2.6 lacks a required native fact or mutation, document the exact missing API and safe fallback in `.context/cdesktop-durable-contract.md`; do not invent mirrored state.

# Delivery

Commit and push a short checkpoint branch. Open a draft PR against `main` with `~/.local/bin/gh-axi`. State the exact proven transitions, checks, and any cdesktop dependency. Do not add agent co-authors.

# Stop condition

Stop after the draft PR is pushed and report its URL and exact head SHA, or after proving the change is blocked on a precise cdesktop contract and committing every independently useful SightMesh part.
