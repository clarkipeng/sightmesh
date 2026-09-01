# Final subscription hot-swap adversarial rereview

Date: 2026-08-18 America/Los_Angeles
Scope: exact-head, read-only review of SightMesh and cdesktop draft PRs.

## Exact-head / PR matrix

All nine PRs were independently checked with explicit repository targeting. Every PR was open, draft, and matched its required head: SightMesh C2 #19 `be40617b`, D #23 `fdf12e0c`, K #22 `fa2defe1`, L #24 `8bd82e7c`; cdesktop A1 #7 `c2a9c2ea`, B #10 `96960fbe`, E #6 `fa9600cf`, I #8 `6defea82`, J #9 `0ca04288e5cd988bf3a3776923715702ac87bd6d`.

## Findings

No source-level defect was confirmed. No narrow fix worker is required.

Evidence and release-gate caveats:

- B's 163 passed / 0 failed focused total is worker-reported, not independently rerun. Git/PR head, ancestry, cleanliness, and contract shape were independently verified.
- E has only independently observed `git diff --check`. `tsc` and `prettier` were unavailable (`spawn ENOENT`), web checks and formatting did not run, and its stacked non-`main` base receives no CI. B exposes no normalized-outcome read route at `96960fbe`; E truthfully omits outcome display.
- I's exact-head regression was independently rerun: 1 passed, 0 failed, 48 filtered.
- J's regression/check/format evidence is worker-reported. Its lost-callback/transient stale-running observation is an operational release concern, not a confirmed J patch defect.
- A1 and C2 focused evidence was not freshly rerun in this rereview. D's full suite was independently rerun 217/0; K's focused escalation/CLI suites 55/0; L's full suite 204/0.

## Attack verdicts

1. Concurrent claims, stale completion, and one terminal winner: **PASS at reviewed contract level**, with A1/B evidence caveats.
2. Restart recovery and durable `auto`/`ask`/`never` approval resume: **PASS**, with B's test provenance caveat.
3. Atomic rejected teammate spawn: **PASS**; independently rerun at I's exact head.
4. Retirement quarantine and successor race: **PASS** at D's exact head; independently rerun 217/0.
5. Parent escalation and intents: **PASS**. K durably records external launcher/fallback state. L classifies explicit `BLOCKED`/`DECISION` as interrupt/replace and routine status/completion as continue, with durable acknowledgment.
6. Secret leakage: **PASS for the reviewed contract**. Provider material is stripped before persistence, redacted in diagnostic surfaces, and opaque binding identifiers stay out of E's UI.
7. E API truthfulness: **PASS**. Approval list/respond shapes, optional reason, and durable states match B. E does not fabricate unavailable normalized outcomes.

## Escalation-intents verdict

L `8bd82e7c14c4b358da0cb1dfaa34417082500ae4` fixes the manager-5 auto-resume evidence and `.context/release-blocker-escalation-intents.md`: routine callbacks use `intent=continue` plus durable acknowledgment, while explicit blocker/decision messages retain replacement semantics. L must travel with K/D in release composition.

## Deferred release gates

- Re-establish independent B focused-test evidence when resources permit.
- Run E's unavailable TypeScript/format/web checks.
- Add and review a truthful backend outcome read route before claiming UI outcome coverage.
- Complete appropriate stacked full-suite/CI evidence without conflating reported and independently observed results.
- Resolve callback/stale-running operational evidence before release sign-off.

## Final recommendation

All exact implementation heads are reviewable while remaining draft. None should be marked ready or merged solely on this rereview; retain draft status until the evidence and integration gates above close.
