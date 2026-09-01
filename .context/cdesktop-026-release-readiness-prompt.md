You own cdesktop 0.2.6 release-readiness preparation in an isolated SightMesh worktree.

Authority and base: work only from fetched `origin/main` at exact SHA `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`. Read and follow `/Users/clarkpeng/Documents/Code/cdesktop/AGENTS.md`. The primary checkout has unrelated dirty files and must not be touched.

Required work:

1. Prove whether the local tag `v0.2.6-20260816210919` points at the exact base and whether that exact source contains the durable session-command recovery APIs SightMesh expects.
2. Audit the release workflow, version metadata, package/install artifacts, checks, and release notes needed to publish 0.2.6 safely.
3. Run only focused checks that can catch regressions in the recovery contract plus the repository-required format/check gates. Use `pnpm run format` before completion if you change files.
4. Fix narrow release-readiness defects on your branch if found. If source changes are needed, push a checkpoint and open a draft PR with `~/.local/bin/gh-axi`; otherwise do not create an empty PR.
5. Write a concise durable report under this worktree's `.context` with exact SHAs, commands, results, remaining risks, and the exact operator action that would publish the release.

Exclusions: do not publish a GitHub release, push or move tags, merge, mark a PR ready, touch credentials, alter the running cdesktop service, or edit the user's primary checkout. Do not broaden into unrelated cdesktop cleanup.

Stop condition: exact-head readiness is proven with focused evidence, every narrow defect you can safely fix is checkpointed, and the remaining publish decision is isolated for explicit user approval. Then report the branch/head, PR if any, and report path.
