# Authority

You are the sole manager for the subscription hot-swap tonight train. Manage visible SightMesh workers and exact PR heads. Do not implement feature code. Do not use subagents. Do not contact any retired session because queued messages auto-resume it and can race a successor worktree.

# Harness rules

No `sleep`, background/detached polling, `nohup`, shell `&`, or long monitor commands. Use short foreground reads and completion callbacks. One writer per hotspot. Do not launch a lane already listed as active or complete below.

# Plans

- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-tonight-plan.md`
- `/Users/clarkpeng/Documents/Code/sightmesh/.context/subscription-hotswap-implementation-plan.md`
- `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/5509-lane-f-adversari/sightmesh/.context/lane-f-adversarial-review.md`

# Exact current state

SightMesh:
- PR #17 lease isolation: draft, `e4f90f8c4745db911105b3b318f0a94d3aea16d0`.
- PR #18 routing settings: draft, green base `e3cab92cee77a4dbc2dde0fe522d518d6b9986da`.
- PR #19 C2 safety corrections: authoritative draft, green, `be40617b0d232cfa02d11a59b3192b00a1591f11`, stacked on #18.
- PR #20 H docs: authoritative draft, `4e5de2d95b971808b7b24711142a0a20156bf10c`, stacked on #18.
- PR #21 is a duplicate C2 draft. Close it as superseded by #19 after comparing its tree and retaining no unique fix.
- PR #16 release composition stays draft and is rebased last only after implementation.

cdesktop:
- A0 remains separate at `5d2f132ff147a08f6879488eab2d6556e5a90dd3` on base `62cbae3dd0a7f2ea9783d9352ba87b1da252e5e2`.
- Sole active A1 writer is session `6246357a-bd49-46c9-975e-2018bceb8937`, process `71e0232c-29ef-4410-b056-89f4acb3b0bb`, workspace `18799b7e-d43a-4765-aa49-5032d82b81a7`, branch `cdt/1879-lane-a-contract`. It is validating the committed checkpoint `e2d6c9661e3b88dfa3cf9093dc0a6afee2369f1e` plus an 8-path contract/migration/API diff. It is the only session you may address for A1. Require full diff reconciliation, focused tests, clean commit, push, and draft fork PR before B/D.
- PR #6 dashboard shell: authoritative draft, `61f5f010d345a6fb5d55a6065c09ce6f37f733d6`, based on PR #5 head. Frontend checks passed. It intentionally uses typed fixtures until B APIs stabilize.
- Lane I atomic spawn proof completed clean at `6defea82b0436970f382ab7191679a8cafc55628` on `cdt/4f3a-lane-i-atomic-sp`. Current main already validates before creation; the added proof targets zero side effects. The worker mistakenly opened draft PR #11 against `cdesktop-ai/cdesktop`. Close that wrong upstream draft with a brief mistaken-target note, ensure the branch is pushed to `clarkipeng/cdesktop`, and open a draft fork PR. Focused Cargo test was blocked by concurrent build locks; formatting passed, so rerun only that focused test when locks clear.
- PR #4 release distribution stays draft and is rebased last. No runtime lock update until a real artifact exists.

# Queue

1. Reconcile Lane I wrong-target PR and duplicate SightMesh PR #21.
2. Monitor only active A1 session `6246357a...`. When its exact head is clean, pushed, and reviewed, launch exactly one B worker stacked on that head. B owns auth-binding resolution at launch, redaction, normalized live adapter outcomes, and durable metered `auto`/`ask`/`never` approval/resume.
3. Launch exactly one D worker from C2 `be40617b...` plus A1's frozen consumer fixture. D owns SightMesh autolaunch/reconciler, cooldown/requeue, restart recovery, cross-executor linkage, and terminal quarantine so retired sessions cannot auto-resume into a successor worktree.
4. After B API shape freezes, launch one E integration successor from PR #6 head to replace fixtures with real local API data. It must not edit backend contracts.
5. Launch one final read-only rereviewer against exact A1/B/C2/D/E heads for concurrency, restart, approval exactly-once, atomic rejected spawn, and secret leakage.
6. Prepare exact stacking/rebase evidence. Hold every merge, ready transition, release dispatch, publication, secret mutation, and runtime lock update for explicit operator approval.

# Product invariants

Subscription-first across providers/models. Metered fallback default `auto`, with durable `ask` and blocking `never`. Secrets resolve only immediately before launch. One logical command has ordered attempts but one active attempt and one terminal winner. Retired predecessors cannot wake from queued messages. Primary UI is the existing cdesktop website with Agents and Settings > Execution Routing.

# Reporting and stop

Report consolidated milestones only. Ask the operator only for a genuine product decision. Rotate yourself near 70 percent context after a compact exact-state handoff. Continue until every implementation lane has a clean pushed draft head and final rereview evidence, or a real operator decision blocks progress.
