# Conductor parity and product boundary

SightMesh is designed for a different center of gravity than Conductor. It does not claim universal superiority.

Both systems treat `.context` as workspace-local files rather than a global context database. SightMesh keeps that familiar convention. Cross-workspace visibility comes from the cdesktop fleet and normal Git worktree inspection; communication comes from Repowire.

| Requirement | SightMesh 0.9 | Conductor | Current verdict |
| --- | --- | --- | --- |
| Local full Claude Code and Codex processes | Yes | Yes | Parity |
| Isolated Git worktrees | Yes | Yes | Parity |
| One screen for live sessions and transcripts | cdesktop browser UI | Native Mac UI | Conductor is more polished |
| Prompt an existing idle session from a script | `sightmesh prompt-idle` | App and agent controls | SightMesh has an explicit local CLI guard |
| Cross-workspace agent-to-agent messaging | Repowire ask/reply into durable cdesktop commands | No documented neutral peer mesh | SightMesh advantage |
| Visible delegated workers instead of hidden native subagents | Enforced by shared Claude/Codex skills | Parallel workspaces are visible; native delegation policy is not SightMesh-controlled | SightMesh is more opinionated |
| Durable command queue, dedupe, and restart recovery | Native cdesktop command log | Internal implementation is not exposed as this operator surface | SightMesh advantage for local scripting |
| Checkout ownership conflict prevention | Explicit expiring leases | Workspace isolation | Different mechanisms; SightMesh exposes ownership state |
| Claude/Codex provider selection | Named mapping to cdesktop providers | Agent authentication and environment settings | Both supported |
| Checkpointed provider failover | Visible successor session or clean new worktree | Manual restart or new workspace | SightMesh advantage for local scripted handoff |
| Ordered fallback across operator-owned accounts | Credential pool selects the first account with quota | No documented support | SightMesh advantage |
| Repository setup and run scripts | cdesktop repository configuration | Mature first-class support | Conductor advantage |
| Copying ignored workspace files | cdesktop capability, not yet wrapped by SightMesh | First-class Files to copy and `.worktreeinclude` | Conductor advantage |
| Diff review, comments, checks, and merge | cdesktop/GitHub surfaces | Deep native integration | Conductor advantage |
| Native Mac application | No, browser UI | Yes | Conductor advantage |
| Mobile control | Local network or separately configured Repowire surfaces; relay disabled by default | iOS app for supported workspaces | Conductor advantage |
| Cloud workspaces and public API | Intentionally no cloud | Yes | Conductor advantage unless local-only is mandatory |
| Open-source policy and lifecycle layer | Apache-2.0 | Proprietary app | SightMesh advantage |
| Resumable Conductor workspace and context migration | In-place adoption, transcript handoffs, leases, status, and rollback | Not applicable | SightMesh transition aid |

## What automatic failover means

SightMesh profiles contain only a name, executor, cdesktop provider UUID, optional model and reasoning defaults, credential classification, and policy flags. Provider keys remain inside cdesktop's provider configuration.

For the explicitly named profile the operator configured, `sightmesh failover` starts a visible successor. The default keeps the same cdesktop workspace so dirty files and transcript context remain visible. A clean committed source can instead use `--new-worktree`. Credential-pool selection is provided separately by `sightmesh pool exec` for direct Claude and Codex CLI launches.

The shared reconciliation skill instructs Claude and Codex managers to invoke this handoff when they observe a capacity or authentication boundary. This is orchestration across accounts the operator owns, each used with its own normal credentials, not credential extraction, auth-header replay, or limit evasion.

## Remaining work before a broad replace-Conductor claim

1. Add a polished SightMesh-specific fleet dashboard instead of relying only on cdesktop's workspace UI and `sightmesh status`.
2. Wrap repository setup scripts, development commands, port allocation, and ignored-file copying as a versioned project manifest.
3. Add first-class diff comments, checks, pull request creation, readiness gates, and merge controls.
4. Run 24-hour burst, crash, reboot, and command-replay soak tests.
5. Expand the compatibility matrix beyond the currently pinned cdesktop, Repowire, Claude Code, and Codex versions.

Until those land, SightMesh is better for the requested local visible multi-agent mesh, not better than Conductor in every aspect.
