You own an independent exact-head review of the SightMesh draft release candidate after its live overview hardening.

Authority: review only the exact candidate head supplied by the launcher. Read SightMesh AGENTS.md and the visible-agent/reconciliation skills. Do not modify source.

Required checks:

1. Diff the supplied exact head against prior reviewed head `f827f425a024813e8f29d39e24420fbc81fe1838` and review every changed line for correctness, privacy, performance, and schema truthfulness.
2. Prove `overview` selects one latest eligible process per visible session, always retains active cards, defaults inactive cards to 24 hours, honors explicit `--since`, and does not snapshot filtered historical processes.
3. Against the live local fleet, report only aggregate counts and populated-field counts. Prove stale failures are gone, model/provider come from native cdesktop facts, provider IDs are not mislabeled as subscription accounts, token/context data has direct normalized-snapshot provenance, and account/quota/cost remain null when unknown.
4. Run focused overview/fleet tests, Ruff, `git diff --check`, and a secret-pattern review of ordinary human and JSON output. Do not print any raw secret or lease token.
5. Record a PASS or blocking findings with exact file/line and reproduction in `.context/release-candidate-live-review.md`, then report to the launcher.

Exclusions: no source edits, commits, pushes, GitHub mutations, cdesktop service changes, merges, or ready transitions.

Stop condition: exact-head evidence supports PASS, or every blocking issue is isolated with a minimal reproduction.
