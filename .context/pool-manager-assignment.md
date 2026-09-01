# Assignment: land the credential pool

## Objective

Get `feat/credential-pool` rebased, green, pushed, and opened as a PR against `main`.

## State

Branch `feat/credential-pool` is one commit (`faef46a`) ahead of local `main`.
Local `main` is **3 commits behind `origin/main`** - the branch has not been rebased onto current origin.
The commit adds `src/sightmesh/pool/`, wires `sightmesh pool <action>` into `cli.py`, lifts the
ambient-consumer restriction in `profiles.py`, and reverses the account-rotation prohibition across
AGENTS.md, CLAUDE.md, README.md, docs/, and the reconcile-agent-work skill.

At the time it was written it was green: 139 tests on Python 3.11 and 3.13, ruff clean,
`scripts/package-smoke.sh` passing with the wheel containing `sightmesh/pool/ui.html`.
That was **before** the rebase, so it must be re-proven after.

## Scope

1. Rebase `feat/credential-pool` onto `origin/main`. Resolve conflicts. The three new origin
   commits may touch `cli.py` or `profiles.py`.
2. Re-run the full proof after rebasing: the test suite on both Python versions the repo supports,
   ruff check and format, and `scripts/package-smoke.sh`. Proof must be run by whoever owns the
   change, not inferred from the earlier run.
3. Verify docs and code agree. The docs now describe a credential pool as an ordered list of
   operator-owned accounts tried until one has quota. Confirm `profiles.py` and `cmd_failover`
   actually permit that, and that no remaining doc contradicts it.
4. Push the branch and open a PR against `main`. Body should state what the feature does, the
   policy reversal it carries and why, and the proof you ran.

## Delegation

Use `sightmesh spawn` with `--profile codex-terra-high` for execution workers. Do not use hidden
subagents. Give each writing worker its own worktree. Parallelize only genuinely disjoint work -
the rebase must land before anything else touches the branch.

## Constraints

- Never push to `main`. Branch plus PR only.
- No agent names as commit co-authors.
- Never use the em dash. Plain dash only.
- Credentials live in `~/.config/agent-pool/`, never in the repo, and token values are never
  printed - only shapes (length, prefix, fingerprint).
- Do not weaken or delete a test to make the suite pass. `tests/test_profiles.py` intentionally
  asserts the reversed invariant.

## Stop condition

PR open against `main`, CI or local proof green, and the PR number reported. If the rebase produces
a conflict you cannot resolve without guessing intent, stop and report rather than guess.
