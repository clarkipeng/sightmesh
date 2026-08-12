# Operations gates

## Workspace ownership leases

Use `agent-deck lease acquire --owner <name> --repo <path>` before taking recovery or migration ownership of a repository. Add `--worktree <path>` when the active checkout is a worktree. Leases are local JSON records under `~/.local/state/agent-deck/leases`, written under an interprocess `fcntl.flock`.

Lease liveness is TTL-based, not CLI-PID-based. One-shot `agent-deck lease acquire` leases remain live until they expire or are explicitly renewed or released. Use `agent-deck lease renew <token>` to extend ownership and `agent-deck lease release <token>` when ownership is handed off.

Conflict rules preserve safe worktree parallelism:

- a direct-checkout lease conflicts with every live lease for the same repository;
- a worktree lease conflicts with a direct-checkout lease for the same repository;
- two worktree leases for the same repository can coexist when their canonical worktree paths differ;
- two worktree leases conflict when their canonical worktree paths match.

Expired leases are recoverable with `agent-deck lease recover-stale`; acquisition also prunes stale records before deciding conflicts. Corrupt lease state fails closed until the invalid file is inspected and repaired or removed.

`agent-deck spawn` is lease-gated. Direct checkout spawn acquires before starting the workspace and attaches the returned workspace/session IDs afterward. Worktree spawn first refuses when a direct lease controls the repository, then acquires the specific returned worktree path after cdesktop reports the container. `agent-deck close --archive --confirm-reconciled` releases only the archived workspace's persisted lease token after successful archive.

Use `agent-deck lease list --json` for inspection. Workspace-to-token mappings are stored separately under the lease state directory so archival can release only the owning workspace.

## Recovery smoke boundary

`scripts/recovery-smoke.sh` defaults to dry-run mode. With `DRY_RUN=0`, it creates a temporary `HOME`, fake disposable `cdesktop` and `agent-deck` executables, installs agent-deck LaunchAgent plists without starting launchd, and verifies lease workspace release inside that temporary state root. It never stops Conductor workers, provider sessions, cdesktop sessions, or unmanaged launchd labels.

Managed service operations remain scoped to the explicit agent-deck labels `io.agent-deck.cdesktop` and `io.agent-deck.bridge`.
