# Authority

The user asked SightMesh to execute and verify its release-readiness plan.

# Review targets

- PR #12, fleet model, exact head `f80ed2df288f6905fbaf88b441f358f89587a7e1`
- PR #13, trust hardening, exact head `e285644f4964f764004f621b748ca7671798855a`
- PR #15, durable manager wake, exact head `222a2c2bb7cba56d5ab890058c2cb6cb52cc7943`
- Common base `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`

# Objective

Perform an independent exact-head review. Do not edit files or push commits.

1. Verify each branch tree and PR diff against the common base.
2. Review PR #13 for capability redaction completeness, installer/uninstaller containment, symlink and path safety, loopback HTTP request limits, Origin/Host correctness, and secret-safe errors.
3. Review PR #15 for native ownership, restart-safe dedupe, parent wake correctness, no wake loops, stream-death recovery, cdesktop 0.2.5 versus 0.2.6 compatibility, and whether any claimed delivery state is unsupported.
4. Review PR #12 for the smallest robust projection, deterministic selectors, privacy-safe serialization, attention ordering, token/cost provenance, and whether it invents a second state owner.
5. Review how the three diffs compose. Call out any shared CLI integration or test-order issue.
6. Run only checks that can catch plausible regressions in the changed behavior.

# Delivery

Post concise blocking findings as PR review comments with `~/.local/bin/gh-axi` when supported. Otherwise provide exact file/line evidence in your final response. Do not approve, mark ready, merge, or modify PR state.

# Stop condition

Report a verdict for each PR, exact checks run, all blockers, non-blocking improvements, and the safe integration order. Stop with a clean worktree.
