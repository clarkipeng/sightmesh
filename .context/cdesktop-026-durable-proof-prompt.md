You own an independent, read-only durable-recovery proof for the SightMesh release candidate against cdesktop 0.2.6.

Authority and sources: your cdesktop worktree must start from `origin/main` at exact SHA `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`. SightMesh draft PR #16 was reviewed at `f827f425a024813e8f29d39e24420fbc81fe1838`. Read and follow cdesktop's AGENTS.md.

Required work:

1. Identify the exact native cdesktop recovery endpoints and invariants SightMesh 0.2.6 feature detection relies on.
2. Exercise the recovery behavior in a disposable isolated backend or the narrowest existing integration harness. Prove at minimum: completed requests are replay-safe; pending or interrupted requests recover once; unsupported or terminal cases do not create an endless wake loop; and a retry has a bounded observable outcome.
3. Do not modify the user's running cdesktop service, its state, or the primary checkout. Do not print credentials or raw lease tokens.
4. Record exact commands, outcomes, gaps, and whether this is sufficient for a release gate in `.context/cdesktop-026-durable-proof.md` in your worktree.
5. If you find a concrete defect, report it immediately to the launcher with exact file and reproduction. Do not overlap source edits with the separate release-readiness owner unless the launcher assigns ownership.

Exclusions: no source changes, GitHub mutations, publishing, tagging, merging, service restarts, or broad test runs unrelated to durable recovery.

Stop condition: a repeatable disposable proof exists with exact evidence, or one concrete blocker is isolated and reported with reproduction.
