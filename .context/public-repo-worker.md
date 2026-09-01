# Authority

The user explicitly asked SightMesh to execute its own release-readiness plan.

# Exact base

Use commit `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e` from `origin/main`. Do not rebase onto a later head without manager direction.

# Objective

Make the already-public repository honest, navigable, and ready for an experimental release period without overclaiming reliability.

1. Rewrite the README around the proven wedge: owned-account capacity routing, visible interruptible Claude/Codex sessions, worktree isolation, and preserved recovery state.
2. Mark the project experimental and state the current proof gate: durable manager wake and acknowledged delivery must run under real load for several weeks before active promotion.
3. Put a pool-only quickstart before the full fleet setup if current commands genuinely support it. Include expected outcomes, not only commands.
4. Move deep migration, update, approval, lease, and operations material into a clear docs map instead of keeping the README as an internal manual.
5. Add `SECURITY.md`, `CONTRIBUTING.md`, support expectations, issue templates, and a release-note template. Explain the local credential and arbitrary-command threat boundary, responsible disclosure, supported versions, and owned-account-only policy.
6. Write a concise architecture note suitable for public signal: visible sessions, native ownership, durable commands, stall recovery, and the honest limitations.
7. Improve CI and repository metadata only when the change is directly justified, such as a Ruff check with narrow configuration or badges tied to existing workflows.

# Owned paths

- `README.md`
- `docs/**`
- new public repository policy files
- `.github/**`
- `pyproject.toml` only for lint or development metadata

Do not edit runtime Python, tests for runtime behavior, scripts, skills, or benchmark generated results. Do not manually edit generated benchmark output.

# Proof

Verify every command and version claim against the source at the exact base. Check local Markdown links. Run any documentation or lint check you add. Inspect the diff for promises that are not implemented.

# Delivery

Commit and push a short checkpoint branch. Open a draft PR against `main` with `~/.local/bin/gh-axi`. State what is still deliberately not claimed. Do not add agent co-authors.

# Stop condition

Stop after the draft PR is pushed and report its URL and exact head SHA.
