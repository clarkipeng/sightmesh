# Contributing

SightMesh welcomes focused reports and small changes that preserve native ownership: cdesktop owns sessions and transcripts, Git owns worktrees and source, `.context` owns workspace-local handoffs, and Repowire owns cross-workspace contact.

Before opening a pull request:

1. Discuss substantial behavior changes in an issue.
2. Keep credentials, transcripts, machine paths, and generated benchmark results out of commits.
3. Add or update tests for behavior changes and document user-visible limitations.
4. Run `pytest` and `./scripts/package-smoke.sh`; use the narrower relevant test while iterating.
5. Keep the pull request scoped, explain the threat boundary, and state what is deliberately not claimed.

Prefer a small invariant over provider-specific recovery branches. Do not add credential extraction, auth-header replay, rate-limit evasion, a global transcript mirror, or a second owner for state already held by cdesktop, Git, `.context`, or Repowire.

By submitting a contribution, you agree that it is licensed under the repository's Apache-2.0 license.
