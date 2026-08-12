# SightMesh

SightMesh is a local reliability and policy layer for full Claude Code and Codex workers. It launches them as visible cdesktop sessions, gives implementation workers isolated git worktrees, and connects explicitly enabled sessions through Repowire.

Delegated work remains visible and interruptible in cdesktop. SightMesh does not replace cdesktop's UI or the agent CLIs. It adds durable delivery, ownership leases, safe closeout, local-only configuration, service recovery, and shared Claude/Codex skills.

See [compatibility](docs/compatibility.md), [operations](docs/operations.md), and the [source-derived competitive bake-off](docs/competitive-bakeoff.md) for tested versions, limitations, and alternatives.

## Install

Prerequisites:

- macOS
- Python 3.11 or newer
- `uv`
- `cdesktop`
- `repowire`
- at least one authenticated supported agent CLI

Fresh setup:

```sh
git clone https://github.com/clarkipeng/sightmesh.git
cd sightmesh
./scripts/bootstrap-local.sh
sightmesh doctor
sightmesh service install --no-start
sightmesh configure
sightmesh service start
sightmesh service open
```

To cut over an existing installation of the former project name, install SightMesh, run `sightmesh configure`, then run `sightmesh service cutover`. Cutover backs up the old LaunchAgent definitions, stops only the two old owned labels, migrates routing and delivery state, starts the new labels, checks health, and rolls back if startup fails.

The installer links the canonical skills into both `~/.claude/skills` and `~/.codex/skills`. It does not copy, inspect, or alter model-provider credentials.

## Core workflow

Supervised worker:

```sh
sightmesh spawn --name worker-name --repo /path/to/repo \
  --base main --executor CODEX --prompt-file prompt.txt --worktree
```

Unattended worker in an isolated worktree:

```sh
sightmesh spawn --name worker-name --repo /path/to/repo \
  --base main --executor CODEX --prompt-file prompt.txt --worktree --unattended
```

`--unattended` is deliberately worktree-only and selects cdesktop's bypass permission policy. Direct checkouts remain supervised. This boundary is required by the tested cdesktop `0.2.3` and Codex CLI `0.147.0` approval behavior.

Other lifecycle commands:

```sh
sightmesh status
sightmesh list
sightmesh message SESSION_ID --message-file follow-up.txt
sightmesh prompt-idle SESSION_ID --message-file follow-up.txt
sightmesh bridge-route WORKSPACE_ID --enabled
sightmesh close WORKSPACE_ID --message-file closeout.txt
sightmesh close WORKSPACE_ID --archive --confirm-reconciled
sightmesh delivery status
sightmesh lease list
```

Archive refuses dirty repositories by default. `--preserve-dirty` is available only for explicitly reconciled state. Spawn acquires an expiring ownership lease; isolated worktrees from the same repository may coexist, while direct-checkout ownership conflicts fail closed.

## Local architecture

```text
Claude Code CLI ─┐
                 ├─ cdesktop visible sessions and worktrees
Codex CLI ───────┘            │
                              ├─ SightMesh lifecycle, leases, delivery queue
                              │
                              └─ Repowire local request/reply mesh
```

The managed LaunchAgents are `io.sightmesh.cdesktop` and `io.sightmesh.bridge`. cdesktop binds to `127.0.0.1:3210`; analytics and relay are disabled; managed worktrees default to `~/.local/share/sightmesh/.cdesktop-workspaces`; state and logs live under `~/.local/state/sightmesh`.

Every workspace created by `sightmesh spawn` is bridge-enabled unless `--no-bridge` is passed. The bridge registers one durable Repowire proxy peer per enabled cdesktop session. Repowire asks become visible cdesktop follow-ups, and `sightmesh bridge-reply` closes the original correlation.

Set `SIGHTMESH_CDESKTOP_URL` when more than one cdesktop process is running. Otherwise SightMesh uses the managed loopback service or cdesktop's local port file.

## Capacity and credentials

SightMesh uses the Claude Code and Codex authentication already available to their CLIs, or a cdesktop provider configured by the user. It never stores provider secrets in its profile registry. Inspect cdesktop providers through a redacted view and create a named mapping:

```sh
sightmesh --json profile providers
sightmesh profile set work-claude-api \
  --executor CLAUDE_CODE \
  --provider CDESKTOP_PROVIDER_UUID \
  --credential-kind api \
  --model sonnet \
  --automatic-failover
```

Use a profile at launch with `sightmesh spawn ... --profile work-claude-api`. When a worker reaches a capacity or authentication boundary, a manager can automatically start a visible successor in the same cdesktop workspace, preserving the existing files and transcript:

```sh
sightmesh failover WORKSPACE_ID \
  --profile work-claude-api \
  --checkpoint-file handoff.md \
  --unattended
```

Use `--new-worktree` only for a clean committed handoff that needs a separate workspace. The source is preserved unless `--archive-source --confirm-reconciled` is explicit.

Automatic failover is allowed only for API or enterprise profiles explicitly configured through cdesktop. Ambient Claude Max, ChatGPT, or Codex consumer subscriptions can be selected for normal launches but cannot enter an automatic failover chain. SightMesh does not extract auth headers, copy cookies or tokens, silently switch logins, rotate consumer subscriptions, or evade rate limits.

## Conductor comparison

SightMesh is not better than Conductor in every dimension. Conductor currently has a more polished native Mac experience, integrated review and merge flows, repository setup and run scripts, file copying, managed settings, cloud workspaces, iOS control, and a hosted API.

SightMesh is the stronger fit when the requirements are local-only execution, a browser-visible fleet of full Claude and Codex sessions, agent-to-agent messaging across independent workspaces, scriptable idle-session prompting, durable message delivery, explicit ownership leases, provider-neutral failover, and open-source control over lifecycle policy. See `docs/conductor-parity.md` for the exact support matrix and remaining gaps.

## Scope

SightMesh is not novel as a general multi-agent terminal or Claude/Codex bridge. The closest overlapping projects include [Agent Deck](https://github.com/asheshgoplani/agent-deck), [cdesktop](https://github.com/cdesktop-ai/cdesktop), and [Claude Codex Bridge](https://github.com/SeemSeam/claude_codex_bridge). SightMesh focuses narrowly on cdesktop-native visibility plus Repowire messaging, durable delivery, worktree ownership, and auditable local lifecycle controls.
