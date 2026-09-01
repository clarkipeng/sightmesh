You now own cdesktop's public GitHub Release distribution path in the existing isolated worktree.

Authority and base: start from exact clean cdesktop `origin/main` SHA `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2` on branch `cdt/6d94-cdesktop-026-rel`. Read and follow cdesktop AGENTS.md. The primary checkout is dirty and must remain untouched.

Objective: remove the mandatory private R2 dependency from the cdesktop prerelease path. A successful prerelease must be self-contained in GitHub Release assets, and the packaged CLI must download its exact pinned binaries from that release.

Required behavior:

1. Define one flat, deterministic release asset layout for every supported platform and binary plus a checksum manifest and the npm tarball. Prefer names such as `cdesktop-PLATFORM.zip`, `cdesktop-mcp-PLATFORM.zip`, `cdesktop-review-PLATFORM.zip`, `manifest.json`, and the existing package tarball.
2. Refactor `npx-cli/src/download.ts` and its tests so the injected default base is the exact GitHub release directory for the packaged tag. Preserve an explicit environment override for private mirrors, but define it as the exact release directory and validate all downloads through the manifest checksum/size. Remove R2-specific names and path assumptions from the active local CLI path.
3. Refactor `.github/workflows/pre-release.yml` so it downloads the already-built platform artifacts, flattens them into the release layout, generates the manifest, builds the npm tarball with the exact release base/tag, and creates the GitHub prerelease with all required assets. Release creation must not require R2 secrets.
4. Remove or clearly retire dead R2 upload/promotion steps for this local cdesktop release line. Do not change unrelated remote/Tauri distribution.
5. Add focused downloader and workflow-contract tests that catch filename, URL, checksum, manifest, and asset-list drift. Update the narrow owning docs.
6. Run focused TypeScript tests/typecheck, workflow/YAML validation, package dry-run/smoke, formatting on changed files, and repository-required checks that can catch this change. Do not keep unrelated formatter output or generated-file edits.
7. Commit, push, and open a draft PR against `main` with `~/.local/bin/gh-axi`. Do not mark ready or merge.

Exclusions: do not publish a release, dispatch workflows, set/read credentials, change package ownership, modify the user's primary checkout, or broaden into unrelated cdesktop errors.

Stop condition: the draft PR proves a secret-free GitHub prerelease can contain every binary and the exact package downloader can verify and fetch those assets. Report exact head, PR, asset contract, checks, and any remaining release-only approval.
