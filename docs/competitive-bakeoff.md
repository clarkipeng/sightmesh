# Competitive Bake-Off

Generated for base `6f17700d931449ac576fae17de9b6e54cfda1b90` on `2026-08-12`.

## Reproduce

```bash
uv run --with pytest python scripts/bakeoff/run_bakeoff.py \
  --manifest benchmarks/bakeoff_manifest.json \
  --out benchmarks/bakeoff_results.json

uv run --with pytest pytest -q tests/test_bakeoff.py
```

The runner clones competitors into `mktemp`, sets a task-local `HOME`, `XDG_*` roots, and `TMUX_TMPDIR`, clears inherited `TMUX`, and does not start provider CLIs or run global installs.

## Pinned Competitors

| Competitor | Source | Pinned ref |
| --- | --- | --- |
| Local agent-deck | local checkout | `6f17700d931449ac576fae17de9b6e54cfda1b90` |
| asheshgoplani/agent-deck | `https://github.com/asheshgoplani/agent-deck.git` | `refs/tags/v1.11.0` |
| SeemSeam/claude_codex_bridge | `https://github.com/SeemSeam/claude_codex_bridge.git` | `refs/tags/v8.6.1` |

## Scoring Snapshot

Scores distinguish static/runtime-safe observations from documented claims. Credentialed provider launches and installer mutations are blocked unless they can run fully inside the runner's isolated temp context.

| Scenario | Local agent-deck | asheshgoplani/agent-deck | CCB |
| --- | --- | --- | --- |
| Launch Claude Code worker | Pass | Pass | Pass |
| Launch Codex worker | Pass | Partial | Pass |
| Human visibility/takeover | Pass | Pass | Pass |
| Cross-agent request/reply | Pass | Partial | Pass |
| Isolated worktrees | Pass | Pass | Pass |
| Dirty-work refusal/equivalent | Pass | Partial | Blocked |
| Crash/restart recovery | Partial | Pass | Pass |
| Local-only operation | Pass | Partial | Partial |
| Install/uninstall containment | Partial | Partial | Partial |

## Main Findings

Local agent-deck is strongest for this project's exact target shape: full visible cdesktop workers, explicit Claude/Codex executor selection, Repowire request/reply, cdesktop worktree isolation, local-only cdesktop configuration, and a concrete dirty archive refusal.

asheshgoplani/agent-deck is a mature tmux/TUI session manager. It has broad session persistence, restart/revive, worktree, install, and visible takeover support, but Codex support is less direct for this bake-off's "launch full worker" requirement and request/reply is implemented through session send/conductor patterns rather than a neutral mesh bridge.

CCB has the broadest built-in cross-provider collaboration surface. It strongly covers visible terminal takeover, Claude/Codex panes, `/ask`, worktree-tagged agents, daemon lifecycle, and crash/restart documentation/source. The runner did not find an exact dirty-work refusal equivalent, and CCB's mobile/remote/update surfaces require careful scoping for strictly local-only bake-off runs.

## Limitations

No credentialed Claude Code or Codex provider session was started. No competitor installer was allowed to mutate the real `HOME`, tmux server, launchd state, cdesktop database, Repowire daemon, or global package state. Release metadata is fetched from GitHub/npm at run time and recorded in `benchmarks/bakeoff_results.json`.
