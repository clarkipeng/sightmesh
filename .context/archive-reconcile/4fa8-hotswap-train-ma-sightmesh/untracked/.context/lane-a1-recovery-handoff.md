# Lane A1 recovery handoff

Source workspace: `18799b7e-d43a-4765-aa49-5032d82b81a7` (`lane-a-contract-2`), killed before checkpoint.

Base and branch: `5d2f132ff147a08f6879488eab2d6556e5a90dd3` on `cdt/1879-lane-a-contract`; base is pushed and remains the distinct A0 SQLx/clippy commit. No A1 commit or PR exists yet.

Preserved dirty patch (whitespace clean, do not discard):
- `crates/db/src/models/execution_process.rs`
- `crates/db/src/models/session_command.rs`
- `crates/executors/src/actions/mod.rs`
- `crates/local-deployment/src/container.rs`
- `crates/services/src/services/container.rs`

Prior visible evidence: `cargo test -p db session_command --lib` initially failed then passed; `cargo test -p executors storage_action_keeps_opaque_provider_ref_without_runtime_bindings --lib` was still compiling when the source session was killed. Previous stale WIP stash on a different branch remains explicitly excluded and must not be applied.

Objective: finish only the preserved A1 contract patch. Verify exact-once active-attempt guard and sanitized persisted executor action; ensure opaque provider/auth reference only and no launch secrets. Run focused tests, inspect diff scope, commit/push a draft stacked PR, and report an exact stable interface fixture. Do not add B/D scope or touch UI/auth provider resolution.
