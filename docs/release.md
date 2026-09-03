# Release checks

Before publishing:

1. Run `pytest`.
2. Run `scripts/recovery-smoke.sh` and only set `DRY_RUN=0` when a disposable local restart is intended.
3. Run `scripts/package-smoke.sh`.
4. Inspect the generated `dist/` artifacts and keep the exact commit SHA in the release notes.

`scripts/package-smoke.sh` builds standard source and wheel artifacts with `python -m build`, installs the wheel into an isolated virtual environment, verifies the `sightmesh` entry point and packaged migration command, and runs `twine check`.

When the staged cdesktop package omits its platform backend archive, `sightmesh update stage` retrieves the matching locked release asset and verifies its manifest SHA-256 before staging it.

The compatibility workflow validates Python 3.11 through 3.13 and runs package smoke on each supported version. Tag releases build wheel and sdist artifacts, write SHA-256 checksums, use `actions/attest@v4` with GitHub OIDC provenance, and upload all artifacts to the matching GitHub release.

GitHub artifact attestations provide signed Sigstore provenance for the published release assets. Publishing to PyPI or another package index remains a separate maintainer decision and is not performed by the release workflow.

## Release contract (shared across repos)

The same shape for every repo we own:

1. Tag -> CI builds -> GitHub Release with checksummed artifacts and attestations. The GitHub Release is the canonical source; nothing installs from a branch.
2. Publish to the ecosystem registry as the install convenience: PyPI for sightmesh (`uv tool install sightmesh==<version>`), npm for cdesktop.
3. Installers pin an exact version and verify the checksum from the release manifest. Never "latest", never unverified.
4. Version bump PR -> tag -> verify assets -> install -> one live canary before unattended use.
