# SightMesh

SightMesh is a local reliability and policy layer for full Claude Code and Codex workers. It launches them as visible cdesktop sessions, gives implementation workers isolated git worktrees, and connects explicitly enabled sessions through Repowire.

Delegated work remains visible and interruptible in cdesktop. SightMesh does not replace cdesktop's UI or the agent CLIs. It adds ownership leases, safe closeout, local-only configuration, service recovery, and shared Claude/Codex skills. cdesktop owns durable command delivery.

The interface is deliberately native-first. `.context` remains a normal workspace-local, Git-ignored directory. cdesktop remains the source of truth for sessions and transcripts, Git remains the source of truth for worktrees and changes, and Repowire remains the cross-workspace contact layer. SightMesh does not build a global context mirror or copy every conversation into another agent-specific format.

See [compatibility](docs/compatibility.md), [operations](docs/operations.md), the [trace-efficiency audit](docs/trace-efficiency.md), and the [source-derived competitive bake-off](docs/competitive-bakeoff.md) for tested versions, limitations, and alternatives.

The replacement v0.9 contract is defined in [architecture](docs/architecture.md).

## Install

Prerequisites:

- macOS
- Python 3.11 or newer
- `uv`
- the SightMesh cdesktop fork package `0.2.3-sightmesh.4`
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

To cut over an existing installation of the former project name, install SightMesh, run `sightmesh configure`, then run `sightmesh service cutover`. Cutover backs up the old LaunchAgent definitions, stops only the two old owned labels, migrates routing and lease state, starts the new labels, and checks health.

The installer links the canonical skills into both `~/.claude/skills` and `~/.codex/skills`. It does not copy, inspect, or alter model-provider credentials.

Bootstrap installs the tested cdesktop fork from its public GitHub release. `sightmesh doctor` rejects an upstream-only CLI installation so the approval API, exact model input, and process-bound response guarantees cannot silently disappear.

## Core workflow

Supervised worker:

```sh
sightmesh spawn --name worker-name --repo /path/to/repo \
  --base main --executor CODEX --prompt-file prompt.txt --worktree
```

Short inputs can be passed inline instead of through a file:

```sh
sightmesh spawn --name worker-name --repo /path/to/repo \
  --base main --executor CODEX --prompt "Implement the bounded task" --worktree
sightmesh message SESSION_ID --message "Review the failing check"
sightmesh steer SESSION_ID --message "Stop the migration and only diagnose"
```

`--prompt-file`, `--message-file`, and `--checkpoint-file` remain available for long or reusable inputs. Each command accepts exactly one inline or file-backed form.

Unattended worker in an isolated worktree:

```sh
sightmesh spawn --name worker-name --repo /path/to/repo \
  --base main --executor CODEX --prompt-file prompt.txt --worktree --unattended
```

`--unattended` is deliberately worktree-only and selects cdesktop's bypass permission policy. Direct checkouts remain supervised. The SightMesh cdesktop fork adds scriptable plan approvals for supervised work; unattended mode remains an explicit opt-in for isolated autonomous work.

For a worktree spawn, `.conductor/settings.toml` `[scripts] setup` runs in the new worktree before the coding agent starts. The managed cdesktop service receives the invoking user's interactive-login command path, so setup tools and the executor resolve as they do in a normal terminal. If cdesktop cannot start the executor after creating a workspace, SightMesh reports the native error and deletes the one unambiguous partial workspace through cdesktop's normal delete lifecycle.

Other lifecycle commands:

```sh
sightmesh status
sightmesh list
sightmesh message SESSION_ID --message-file follow-up.txt
sightmesh prompt-idle SESSION_ID --message-file follow-up.txt
sightmesh steer SESSION_ID --message-file correction.txt
sightmesh workspace rename WORKSPACE_ID catapult-games/voice-manager
sightmesh bridge-route WORKSPACE_ID --enabled
sightmesh close WORKSPACE_ID --message-file closeout.txt
sightmesh workspace archive WORKSPACE_ID --confirm-reconciled
sightmesh workspace restore WORKSPACE_ID
sightmesh workspace delete WORKSPACE_ID --confirm-delete
sightmesh lease list
```

