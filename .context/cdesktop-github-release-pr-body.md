## Summary

- replace the local CLI's mandatory R2 binary path with one exact GitHub Release asset directory
- publish a flat deterministic set of 18 platform/binary ZIPs, `manifest.json`, and the npm tarball from the prerelease workflow
- authenticate the binary manifest with a SHA-256 embedded in the npm package, then verify every cached or downloaded ZIP by size and SHA-256
- route every normal launch through the same authenticated archive provider, including an already cached ZIP
- keep private mirrors as location-only overrides that must serve the same authenticated manifest and assets
- include the authoritative root Apache license in the packed npm artifact
- leave the unrelated disabled Tauri promotion path unchanged

## Integrity chain

The workflow builds the flat binary manifest first, injects its digest and release tag/base into the CLI bundle, and then creates the npm tarball. The tarball is a sibling release asset and is intentionally excluded from the binary manifest to avoid a hash cycle. A downstream SightMesh runtime lock can therefore pin the tarball URL and SHA-256, which transitively pins the binary manifest and ZIP bytes.

## Checks

- `pnpm run check:npx-cli`
- `pnpm run test:release-contract`
- launcher regression proving a corrupt preexisting cache cannot bypass the archive provider
- `npm run build` in `npx-cli`
- `npm pack --dry-run --ignore-scripts` in `npx-cli`
- dedicated path-filtered `release-distribution-checks` job added to PR CI
- repository formatter run with unrelated mechanical output removed
- `git diff --check`

## Hold

This PR does not publish or dispatch a release. Keep it draft until the exact-head review and release approval are complete.
