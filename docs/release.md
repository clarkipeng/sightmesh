# Release checks

Before publishing:

1. Run `pytest`.
2. Run `scripts/recovery-smoke.sh` and only set `DRY_RUN=0` when a disposable local restart is intended.
3. Run `scripts/package-smoke.sh`.
4. Inspect the generated `dist/` artifacts and keep the exact commit SHA in the release notes.

`scripts/package-smoke.sh` builds standard source and wheel artifacts with `python -m build`, installs the wheel into an isolated virtual environment, verifies the `agent-deck` entry point imports, and runs `twine check`.

The compatibility workflow validates Python 3.11 through 3.13 and runs package smoke on each supported version. The package provenance job uses `actions/attest@v4` with `contents: read`, `id-token: write`, and `attestations: write`, and attests the built wheel and sdist subject paths under `dist/`.

Public binary releases require signing with an available project-approved identity. This repository does not declare a signing identity, so unsigned public binary distribution is blocked until the maintainer supplies one and documents the signing command and verification step.
