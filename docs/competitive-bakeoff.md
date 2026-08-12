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

Scores distinguish static/runtime-safe observations from documented claims. Every `observed_static` result in `benchmarks/bakeoff_results.json` includes machine-readable `source_evidence` with relative file, line, matched pattern, and excerpt. Credentialed provider launches and installer mutations are not counted as fresh runtime evidence unless they can run fully inside the runner's isolated temp context.

| Scenario | Local agent-deck | asheshgoplani/agent-deck | CCB |
| --- | --- | --- | --- |
| Launch Claude Code worker | Pass | Pass | Pass |
| Launch Codex worker | Partial | Pass | Pass |
| Human visibility/takeover | Pass | Pass | Pass |
| Cross-agent request/reply | Pass | Partial | Pass |
| Isolated worktrees | Pass | Pass | Pass |
| Dirty-work refusal/equivalent | Pass | Unknown | Unknown |
| Crash/restart recovery | Partial | Pass | Pass |
| Local-only operation | Pass | Pass | Pass |
| Install/uninstall containment | Partial | Partial | Partial |

## Scenario Winners

- Launch Claude Code worker: three-way static tie.
- Launch Codex worker: asheshgoplani/agent-deck and CCB; local agent-deck is qualified by the recorded cdesktop/Codex stall.
- Human visibility/takeover: three-way static tie.
- Cross-agent request/reply: local agent-deck and CCB.
- Isolated worktrees: three-way static tie.
- Dirty-work refusal/equivalent: local agent-deck only under the exact close/archive dirty-refusal criterion; the runner did not verify an equivalent in the competitors.
- Crash/restart recovery: asheshgoplani/agent-deck and CCB.
- Local-only operation: three-way static tie; optional remote/mobile features are not penalized when they are opt-in.
- Install/uninstall containment: no full winner; all three are partial because runtime install/uninstall was not executed against real user-global state.

## Main Findings

CCB is the closest feature-overlap competitor. It covers visible terminal takeover, Claude/Codex panes, `/ask`, worktree-tagged agents, daemon lifecycle, crash/restart recovery, loopback defaults, and opt-in remote boundaries in the pinned source. The runner did not verify an exact dirty close/archive refusal equivalent.

asheshgoplani/agent-deck is a mature tmux/TUI session manager. The corrected runner scores its direct `-c codex` support as pass when proved by the pinned source. It has broad session persistence, restart/revive, worktree, install, local tmux operation, and visible takeover support; request/reply is implemented through session send/conductor patterns rather than a neutral mesh bridge.

Local agent-deck has the clearest match for cdesktop workspaces, Repowire proxy ask/reply, explicit cdesktop worktree routing, local-only cdesktop configuration, and dirty archive refusal. Its Codex launch/recovery score is qualified: the local compatibility record for cdesktop `0.2.3` plus Codex CLI `0.147.0` is supplemented by this bake-off's observed supervised-approval/MCP-elicitation stall, so unattended Codex launch/recovery is not counted as a fresh runtime pass.

The total scores in the JSON are useful as a compact checklist count only when comparing like evidence classes. They are not a fresh end-to-end runtime bake-off because no credentialed provider launch was executed.

## Limitations

No credentialed Claude Code or Codex provider session was started by the runner. A successful CLI introspection command is recorded separately from launch evidence and is not treated as proof that a provider launched. No competitor installer was allowed to mutate the real `HOME`, tmux server, launchd state, cdesktop database, Repowire daemon, or global package state. Release metadata is fetched from GitHub/npm at run time and recorded in `benchmarks/bakeoff_results.json`.