Stage a verified cdesktop release while workers continue running:

```sh
sightmesh update stage \
  --package https://github.com/OWNER/cdesktop/releases/download/TAG/cdesktop.tgz \
  --version VERSION \
  --sha256 EXPECTED_SHA256
sightmesh update status
```

Staging installs the package in a private versioned directory and never modifies the
running executable. Activate it explicitly with `sightmesh update activate`. Activation
returns safely while work or approvals are active, verifies backend health and version,
and never runs a polling daemon or automatic retry loop. `sightmesh update cancel`
cancels a pending activation without deleting its verified package.

The SightMesh CLI and skills can be replaced between commands without restarting
cdesktop. The cdesktop backend cannot be replaced inside an active model turn because
that process owns the executor stream and live approval responder. The staged updater
therefore performs an automatic boundary handoff rather than claiming an unsafe
mid-turn hot swap.

Review pending agent plans from a lead session or directly as the local human:

```sh
sightmesh approval list
sightmesh approval show APPROVAL_ID
sightmesh approval approve APPROVAL_ID
sightmesh approval reject APPROVAL_ID --reason "The migration rollback is incomplete"
sightmesh approval history
sightmesh inbox
sightmesh respond --responses '[{"approval_id":"question-id","answers":["Option A",["Check 1","Check 2"]]},{"approval_id":"plan-id","decision":"approve"}]'
```

Visible agent sessions cannot approve their own requests. When `CDESKTOP_SESSION_ID` is set, only the earliest session in that cdesktop workspace is treated as its lead reviewer. Questions remain interactive in cdesktop and are also available through the global inbox. Non-plan tool requests require an explicit `"allow_non_plan": true` acknowledgement in a batch response.

`sightmesh inbox` joins every pending question, plan, or tool request with its agent, workspace, normalized request payload, and response template. `sightmesh respond` accepts an inline JSON array or file, prevalidates the complete batch before its first response, preserves ordered answers when question text repeats, and reports per-item timeout races without blocking later responses.

Pending metadata could be copied to disk, but the live responder belongs to the running executor. A backend restart interrupts that executor, so SightMesh does not display a persisted request as if its original turn could still accept a response. Restart recovery must resume the visible session with an explicit follow-up.

## Migrate from Conductor

Create a private, read-only plan first:

```sh
sightmesh --json migrate plan --conductor-root ~/conductor
```

After pausing the selected Conductor sessions, adopt current workspaces without launching agents:

```sh
sightmesh migrate apply PLAN_PATH --all --confirm-conductor-paused
```

The migration is resumable and preserves checkouts, dirty files, `.context`, archived contexts, and the original Conductor transcript database in place. Active workspaces become cdesktop rows. Archived history is cataloged as private handoffs without filling the active or archived cdesktop sidebar unless `--materialize-archived` is explicitly selected. See [Conductor migration](docs/migration.md) before applying it to real workspaces and [local storage and retention](docs/storage.md) for the ownership and cleanup contract.

Archive always refuses a dirty cdesktop-managed worktree because cdesktop reclaims archived worktrees after about one hour. Dirty state can be explicitly preserved only for a direct workspace, whose repository cdesktop does not own. Restore keeps the archive's history and recreates a reclaimed managed worktree from its preserved Git branch when execution resumes. Delete is a separate confirmed action that removes the cdesktop archive and owned worktree while preserving the branch by default. Spawn acquires an expiring ownership lease; isolated worktrees from the same repository may coexist, while direct-checkout ownership conflicts fail closed.

## Local architecture

```text
Claude Code CLI ─┐
                 ├─ cdesktop visible sessions and worktrees
Codex CLI ───────┘            │
                              ├─ SightMesh lifecycle and leases
                              │
                              └─ Repowire local request/reply mesh
```

The managed LaunchAgents are `io.sightmesh.cdesktop` and `io.sightmesh.bridge`. cdesktop binds to `127.0.0.1:3210`; analytics and relay are disabled; managed worktrees default to `~/.local/share/sightmesh/.cdesktop-workspaces`; state and logs live under `~/.local/state/sightmesh`.

