# Lane P brief: updater stages the GitHub-assets distribution (issue #31)

Single writer. Read clarkipeng/sightmesh#31 first.

## Base
sightmesh `main` (must contain #34's squash). Verify before writing.

## Problem
cdesktop's release tgz no longer bundles platform zips; the npx CLI downloads them from release assets per `manifest.json`. `sightmesh update stage` still requires `node_modules/cdesktop/dist/<platform>/cdesktop.zip` and fails: "Staged cdesktop backend archive is missing".

## Scope
- In `stage()` (`src/sightmesh/updates.py`): when the platform archive is absent after package install, download `cdesktop-<platform>.zip` and `manifest.json` from the release matching the runtime lock tag, verify the zip's sha256 against the manifest before placing it, and fail closed on any mismatch or absence. Reuse the existing `_download`/`_sha256` helpers; no new dependencies.
- The manifest and asset URLs derive from the runtime lock's tag and repository - no hardcoded URLs.
- Owned paths: `src/sightmesh/updates.py`, `tests/test_updates.py` (create if missing), `docs/release.md` one-line note if user-visible behavior changes.

## Proof
Tests with why-docstrings: absent-archive path fetches+verifies (fake transport), sha mismatch fails closed without placing the file, bundled-archive path unchanged. Full suite green.

## Delivery (lane policy C)
PR to clarkipeng/sightmesh main (explicit --repo). Self-mark ready on green local gates; append STATUS to `.context/lane-p-status.md` AND `sightmesh parent --message`. Reviewer merges. No background processes; never message retired sessions.
