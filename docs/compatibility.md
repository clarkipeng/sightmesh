# Compatibility and tested behavior

## Tested local stack

- macOS 26.5.1 arm64
- cdesktop 0.2.3
- Repowire 0.17.0
- Codex CLI 0.147.0
- Claude Code 2.1.228

## Passing behavior

- local-only cdesktop configuration with analytics and relay disabled;
- cdesktop workspace launch against an existing disposable checkout;
- cdesktop-managed isolated worktree launch;
- full visible Codex teammate launch from a cdesktop parent;
- teammate roster discovery;
- visible cross-workspace follow-up by cdesktop session ID;
- native archive, restore, and confirmed archive deletion with Git branch preservation;
- clean managed-worktree reclamation after the native one-hour archive grace period;
- dirty managed-worktree retirement refusal and direct-repository preservation;
- launchd unload/reload recovery with bounded transient retry and definition rollback;
- owner-only SightMesh state, configuration, log, lease, delivery, and migration storage;
- shared skill discovery through links in both Claude and Codex skill roots;
- Repowire local daemon, hooks, MCP installation, and peer registration;
- opt-in Repowire proxy peer registration for cdesktop sessions;
- Repowire ask injection as a visible cdesktop follow-up;
- correlated plain-ask acknowledgement and structured-question response;
- bridge peer identity reuse across bridge reconnects.
- Repowire health-check rejection when its CLI exits successfully but reports a daemon error;
- worktree-disabled migration import without starting an agent, followed by empty-workspace rollback.

The migration planner was also run read-only against the local Conductor installation on 2026-08-12. It found 365 database and filesystem workspace/context records. Two were blocked because their Conductor sessions were active; all other records had an in-place source or were eligible for a private transcript-only handoff. No real Conductor workspace was migrated during this validation. A disposable direct workspace completed an archive, restore, second archive, and delete lifecycle smoke without launching a model.

The isolated bridge run on 2026-08-12 proved both transports. A blocking `repowire peer ask` returned `REPOWIRE_VISIBLE_OK`. A normal non-blocking ask returned `PLAIN_REPLY_OK`, and its trace recorded `created`, `resolved_peer`, `routed`, `hook_received`, `pane_injected`, `acked`, and `closed`. Restarting the foreground bridge reclaimed the same Repowire peer ID.

## Current constraints

### Codex service tier

cdesktop 0.2.3 uses Codex app-server protocol types from Codex 0.121. An explicit `service_tier = "default"` in current Codex configuration causes cdesktop to reject the thread-start response because its older enum understands only `fast` and `flex`. Omitting the explicit default restores compatibility without selecting a different paid tier.

### Claude capacity

An initial live run on 2026-08-12 encountered the account's weekly Max limit. After the provider's stated reset, a fresh visible Claude Code workspace completed successfully with `CLAUDE_SIGHTMESH_OK`, and a blocking Repowire request returned `CLAUDE_REPOWIRE_OK`. SightMesh treats future capacity boundaries as checkpointed supported-profile failover events. It does not bypass limits or manipulate subscription credentials.

### Repowire app-server hook replacement

Repowire registers cdesktop Codex app-server sessions and starts its MCP process, but the session's WebSocket inbound hook does not remain online. Direct `repowire peer ask` therefore rejects the offline peer. `sightmesh message` is the supported immediate cross-workspace transport because it creates a visible follow-up in the target cdesktop transcript.

The SightMesh bridge provides a separate durable proxy peer for every session in an explicitly enabled workspace. Repowire asks to that proxy become cdesktop follow-ups, and `sightmesh bridge-reply` closes the original correlation. Repowire assigns the displayed name from its own session mapper, so identify the online proxy by its repository path, backend, and peer ID in `repowire peer list -a` or `repowire peer describe`. Do not target the offline app-server hook identity.

The bridge is local and opt-in. It does not inspect or migrate message routing for existing cdesktop workspaces until their workspace IDs are enabled. Workspaces created through `sightmesh spawn` are enabled automatically unless `--no-bridge` is passed. Ownership leases are still reconciled for every active workspace.

## Service activation boundary

The managed LaunchAgents are installed without starting them when an unmanaged cdesktop instance already exists. Close unmanaged instances before starting `io.sightmesh.cdesktop` and `io.sightmesh.bridge` so only one process owns cdesktop's local database and port file. `sightmesh service cutover` handles the former owned labels with health-checked rollback.

### Codex approval compatibility

cdesktop `0.2.3` does not surface the approval and MCP elicitation flow from Codex CLI `0.147.0` reliably. Supervised sessions can stall at the first approval request. SightMesh therefore makes unattended execution explicit with `--unattended`, requires an isolated worktree, and maps that mode to cdesktop's bypass permission policy. Direct checkouts cannot use unattended mode.
