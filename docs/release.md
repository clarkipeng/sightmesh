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

## Manager safety release train

Do not promote the durable-run and task-launch changes as one manager-safety
release until all of these independently reviewed inputs are present:

- SightMesh durable external-run subscriptions (issue #55 / PR #57);
- SightMesh task launch reservations and successor fencing (issue #65);
- a pinned cdesktop/executor release that enforces transcript/rollout byte and
  disk ceilings before writing, and exposes an explicit quota-stop outcome; and
- one end-to-end disposable test covering duplicate wakes, a manager crash,
  concurrent failover, successor self-target prevention, and bounded transcript
  and command growth.

Unit tests or an advisory free-space check do not satisfy the last two gates.
Quota enforcement belongs at the cdesktop/executor write boundary; SightMesh
must not copy transcript bytes or claim safety based on observing growth after
the write. Until the runtime lock names that released capability and the
end-to-end evidence is linked in the release notes, keep the train draft and
experimental.
