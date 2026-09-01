# Authority

You are the sole A1 validation and delivery successor in the existing cdesktop worktree. No implementation redesign and no subagents.

# Exact state

- Branch `cdt/1879-lane-a-contract`, clean head `e2d6c9661e3b88dfa3cf9093dc0a6afee2369f1e`.
- Parent A0 remains separate at `5d2f132ff147a08f6879488eab2d6556e5a90dd3`.
- A1 diff is limited to five Rust files. No push or PR yet.
- The predecessor stash is forbidden. Do not inspect, apply, drop, or alter it.
- `cargo fmt --check` reported formatting in changed files plus unrelated pre-existing files. Format only the five A1-owned files. Do not write unrelated paths.

# Work

1. Inspect the exact commit and run formatting only on the five owned Rust files. Commit formatting separately if it changes them.
2. Run focused tests for names present in the diff:
   - `cargo test -p db session_command --lib`
   - `cargo test -p db complete_running_attempt --lib`
   - `cargo test -p executors storage_action_keeps_opaque_provider_ref_without_runtime_bindings --lib`
   - discover only the narrow local-deployment/services test names from `git diff 5d2f132f..HEAD`, then run those filters.
3. Fix only proven failures in the five owned files. No broad full-workspace test or cold dependency reinstall.
4. Push `cdt/1879-lane-a-contract` and open a draft PR against cdesktop `main` if none exists.
5. Report exact pushed head, test results, PR URL, diff paths, and the A1 consumer contract for B/D.

# Stop

Stop at a clean pushed exact head and draft PR. No merge, ready transition, release, workflow dispatch, secret mutation, UI, auth resolver, approval backend, or SightMesh edits.
