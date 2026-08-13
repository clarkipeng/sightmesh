# Compatibility and tested behavior

## Tested local stack

- macOS 26.5.1 arm64
- cdesktop 0.2.3 and local fork package 0.2.3-sightmesh.1
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
- verified workspace-scoped interrupt followed by same-session resume with inherited executor configuration;
- native cdesktop workspace rename without changing its Git branch;
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
- worktree-disabled active migration import without starting an agent, catalog-only archived migration, explicit archive materialization, and empty-workspace rollback.

The migration planner was also run read-only against the local Conductor installation on 2026-08-12. It found 365 database and filesystem workspace/context records. Two were blocked because their Conductor sessions were active; all other records had an in-place source or were eligible for a private transcript-only handoff. No real Conductor workspace was migrated during this validation. A disposable direct workspace completed an archive, restore, second archive, and delete lifecycle smoke without launching a model.

The isolated bridge run on 2026-08-12 proved both transports. A blocking `repowire peer ask` returned `REPOWIRE_VISIBLE_OK`. A normal non-blocking ask returned `PLAIN_REPLY_OK`, and its trace recorded `created`, `resolved_peer`, `routed`, `hook_received`, `pane_injected`, `acked`, and `closed`. Restarting the foreground bridge reclaimed the same Repowire peer ID.

## Current constraints

### Codex protocol boundary

Upstream cdesktop 0.2.3 uses Codex app-server protocol types from Codex 0.121. An explicit `service_tier = "default"` in current Codex configuration causes that build to reject the thread-start response because its older enum understands only `fast` and `flex`.

The SightMesh fork updates both the live app-server boundary and event-log normalizer to Codex protocol 0.147. Service tier is represented as an open string, current server requests are handled, new event fields are tolerated, and image and audio tool outputs remain visible. Fork version `0.2.3-sightmesh.2` also adds the bounded maintenance drain required by SightMesh's staged updater. The drain rejects new mutations for at most 30 seconds while leaving reads and existing executor streams intact.

### Codex 5.6 model selection

The fork's picker includes the GPT-5.6 family and accepts an exact free-form model ID when a newly released or provider-specific model is not yet listed. Codex and Claude selections expose `low`, `medium`, `high`, `xhigh`, and `max`; SightMesh validates and forwards the same set instead of silently downgrading it.

### Claude capacity

An initial live run on 2026-08-12 encountered the account's weekly Max limit. After the provider's stated reset, a fresh visible Claude Code workspace completed successfully with `CLAUDE_SIGHTMESH_OK`, and a blocking Repowire request returned `CLAUDE_REPOWIRE_OK`. SightMesh treats future capacity boundaries as checkpointed supported-profile failover events. It does not bypass limits or manipulate subscription credentials.

### Repowire app-server hook replacement

Repowire registers cdesktop Codex app-server sessions and starts its MCP process, but the session's WebSocket inbound hook does not remain online. Direct `repowire peer ask` therefore rejects the offline peer. `sightmesh message` is the supported immediate cross-workspace transport because it creates a visible follow-up in the target cdesktop transcript.

The SightMesh bridge provides a separate durable proxy peer for every session in an explicitly enabled workspace. Repowire asks to that proxy become cdesktop follow-ups, and `sightmesh bridge-reply` closes the original correlation. Repowire assigns the displayed name from its own session mapper, so identify the online proxy by its repository path, backend, and peer ID in `repowire peer list -a` or `repowire peer describe`. Do not target the offline app-server hook identity.

The bridge is local and opt-in. It does not inspect or migrate message routing for existing cdesktop workspaces until their workspace IDs are enabled. Workspaces created through `sightmesh spawn` are enabled automatically unless `--no-bridge` is passed. Ownership leases are still reconciled for every active workspace.

## Service activation boundary

The managed LaunchAgents are installed without starting them when an unmanaged cdesktop instance already exists. Close unmanaged instances before starting `io.sightmesh.cdesktop` and `io.sightmesh.bridge` so only one process owns cdesktop's local database and port file. `sightmesh service cutover` handles the former owned labels with health-checked rollback.

### Approval compatibility

Upstream cdesktop 0.2.3 does not surface every approval and MCP elicitation flow from Codex CLI 0.147.0 reliably. The SightMesh fork adds a native pending-approval snapshot API and rejects a response whose execution-process ID does not match the pending request. SightMesh records every decision attempt locally and permits only a lead session or the local human to decide another session's plan.

The websocket stream remains as a compatibility fallback for an upstream cdesktop process. `--unattended` still requires an isolated worktree and maps to bypass permissions. Direct checkouts cannot use unattended mode.
