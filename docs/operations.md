# Operations gates

## Workspace ownership leases

Use `agent-deck lease acquire --owner <name> --repo <path>` before taking recovery or migration ownership of a repository. Add `--worktree <path>` when the active checkout is a worktree. Leases are local JSON records under `~/.local/state/agent-deck/leases`, written under an atomic directory lock.

Lease acquisition fails closed when a live lease already controls the same repository or worktree. Expired leases and leases owned by dead local PIDs are recoverable with `agent-deck lease recover-stale`; acquisition also prunes stale records before deciding conflicts.

Use `agent-deck lease list --json` for inspection and `agent-deck lease release <token>` when ownership is handed off.

## Recovery smoke boundary

`scripts/recovery-smoke.sh` defaults to dry-run mode. With `DRY_RUN=0`, it starts, stops, and restarts only a task-owned disposable Python HTTP server bound to `127.0.0.1`. It never stops Conductor workers, provider sessions, cdesktop sessions, or unmanaged launchd labels.

Managed service operations remain scoped to the explicit agent-deck labels `io.agent-deck.cdesktop` and `io.agent-deck.bridge`.
