# cdesktop and Repowire reference

## Roles

- cdesktop owns process lifecycle, workspaces, worktrees, transcripts, and the human-visible UI.
- Repowire owns discovery and messaging across independently running agent sessions.
- Git and the PR host own source, branch, review, and merge truth.
- Handoff files own durable task-local context that must survive a session restart.

## cdesktop topology

`cdesktop team spawn` creates a visible teammate in the same workspace. Teammates share that workspace filesystem. Use this only for read-only work or explicitly disjoint paths.

`agent-deck spawn --worktree` creates a new cdesktop workspace and lets cdesktop create an isolated worktree. Use this for implementation or independently reviewed changes.

`agent-deck spawn --direct` attaches cdesktop to an existing checkout. Use it for migration only after verifying the exact path, current branch, dirty state, and authoritative owner. Never attach two writing agents to the same checkout.

## Session commands

Every cdesktop-launched executor receives `CDESKTOP_SESSION_ID`.

```sh
agent-deck teammate-list
agent-deck teammate-spawn --name reviewer --prompt-file prompt.txt
agent-deck message <session-id> --message-file follow-up.txt
```

The oldest session in a workspace is the lead. The CLI permits only that lead to spawn teammates.

## Repowire commands

```sh
repowire status
repowire peer list -a
repowire peer describe <name>
repowire peer ask <name> "question"
repowire trace <correlation-id>
```

Prefer Repowire's in-agent MCP tools when available. Use the CLI as a diagnostic and fallback surface. A new cdesktop session must appear online in `repowire peer list` before relying on cross-workspace delivery.

Target the online proxy identity created by agent-deck by matching repository path and backend. Repowire assigns its displayed name. The executor's own app-server hook may appear offline and is not the inbound route. If the proxy is not online, route the immediate message with `agent-deck message` so it becomes a visible follow-up in the target transcript. Do not claim Repowire delivery when the proxy is offline.

Existing workspaces are opted in explicitly:

```sh
agent-deck bridge-route <workspace-id> --enabled
```

`agent-deck spawn` enables its new workspace automatically. Archiving through `agent-deck close` disables the route.

## Safety

- Bind cdesktop and Repowire to loopback unless the user explicitly configures another trusted local network surface.
- Disable cdesktop analytics and relay for a local-only control plane.
- Do not place secrets in prompts, transcripts, handoff files, command arguments, or the orchestration repository.
- Do not rely on a beta application's automatic cleanup for imported worktrees. Reconcile and archive explicitly.
