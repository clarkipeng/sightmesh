# SightMesh experimental release candidate handoff

## Objective

Execute the release-readiness plan through SightMesh's own visible workers, independently review every lane, compose one candidate, and leave all public changes draft pending explicit release approval and the operating proof gate.

## Base

- Repository: `/Users/clarkpeng/Documents/Code/sightmesh`
- Base: `origin/main`
- Base SHA: `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`

## Source lanes

### Trust and install hardening

- Workspace: `f9c521a6-a6e4-44b7-bd68-e7861ac492d6`
- Session: `b8791b23-d5a9-4790-81b3-52abdbd05001`
- Branch: `cdt/f9c5-release-trust-ha`
- HEAD: `e91319f9d2103bb5670e6b748b891776c4685007`
- Draft PR: https://github.com/clarkipeng/sightmesh/pull/13
- Delivered: public lease projection, Agent Deck uninstall removal, contained uninstall, loopback pool-server hardening, focused tests.
- Validation: 194 full-suite tests at final owner checkpoint; exact-head GitHub compatibility checks passed.

### Fleet visibility and usage

- Workspace: `e8379ddb-97d7-46a3-bbcf-32a91db89e4f`
- Session: `0f6372ad-4d9c-4361-be9b-920969485cd5`
- Branch: `cdt/e837-fleet-visibility`
- HEAD: `9429f216897a0b4e0ba43d79894b33432ca894d6`
- Draft PR: https://github.com/clarkipeng/sightmesh/pull/12
- Delivered: pure fleet projection, deterministic attention groups and selectors, allowlisted public serialization, provenance-required token/cost facts.
- Validation: focused tests and exact-head GitHub compatibility checks passed.

### Durable manager wake and delivery

- Workspace: `a090b2d1-2ac6-44ad-8416-76ebbd7f6f45`
- Session: `af4fd50e-b5ce-42e0-ad9c-4fe514757763`
- Branch: `cdt/a090-durable-manager`
- HEAD: `a3cc55e471df27dc58ea7038dac35ce1d9e6d0e1`
- Draft PR: https://github.com/clarkipeng/sightmesh/pull/15
- Delivered: native command lifecycle projection, restart-safe parent wake dedupe, stream-death recovery, unknown-stop reconciliation that waits for terminal evidence.
- Validation: focused tests, 186 full-suite tests, and exact-head GitHub compatibility checks passed.
- Boundary: active durable recovery requires cdesktop 0.2.6.

### Public repository and docs

- Workspace: `eb4f8542-e43e-47bc-8652-2d4f57cc1100`
- Session: `fdb34ac7-225e-49bf-8a10-fb5fc3647050`
- Branch: `cdt/eb4f-public-repo-poli`
- HEAD: `333df45194af6759df63f06b3a690cd1c884e1f0`
- Draft PR: https://github.com/clarkipeng/sightmesh/pull/14
- Delivered: experimental README, pool-first quickstart, docs map, security/contribution/support policies, public architecture, issue/PR/release templates.
- Validation: 183 full-suite tests, package smoke, Markdown links, YAML parsing, and exact-head GitHub compatibility checks passed.

## Independent review

- Workspace: `9d925979-c29c-4f5b-97b5-f86e9a0a0c0a`
- Session: `042231f0-c7e3-40a2-99f2-11f40ad44cef`
- Reviewed every source head and the composed candidate.
- Found and caused fixes for fleet secret projection and invented provenance, probe-output leakage, uninstall ownership, HTTP 424 duplicate requeue, inaccurate docs claims, and migration report token leakage.
- Final exact-head verdict on the candidate: pass.
- Live visibility review workspace `30c5d07e-cb1e-4bbe-9338-5d9abb7d593b` found that the shared latest-process helper trusted native row order. The live fleet had 12 out-of-order session lists and 3 incorrect selections. The integration owner fixed the root invariant, and the same reviewer passed exact head `6e0bf793c982c251a6c489a65d5edc57c11ce7f4` with 12 out-of-order lists and 0 selection mismatches.
- The blocking report is preserved at `.context/release-candidate-live-review.md`; the rereview closeout records the superseding PASS.

## Integrated candidate

- Workspace: `0c5becee-d2c6-441a-8501-f28df468cb77`
- Session: `c61ded1d-fe5c-4dcb-8e19-551c23773ca0`
- Branch: `cdt/0c5b-release-candidat`
- HEAD: `6e0bf793c982c251a6c489a65d5edc57c11ce7f4`
- Upstream: `origin/cdt/0c5b-release-candidat`, synchronized
- Draft PR: https://github.com/clarkipeng/sightmesh/pull/16
- Integrated: all four reviewed lanes, token-free normal lifecycle and migration output, explicit capability-only lease operations, an agent-centric `sightmesh overview`, and a bounded cdesktop 0.2.6 durable-recovery gate.
- Live overview now selects one latest eligible process per visible session, uses stable session selectors, defaults inactive cards to 24 hours, exposes only native model/provider/token/context facts, reports derived context pressure, keeps unknown account/quota/cost null, and treats native killed executions as terminal.
- Validation: 217 full-suite tests; focused integration, latest-process, migration, and redaction tests; Ruff; package smoke; Markdown links; GitHub YAML; diff and secret-output review.
- Exact-head CI run `32211712388`: success on Python 3.11, 3.12, and 3.13. PR checks report 6 of 6 passed.
- Worktree: clean, fully pushed, draft, unmerged.

## cdesktop 0.2.6 proof and release state

- Exact source/tag head: `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Disposable proof passed 7 session-command recovery tests, 7 replay-safe stop-operation tests, and 4 server response-mapping tests. The source-side durable recovery contract is sufficient and no cdesktop defect was found.
- The prior prerelease run built frontend, all six platform binaries, and all six platform packages successfully. It failed only before R2 upload because the repository has none of the five required R2 secrets, so no 0.2.6 GitHub prerelease exists.
- After secrets and explicit approval, dispatch the prerelease workflow from current main with `version_type=none`. It will mint a fresh timestamped 0.2.6 tag and rebuild the artifacts. SightMesh must pin that new prerelease. Full release and npm publication remain a separate explicit decision.
- Durable proof and release readiness reports are preserved at `.context/cdesktop-026-durable-proof.md` and `.context/cdesktop-026-release-readiness.md`.

## Deferred scope and owner

- Configure or replace the cdesktop binary distribution backend, then create and bootstrap-pin a fresh 0.2.6 prerelease: cdesktop release owner plus explicit user infrastructure approval.
- Run durable manager wake and acknowledged delivery under real Catapult load for several continuous weeks without manual nudges or required watchdog scaffolding: SightMesh release manager.
- Decide whether to promote, mark ready, merge, and publish the candidate: user approval after the proof gate.
- Monetary cost remains optional externally supplied data with provenance. SightMesh does not infer provider prices.

## Next exact action

Keep PR #16 draft. Choose the cdesktop binary distribution path, create a fresh 0.2.6 prerelease, pin it in SightMesh, exercise the candidate under Catapult workload, record manual interventions and delivery outcomes, then request explicit user approval before readying or merging PR #16. Source PRs #12 through #15 remain draft evidence lanes.

## Retirement

All source, review, and integration branches are clean and fully pushed. Their cdesktop workspaces may be archived while retaining transcripts, branches, and PRs.
