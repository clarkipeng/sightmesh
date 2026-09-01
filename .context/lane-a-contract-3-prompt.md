# Authority

You are the sole successor for cdesktop Lane A1 in the existing dirty worktree. No other worker owns these files. Do not use subagents.

# Exact state

- Repo/worktree: `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/1879-lane-a-contract/cdesktop`
- Branch: `cdt/1879-lane-a-contract`
- Base/A0: `5d2f132ff147a08f6879488eab2d6556e5a90dd3`
- Preserved dirty paths, and only these paths are authorized:
  - `crates/db/src/models/execution_process.rs`
  - `crates/db/src/models/session_command.rs`
  - `crates/executors/src/actions/mod.rs`
  - `crates/local-deployment/src/container.rs`
  - `crates/services/src/services/container.rs`
- Prior worker reported `cargo test -p db session_command --lib` passed. It was killed while a cold executor test compiled. No cargo/rustc process remains.
- Do not inspect, apply, drop, or alter the predecessor stash.

# Required sequence

1. Read `git status`, `git diff --check`, and the existing diff. Do not redo repository discovery.
2. If the diff is internally coherent and `git diff --check` passes, commit it immediately as the A1 checkpoint before starting any long compile. Preserve A0 as its own prior commit.
3. Run only focused warm-cache checks that exercise the changed contract:
   - `cargo test -p db session_command --lib`
   - `cargo test -p db complete_running_attempt --lib`
   - `cargo test -p executors storage_action_keeps_opaque_provider_ref_without_runtime_bindings --lib`
   - the narrow local-deployment/services tests named in the diff
   - `cargo fmt --check` or the repository's equivalent on changed Rust files
4. Fix only proven regressions inside the five owned files. Amend or add a second focused commit, then push.
5. Open a draft stacked PR against cdesktop `main` if none exists. Report exact head, test results, diff paths, and the consumer contract for B/D.

# Invariants

One logical command may have ordered attempts but only one active attempt and one terminal winner. Stale predecessor completion must not close the successor. Persist only normalized safe outcome metadata and opaque auth-binding references. Runtime launch material and secrets must be stripped from persisted executor action. No UI, auth resolver, approval policy, SightMesh Python, release, merge, ready transition, workflow dispatch, or secret mutation.

# Stop

Stop only at a clean pushed exact head and draft PR, or a concrete failing test that cannot be fixed within the five owned files. Send completion to the manager with `sightmesh parent --message`.
