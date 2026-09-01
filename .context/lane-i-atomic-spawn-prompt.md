# Authority

Own only the cdesktop regression proof that a rejected teammate spawn has zero durable side effects. Work in an isolated worktree, use no subagents, and keep the PR draft.

# Proven runtime reproduction

Against local cdesktop 0.2.5, a cross-executor teammate request without `selected_provider_id` returned HTTP 400 `EXECUTOR_REQUIRES_PROVIDER`, yet created session `76494346-3386-4e5d-97db-6864d8d3841d` and running process `97194369-055d-42c0-9124-046c036dff36`. The process was explicitly stopped before it wrote.

# Grounding

Current source in `crates/server/src/routes/teammates.rs` appears to validate provider/model before `Session::create`. Do not rewrite correct logic without a failing test. First determine whether `origin/main` already prevents the side effect and whether the gap is test coverage or a later launch path.

# Work

1. Add the narrowest backend integration/unit test that issues the rejected cross-executor request and asserts no session and no execution process were created.
2. If the test fails, fix the root ordering/transactionality before creation. If it passes on main, deliver the regression test only and document that the observed defect is in the installed 0.2.5 runtime and requires the final release artifact upgrade.
3. Own only teammate route test/support paths. Do not touch A1 execution/session command files, auth resolver, UI, release workflow, or generated files.
4. Run only the focused server/route test and formatting check that can catch this regression.
5. Push and open a draft PR against cdesktop `main`. Report exact SHA, paths, test command/result, and whether code changed or only proof was added.

# Stop

Stop at a clean pushed draft head. No merge, ready transition, release dispatch, secrets, or runtime lock updates.
