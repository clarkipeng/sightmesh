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
- completed-session stop and archive;
- dirty-repository retirement refusal;
- shared skill discovery through links in both Claude and Codex skill roots;
- Repowire local daemon, hooks, MCP installation, and peer registration.

## Current constraints

### Codex service tier

cdesktop 0.2.3 uses Codex app-server protocol types from Codex 0.121. An explicit `service_tier = "default"` in current Codex configuration causes cdesktop to reject the thread-start response because its older enum understands only `fast` and `flex`. Omitting the explicit default restores compatibility without selecting a different paid tier.

### Claude capacity

Claude Code launches through cdesktop, but the live end-to-end run on 2026-08-12 encountered the account's weekly Max limit. agent-deck treats this as a checkpoint and supported-profile failover event. It does not bypass the limit or manipulate subscription credentials.

### Repowire inbound push for cdesktop app-server sessions

Repowire registers cdesktop Codex app-server sessions and starts its MCP process, but the session's WebSocket inbound hook does not remain online. Direct `repowire peer ask` therefore rejects the offline peer. `agent-deck message` is the supported immediate cross-workspace transport because it creates a visible follow-up in the target cdesktop transcript.

A future Repowire proxy bridge may keep a durable peer online and translate asks into cdesktop follow-ups. Until that bridge can preserve correlation, response, and audit semantics, the skills must not claim Repowire push delivery for an offline cdesktop peer.

## Service activation boundary

The managed LaunchAgent is installed without starting it when an unmanaged cdesktop instance already exists. Close unmanaged instances before starting `io.agent-deck.cdesktop` so only one process owns cdesktop's local database and port file.
