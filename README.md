# agent-deck

Local orchestration for full Claude Code and Codex workers that remain visible in cdesktop and communicate through Repowire.

The project enforces one core rule: delegated work runs as a first-class cdesktop session, never as a native hidden subagent. Implementation workers receive isolated worktrees. Read-only reviewers may share a workspace as cdesktop teammates. Repowire connects workers across workspaces.

See [docs/compatibility.md](docs/compatibility.md) for the exact tested versions and current Repowire app-server limitation.

## Install

Prerequisites:

- Python 3.11 or newer
- `uv`
- `cdesktop`
- `repowire`
- at least one authenticated supported agent CLI

For a fresh machine, run:

```sh
./scripts/bootstrap-local.sh
```

For a machine that already has cdesktop and Repowire, run:

```sh
./scripts/install-local.sh
agent-deck doctor
agent-deck service install
agent-deck configure
agent-deck service open
```

The installer links the canonical skills into both `~/.claude/skills` and `~/.codex/skills`. It does not copy, inspect, or alter model-provider credentials.

## Core commands

```sh
agent-deck list
agent-deck spawn --name worker-name --repo /path/to/repo \
  --base origin/main --executor CODEX --prompt-file prompt.txt --worktree
agent-deck message SESSION_ID --message-file follow-up.txt
agent-deck close WORKSPACE_ID --message-file closeout.txt
agent-deck close WORKSPACE_ID --archive --confirm-reconciled
```

Archive refuses dirty repositories by default. Use `--preserve-dirty` only after the dirty paths are recorded in a durable handoff and explicitly assigned or deferred.

`agent-deck service` owns a macOS LaunchAgent at `io.agent-deck.cdesktop`, binds cdesktop to `127.0.0.1:3210`, disables automatic worktree cleanup, restarts it after failure, and keeps logs under `~/.local/state/agent-deck`. It manages only that labeled service and never kills unrelated cdesktop processes.

`agent-deck configure` preserves the existing cdesktop configuration while forcing analytics and relay off and placing managed worktrees under `~/.local/share/agent-deck/.cdesktop-workspaces` after the next cdesktop restart.

Set `AGENT_DECK_CDESKTOP_URL` to an exact local backend URL when more than one cdesktop process is running. Otherwise the CLI reads cdesktop's local port file.

## Capacity and credentials

The project supports compliant failover between provider profiles that the user has explicitly configured through supported vendor mechanisms. It does not extract auth headers, copy cookies or tokens, rotate consumer subscriptions, or evade rate limits. On exhaustion, checkpoint the worker, stop new requests, and resume through an explicitly authorized profile with the handoff recorded.
