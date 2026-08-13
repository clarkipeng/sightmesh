# cdesktop and Repowire reference

## Roles

- cdesktop owns process lifecycle, workspaces, worktrees, transcripts, and the human-visible UI.
- Repowire owns discovery and messaging across independently running agent sessions.
- Git and the PR host own source, branch, review, and merge truth.
- Handoff files own durable task-local context that must survive a session restart.

`.context` is local to each workspace. It is not a global coordination database. Inspect sibling worktrees with normal filesystem and Git commands, inspect conversations in cdesktop, and contact their owners through Repowire. Do not mirror all worktrees or transcripts into a second knowledge system.

## cdesktop topology

`cdesktop team spawn` creates a visible teammate in the same workspace. Teammates share that workspace filesystem. Use this only for read-only work or explicitly disjoint paths.

`sightmesh spawn --worktree --unattended` creates a new cdesktop workspace and lets cdesktop create an isolated worktree. Use this for delegated implementation or independently reviewed changes. Omit `--unattended` when a human will supervise approval prompts.

`sightmesh spawn --direct` attaches cdesktop to an existing checkout. Use it for migration only after verifying the exact path, current branch, dirty state, and authoritative owner. Direct checkouts cannot be unattended. Never attach two writing agents to the same checkout.

## Session commands

Every cdesktop-launched executor receives `CDESKTOP_SESSION_ID`.

```sh
sightmesh teammate-list
sightmesh teammate-spawn --name reviewer --prompt-file prompt.txt
sightmesh message <session-id> --message-file follow-up.txt
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

Target the online proxy identity created by SightMesh by matching repository path and backend. Repowire assigns its displayed name. The executor's own app-server hook may appear offline and is not the inbound route. If the proxy is not online, route the immediate message with `sightmesh message` so it becomes a visible follow-up in the target transcript. Do not claim Repowire delivery when the proxy is offline.

Existing workspaces are opted in explicitly:

```sh
sightmesh bridge-route <workspace-id> --enabled
```

`sightmesh spawn` enables its new workspace automatically. `sightmesh workspace archive` disables the route and `sightmesh workspace restore` re-enables it.

## Safety

- Bind cdesktop and Repowire to loopback unless the user explicitly configures another trusted local network surface.
- Disable cdesktop analytics and relay for a local-only control plane.
- Do not place secrets in prompts, transcripts, handoff files, command arguments, or the orchestration repository.
- Reconcile before archiving. cdesktop's native cleanup may reclaim a clean archived managed worktree after about one hour, while imported direct workspaces remain outside cdesktop's filesystem ownership.
