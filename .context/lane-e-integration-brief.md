# Lane E integration successor brief

You are the sole Lane E integration writer. Replace the typed fixture-backed execution-routing UI with real local cdesktop API data while preserving the existing Agents and Settings surfaces.

## Exact start and upstream contract

- Repository: `/Users/clarkpeng/Documents/Code/cdesktop`.
- Start from the exact implementation commit of draft PR #6: `61f5f010d345a6fb5d55a6065c09ce6f37f733d6`. Verify this is your initial HEAD before writing.
- PR #6's branch later gained docs-only handoff commit `ecc986b7`; do not treat that as product code. Before delivery, make your branch descend cleanly from current `origin/cdt/964b-lane-e-dashboard` so the integration PR stacks normally.
- Frozen Lane B contract: draft PR #10, branch `cdt/b514-lane-b-auth-appr`, exact head `96960fbe4ab1ecc7feea22d6bc9b1ab7eee03a34`, reconciliation `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/afb2-hotswap-train-ma/sightmesh/.context/lane-b-reconciliation.md`.
- Inspect Lane B read-only via Git (`git show 96960fbe:PATH`) or its clean worktree. Do not modify, merge, or push Lane B.

## Ownership and invariants

- Own frontend-only paths under `packages/local-web/**`, `packages/web-core/**`, and `packages/ui/**` as needed.
- Do not edit `crates/**`, migrations, backend contracts, `shared/types.ts`, runtime locks, workflows, or release files.
- Remove production dependence on `packages/web-core/src/shared/lib/execution-routing/fixtures.ts`; keep only narrowly useful test fixtures if tests require them.
- Consume real local endpoints and safe API fields, including `GET /metered-approvals`, `POST /metered-approvals/{id}/respond`, normalized execution outcomes, and session-message metered data. Follow existing frontend API/query/mutation conventions.
- Never display or log secrets, auth binding IDs, credential material, raw provider errors, or unsafe transcript content. Account aliases appear only when the API exposes a safe alias.
- Preserve subscription-first status vocabulary, approval exactly-once UX, blocked state, cooldown/reset/retry visibility, and compatibility-only status of the standalone pool page.

## Proof and delivery

- Add focused frontend tests for live loading, approval response, blocked/error states, and secret-safe projection where the existing test structure supports them.
- Run the narrow package checks for every touched frontend package, `git diff --check`, and formatting. Report exact test/check results and any baseline failure separately.
- Keep one writer and a clean pushed branch. Open one DRAFT PR with explicit `--repo clarkipeng/cdesktop`, stacked on `cdt/964b-lane-e-dashboard`. Never merge, mark ready, publish, dispatch workflows, mutate secrets, or update runtime locks.
- Report branch, exact head, draft PR number, changed-path scope, and exact checks to the parent, then stop.
