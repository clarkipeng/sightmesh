# Lane H — docs and release integration

Start from the frozen public routing-schema checkpoint `e3cab92cee77a4dbc2dde0fe522d518d6b9986da` (PR #18 / `origin/cdt/5709-lane-c-settings`). C2 corrections do not change the public settings shape, so document this schema now.

Own README/docs/examples/upgrade/security/compatibility and release integration text only. Do not edit Python runtime, CLI, tests, pool/auth implementation, UI, package/runtime locks, or cdesktop source. Do not publish, dispatch, merge, or mark any PR ready.

Document subscription-first cross-provider/model routing, metered `auto`/`ask`/`never`, durable approval expectation, authoritative owned-auth inventory, safe alias visibility, no-secret orchestration state, compatibility-only standalone pool UI, and current implementation/release boundaries. Examples must be secret-free and match the frozen schema exactly. Add focused link/example validation, commit and push a draft PR, then report exact head, checks, and merge order to `sightmesh parent --message` and stop.
