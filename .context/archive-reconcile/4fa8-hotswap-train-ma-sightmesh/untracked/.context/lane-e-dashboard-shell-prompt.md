# Lane E — cdesktop dashboard shell and fixtures

Start from exact cdesktop PR #5 head `41d37b261ada0d03b73e82cfd59d1fa39140a61b` only (remote branch `origin/cdt/13da-cdesktop-format`). The head is verified; its current GitHub checks show 3 passed / 4 failed. Do not alter, repair, revert, or reformat any PR #5 baseline files: report any baseline obstruction separately.

Own only cdesktop app navigation, a new Agents destination/view, Settings > Execution Routing UI, and local fixture/test data. The primary product surface is the existing cdesktop website. The standalone SightMesh pool page is compatibility/recovery only and is out of scope.

Use the frozen SightMesh settings contract from `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`: ordered subscription-first routes that may cross provider/model, metered fallback `auto`/`ask`/`never`, safe account aliases, cooldown/reset/retry state, approval-required and blocked states. Build a UI shell wired to typed fixtures only; do not invent or edit backend contracts, Rust database/schema code, provider/auth resolution, secret material, or the PR #5 formatter/frontend fixes.

First take a narrow read-only map of the existing navigation/settings conventions and fixture/test patterns. Implement one coherent fixture-backed shell checkpoint with focused frontend checks, commit and push it, open a draft PR, and report exact head, owned files, proof, and the explicit API seams still required from A1/B. Stop after that checkpoint.