Every workspace created by `sightmesh spawn` is bridge-enabled unless `--no-bridge` is passed. The bridge registers one Repowire proxy peer per enabled cdesktop session. Repowire asks become durable cdesktop commands, and `sightmesh bridge-reply` closes the original correlation.

Every workspace launched from a cdesktop agent records that exact parent session. Children can inspect or immediately contact their launcher without looking up an ID:

```bash
sightmesh parent
sightmesh parent --message "BLOCKED: need a decision on the storage schema"
```

`sightmesh children` lists only the workspaces directly launched by the current session. These edges are return addresses, not authority locks. Same-workspace teammates can also use the native `cdesktop team manager` alias. Cross-workspace durable, non-interrupting communication continues to use Repowire.

Every local agent can inspect and immediately steer every other visible agent with compact, exact selectors:

```bash
sightmesh peers
sightmesh peek @recorder-v2-provider-proof
sightmesh steer @recorder-v2-provider-proof --message "Re-check the exact remote head before finishing"
```

`peers` avoids the full workspace/status payload. `peek` coalesces the live normalized trace into the latest assistant message, recent tools, and token usage in one response. `steer` stops only the selected session's active coding turn, leaves its peer sessions and dev server alone, then starts the follow-up immediately. It refuses self-steering and agents with a pending approval or question.

The fork renders a multi-question request as one scrollable form and submits every answer in one response. Worker prompts also require independent read-only tool calls and currently known independent questions to be batched, while dependent writes, approvals, and destructive actions stay sequential.

cdesktop already groups the sidebar by repository, shows the live transcript and process logs, renders changed-file diffs, opens the full checkout in the configured IDE, and provides a built-in browser preview for running applications and image assets. SightMesh keeps those native surfaces instead of adding a second file viewer or transcript store.

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

A profile names one account the operator owns. Several profiles for the same executor can be ordered into a credential pool: a launch takes the first account in that order which still has quota, and moves down the list as accounts are exhausted. Claude Max and Codex subscription accounts belong in a pool alongside API and enterprise profiles.

Use a profile at launch with `sightmesh spawn ... --profile work-claude-api`. When a worker reaches a capacity or authentication boundary, a manager can automatically start a visible successor in the same cdesktop workspace, preserving the existing files and transcript:

```sh
sightmesh failover WORKSPACE_ID \
  --profile work-claude-api \
  --checkpoint-file handoff.md \
  --unattended
```

Use `--new-worktree` only for a clean committed handoff that needs a separate workspace. The source is preserved unless `--archive-source --confirm-reconciled` is explicit.

Every account in a pool is one the operator owns and logged into through the provider's own interface, and each worker runs on that account's own normal credentials. SightMesh observes quota and selects the next owned account. It does not extract credentials, replay auth headers, copy cookies or tokens, share one account's session with another worker, or evade rate limits.

## Conductor comparison

SightMesh is not better than Conductor in every dimension. Conductor currently has a more polished native Mac experience, integrated review and merge flows, repository setup and run scripts, file copying, managed settings, cloud workspaces, iOS control, and a hosted API.

SightMesh is the stronger fit when the requirements are local-only execution, a browser-visible fleet of full Claude and Codex sessions, agent-to-agent messaging across independent workspaces, scriptable idle-session prompting, durable cdesktop commands, explicit ownership leases, provider-neutral failover, and open-source control over lifecycle policy. See `docs/conductor-parity.md` for the exact support matrix and remaining gaps.

## Scope

SightMesh is not novel as a general multi-agent terminal or Claude/Codex bridge. The closest overlapping projects include [Agent Deck](https://github.com/asheshgoplani/agent-deck), [cdesktop](https://github.com/cdesktop-ai/cdesktop), and [Claude Codex Bridge](https://github.com/SeemSeam/claude_codex_bridge). SightMesh focuses narrowly on cdesktop-native visibility plus Repowire messaging, worktree ownership, and auditable local lifecycle controls.
