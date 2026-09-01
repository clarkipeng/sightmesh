# Final subscription hot-swap adversarial rereview

You are the final fresh READ-ONLY adversarial reviewer. Do not implement fixes and do not edit any product/implementation file. Your only writable artifact is `.context/final-rereview.md` in your isolated SightMesh worktree.

## Exact heads (fail closed on any mismatch)

SightMesh (`/Users/clarkpeng/Documents/Code/sightmesh`, repo `clarkipeng/sightmesh`):

- C2 / draft PR #19: `be40617b0d232cfa02d11a59b3192b00a1591f11`.
- D / draft PR #23: `fdf12e0c6552d2dafd54b4e3893f6dd6a70b3ea2`.
- K / draft PR #22: `fa2defe148e06e3e2f6ba4df45dc3b5b7b973f0d`.
- L / draft PR #24: `8bd82e7c14c4b358da0cb1dfaa34417082500ae4`.

cdesktop (`/Users/clarkpeng/Documents/Code/cdesktop`, repo `clarkipeng/cdesktop`):

- A1 / draft PR #7: `c2a9c2eaacfdd4b2dea066c95793faf755b834be`.
- B / draft PR #10: `96960fbe4ab1ecc7feea22d6bc9b1ab7eee03a34`.
- E / draft PR #6: `fa9600cf34c67d89ff82287f76f1cd6cd35116ed`.
- I / draft PR #8: `6defea82b0436970f382ab7191679a8cafc55628`.
- J / draft PR #9: `0ca04288` (resolve and record full SHA, then require exact match).

Verify each PR is open, draft, and has the expected head before relying on it. Use explicit `--repo clarkipeng/<repo>` for GitHub calls. Do not push, open/close/edit/review PRs, dispatch workflows, merge, publish, mutate secrets, or update runtime locks.

## Review attacks

Inspect exact diffs and relevant tests/contracts across both repositories. Attack:

1. Concurrent reconciler/dispatcher claims, stale completion, one logical command / one active attempt / one terminal winner.
2. Restart recovery and durable resume, including approval `ask` exactly once and `auto`/`never` behavior.
3. Atomic rejected teammate spawn: no session/process/workspace side effects.
4. Retirement quarantine: queued or later delivery cannot auto-resume a superseded predecessor or race the successor worktree.
5. Parent escalation and escalation intents: Conductor/external launcher fallback, routine callbacks queue with `intent=continue`, blocker/decision interruption, durable acknowledgement. Explicitly confirm whether L `8bd82e7c` fixes the manager-5 auto-resume evidence in `.context/manager-5-resume-findings.md` and the blocker in `.context/release-blocker-escalation-intents.md`.
6. Secret leakage across persisted actions, serialization, Debug/Display, errors, logs, APIs, UI, snapshots, and transcripts. Treat auth binding identifiers as opaque and UI-hidden.
7. E API truthfulness: approval list/respond shapes and optional reason; no fabricated normalized outcome display where B has no read route.

## Evidence discipline

- Lane B's 163 passed / 0 failed focused counts are worker-reported. Git/PR state was independently verified, but tests were NOT independently rerun. Weigh and label this honestly.
- Lane E: only `git diff --check` passed. `tsc`/`prettier` were unavailable (`spawn ENOENT`); web checks/format did not run. PR #6 has no CI because its stacked base is not `main` and `test.yml` restricts pull requests to `main`.
- Lane I was independently rerun at exact head: 1 passed, 0 failed, 48 filtered.
- Preserve every other lane's worker-reported versus independently rerun distinction from `.context/lane-*-reconciliation.md` and PR evidence.
- You may run narrow read-only checks if they are practical and build locks/resources are clear. Do not claim a check you did not observe finish. Do not run broad cold builds merely to manufacture confidence.

## Findings artifact

Write `.context/final-rereview.md` with:

- exact-head/PR matrix and evidence provenance;
- findings ordered by severity, each with exact file/line or diff evidence and affected invariant;
- explicit verdict for every attack category above;
- explicit escalation-intents/L verdict;
- explicit honest-evidence caveats;
- confirmed defects that require a narrow fix worker, separated from gaps/deferred release gates and non-blocking observations;
- final recommendation: whether implementation heads are reviewable while remaining draft.

If you find a confirmed defect, do not fix it. Report the narrowest owner/path and stop. When complete, send the parent the findings path and concise verdict, then stop.
