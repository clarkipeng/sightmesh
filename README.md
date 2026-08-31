# SightMesh

> **Experimental.** SightMesh is usable for local evaluation, not yet a reliability claim. The current proof gate is several weeks of durable manager wake and acknowledged-delivery operation under real load. Until that evidence exists, active promotion is deliberately on hold.

SightMesh routes work across Claude and Codex accounts you own, launches full agents as visible and interruptible [cdesktop](https://github.com/cdesktop-ai/cdesktop) sessions, isolates implementation work in Git worktrees, and preserves the native state needed to inspect or recover work.

The proven wedge is deliberately small:

- ordered, quota-aware selection among accounts the operator owns and logged into normally;
- visible Claude Code and Codex sessions that a human can inspect or interrupt;
- isolated worktrees with explicit local ownership;
- recovery through Git, cdesktop transcripts and durable commands, and workspace-local `.context` handoffs.

See [execution routing](docs/execution-routing.md), [compatibility](docs/compatibility.md), [operations](docs/operations.md), the [trace-efficiency audit](docs/trace-efficiency.md), and the [source-derived competitive bake-off](docs/competitive-bakeoff.md) for tested versions, limitations, and alternatives.

SightMesh does not extract or replay credentials, bypass provider limits, make arbitrary local commands safe, or promise unattended recovery. See [Security](SECURITY.md) and [Architecture](docs/architecture.md).

## Pool-only quickstart

The credential pool works without installing or running the cdesktop fleet. It requires macOS, Python 3.11+, `uv`, the provider CLI you intend to use, and accounts you own.

```sh
git clone https://github.com/clarkipeng/sightmesh.git
cd sightmesh
uv tool install .

# Add one or more normally authenticated accounts.
sightmesh pool add-codex personal --mode sub
# Or, while currently logged into Claude, follow the setup-token prompt:
sightmesh pool add-claude personal-claude

sightmesh pool verify
sightmesh pool status
sightmesh pool exec codex exec "Summarize this repository"
```

Expected outcomes:

- `add-codex` opens the provider's normal browser login in a dedicated `CODEX_HOME`; `add-claude` asks for a token minted by `claude setup-token` for the currently logged-in account.
- `verify` makes a real request and reports whether entries are distinct and usable. Claude setup tokens and API keys do not expose live per-account quota, so their quota is reported as unknown rather than guessed.
- `status` names the account currently selected for each configured provider.
- `exec` prints the selected account to stderr and replaces itself with the normal provider CLI using only that account's credentials.

Pool state is local under `~/.config/agent-pool` by default. Do not add accounts you do not own or use pool ordering to evade a provider limit.

## Full visible fleet

The full setup additionally installs the cdesktop release and checksum recorded in the authoritative [runtime lock](src/sightmesh/runtime-lock.json), Repowire (`0.17.0`), local LaunchAgents, and shared Claude/Codex skills. The live behavior record and feature boundary are documented in [Compatibility](docs/compatibility.md).

```sh
./scripts/bootstrap-local.sh
sightmesh doctor
sightmesh configure
sightmesh service start
sightmesh service open
```

Expected outcome: `doctor` accepts the local dependencies, `service start` starts loopback-only cdesktop and bridge services, and `service open` opens the visible cdesktop fleet.

Launch an isolated supervised worker:

```sh
sightmesh spawn --name docs-review --repo /path/to/repo \
  --base main --executor CODEX --prompt "Review the public documentation" --worktree
```

The command creates a cdesktop workspace and Git worktree, starts a visible agent session, records local ownership, and bridge-enables the workspace unless `--no-bridge` is passed. Use `--unattended` only as an explicit worktree-only opt-in.

Inspect and contact the fleet:

```sh
sightmesh peers
sightmesh peek @docs-review
sightmesh message @docs-review --message "Check the installation claims"
sightmesh steer @docs-review --message "Stop implementation and report findings"
```

`message` waits for the next safe turn boundary. `steer` sends a native `replace` command scoped to the selected session; on this release it does not independently guard against a pending approval or question, so inspect the target before steering.

## Managed task SDK

Use semantic task keys and normal prompt strings. SightMesh owns task IDs, epochs, request hashes, leases, and command dedupe keys. cdesktop owns visible workspaces, worktrees, sessions, transcripts, and the exact-once native launch journal.

```python
from sightmesh import Command, SightMesh, WorkerSpec

mesh = SightMesh()
mesh.start(WorkerSpec(
    key="auth-audit",
    prompt="Audit the authentication boundary and report concrete risks.",
    repo="catapult-games",
    base="main",
))

mesh.start_all([
    WorkerSpec("api-tests", "Add focused API coverage.", "catapult-games"),
    WorkerSpec("docs-check", "Verify the public setup guide.", "catapult-games"),
])
mesh.send_all([
    Command("api-tests", "Also cover duplicate wakeups."),
    Command("docs-check", "Keep examples human-readable."),
])
```

`start` is idempotent for a key in the current manager scope. `start_all` validates and reserves the full batch before launching its members, then returns one result or error per key. `send_all` validates every destination before queueing any command. A replacement gets a new session in the same task-owned worktree only after the old session is quarantined. Checkpoint content stays in that worktree under `.context`, addressed by its content hash; SightMesh stores only the reference. Three total launch attempts trip the circuit breaker.

Repository names resolve to the one canonical checkout even when old managed-worktree registrations share that name. For a routed managed task, the bridge recognizes a terminal subscription quota refusal, cools only that opaque account binding, and transfers the task once to the next eligible configured route. Explicit profiles are never changed automatically.

The flat CLI uses the same service:

```sh
sightmesh start auth-audit "Audit the authentication boundary" --repo catapult-games
sightmesh show auth-audit
sightmesh send auth-audit "Also inspect refresh-token rotation"
sightmesh checkpoint "Tests pass; documentation remains"
sightmesh complete --summary "Audit complete"
```

For batches, prefer the Python SDK. The CLI also accepts `sightmesh start --batch jobs.json` and `sightmesh send --batch commands.json` for shell workflows. Prompt files are optional, not required.

Model rules of thumb are judgment, not routing code. Terra or Luna are usually efficient worker defaults. Use a stronger model such as Sol or Opus when debugging, auditing, or planning genuinely requires it. Scope and uncertainty matter more than the role label; profiles and the configured routing chain remain the source of truth.

## How ownership stays native

```text
Claude Code / Codex
        │
        ▼
cdesktop: visible sessions, transcripts, workspaces, durable commands
        │                    │
        │                    └── Git: branches and worktrees
        └── SightMesh: intent validation, leases, pool order, recovery policy
                             │
                             └── Repowire: cross-workspace contact
```

SightMesh does not create a second transcript store, global context mirror, or credential broker. That keeps recovery state inspectable in the systems that already own it, but also means the experimental stack inherits their failure modes.

## Documentation

Start with the [documentation map](docs/README.md). In particular:

- [Architecture](docs/architecture.md) — public design signal and honest limitations
- [Compatibility](docs/compatibility.md) — exact tested versions and platform boundary
- [Operations](docs/operations.md) — approvals, updates, failover, leases, and recovery
- [Conductor migration](docs/migration.md) — plan, apply, rollback, and preservation rules
- [Storage and retention](docs/storage.md) — local state and deletion boundaries
- [Release checks](docs/release.md) — maintainer verification and artifact provenance

## Project status and support

The frozen subscription-first execution-routing settings and their current integration boundary are documented in [execution routing](docs/execution-routing.md). cdesktop remains the primary workspace and session UI. `sightmesh pool serve` is a local, recovery/compatibility view of pool health only; it is not the execution-routing UI or a replacement for cdesktop.

SightMesh is Apache-2.0 licensed and accepts focused experimental feedback. It currently supports the tested macOS/Python combinations in [Compatibility](docs/compatibility.md); there is no SLA or production support commitment. Read [SUPPORT.md](SUPPORT.md), [CONTRIBUTING.md](CONTRIBUTING.md), and [SECURITY.md](SECURITY.md) before opening a report.
