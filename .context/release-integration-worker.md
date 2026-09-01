# Authority

The user explicitly asked SightMesh to execute its release-readiness plan. You are the sole integration owner for shared composition files.

# Base and reviewed inputs

Start from exact base `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`.

Integrate these exact reviewed branch heads in order:

1. PR #13 trust hardening: `e91319f9d2103bb5670e6b748b891776c4685007`
2. PR #15 durable manager wake: `a3cc55e471df27dc58ea7038dac35ce1d9e6d0e1`
3. PR #12 fleet projection: `9429f216897a0b4e0ba43d79894b33432ca894d6`
4. PR #14 public docs: `333df45194af6759df63f06b3a690cd1c884e1f0`

Each head contains two commits from the common base. Preserve the reviewed commits and resolve only genuine integration conflicts. Before writing, verify every head still matches the remote PR head.

# Objective

Create one coherent experimental release candidate without widening native ownership.

1. Compose the reviewed branches in the stated order.
2. Wire `Lease.to_public_dict()` into every normal status, list, spawn, restore, archive, and diagnostic output that does not explicitly need to return a capability. Keep raw tokens only in the narrow explicit lease capability commands that require them, and document that boundary in help/tests.
3. Add the smallest useful fleet overview CLI composition using the pure `sightmesh.fleet` projection and current native readers. Default output must group Needs attention, Running, and Done since view with stable unique selectors, one reason, and one safe next action. Structured output must use the privacy-safe projection. Do not create persistence, a second transcript, or a GitHub mirror.
4. Preserve existing machine-readable status compatibility where reasonable. Prefer a new `overview` command if changing `status` would break current scripts. Keep `status --json` sanitized.
5. The durable reconciler requires cdesktop 0.2.6 APIs, while the public bootstrap still pins released cdesktop 0.2.5. Add a feature-specific capability or version gate so 0.2.5 fails closed with one clear bounded diagnostic and does not repeatedly call unsupported recovery endpoints. Do not globally reject 0.2.5 or point bootstrap at an unpublished artifact.
6. Update only the narrow help and docs necessary for the integrated command and version boundary. Keep the project experimental and do not claim the multi-week proof is complete.
7. Add focused integration tests for token redaction across lifecycle output, fleet grouping through CLI inputs, structured privacy, and the 0.2.5/0.2.6 durable feature gate.

# Ownership and exclusions

You own `src/sightmesh/cli.py`, `tests/test_cli.py`, and exact integration changes required in already-reviewed files. You may make narrow documentation adjustments.

Do not change credential policy, add a database, add polling/watchdogs/caffeinate, publish cdesktop 0.2.6, mark any PR ready, merge to main, publish a package, or edit generated benchmark results.

# Proof

Run:

- exact remote-head verification;
- focused changed-behavior tests;
- the full suite with cdesktop session environment removed to match CI;
- Ruff on changed Python;
- package smoke;
- local Markdown link and GitHub YAML validation when docs change;
- `git diff --check`;
- a secret-pattern review of default CLI output fixtures.

Re-run only the latest GitHub CI for your exact head. Do not treat inherited visible-session environment failures as product failures.

# Delivery

Commit and push an integration branch. Open one draft PR against `main` with `~/.local/bin/gh-axi`. In the PR, list the four source PRs and exact heads, integrated behavior, checks, known 0.2.6 dependency, remaining multi-week proof gate, and why the original PRs remain drafts.

# Stop condition

Stop after the draft integration PR is pushed, exact-head CI has started, the worktree is clean, and you report its URL and SHA. Do not mark ready or merge.
