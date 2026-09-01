You continue owning SightMesh draft PR #16 from exact head `6e0bf793c982c251a6c489a65d5edc57c11ce7f4` on branch `cdt/0c5b-release-candidat`.

Objective: make cdesktop an explicit pinned runtime dependency without vendoring or a Git submodule. Add one authoritative machine-readable runtime lock and derive every pin/compatibility consumer from it.

Required behavior:

1. Add one small versioned runtime lock owned by SightMesh. It must contain cdesktop repository identity, current released version/tag, exact package asset URL, SHA-256, and the minimum/general plus durable-recovery compatibility versions. Use the currently released 0.2.5 artifact until a new 0.2.6 prerelease actually exists.
2. Make the lock available both from a repository checkout and an installed wheel. Add a small typed loader/validator with fail-closed errors for malformed or unsupported schema data.
3. Replace hardcoded cdesktop pin/version constants in bootstrap, CLI compatibility, durable recovery, updater defaults, and owning tests with the lock. Derive selection from this authoritative file rather than adding mirrors or a generated source copy.
4. Verify the package artifact SHA-256 before installation/staging. Preserve explicit local package/path override behavior for development, but require caller-supplied verification semantics or an explicit local-development mode rather than silently weakening the pinned path.
5. Remove copied version text from public docs where possible and link readers to the runtime lock. Add a short dependency/release contract: cdesktop releases first, then SightMesh updates the lock in one reviewed change; no submodule or vendored checkout.
6. Add focused schema, wheel-resource, checksum success/failure, bootstrap derivation, compatibility-version, and stale-hardcoded-pin tests. Add a non-destructive compatibility CI surface for the pinned tag and cdesktop main if it can be truthful without private credentials; keep main-edge failure non-blocking and avoid pretending mocked API tests are live binary compatibility.
7. Run focused tests, full suite, Ruff, package smoke, shell checks, Markdown links, diff check, and secret-pattern review. Commit, push, and update draft PR #16. Keep it draft.

Coordinate with the cdesktop distribution owner only through queued messages. Its future GitHub asset layout must be representable by this lock, but do not pin a nonexistent 0.2.6 artifact.

Exclusions: no Git submodule, vendored cdesktop source, new persistence, GitHub release, workflow dispatch, merge, ready transition, or invented capabilities.

Stop condition: a clean exact-head draft makes the current 0.2.5 runtime dependency explicit and verified, and updating to the future 0.2.6 release is a single lock change with no duplicated pins.
