# Lane V brief: split cli.py into per-command-group modules (issue #26)

Single writer. Read clarkipeng/sightmesh#26 first. Pure mechanical restructure - ZERO behavior change.

## Base
sightmesh `main` (must contain PR #40 and #41 squashes). If PR from Lane U (#27) merges while you work, rebase onto it before opening your PR - it touches succession/escalation, you touch structure.

## Scope
- `src/sightmesh/cli.py` is ~3,500 lines. Split into a `cli/` package: `cli/__init__.py` (parser assembly + main + shared helpers), and one module per command group (pool, routing, policy, workspace/lifecycle, spawn/failover, bridge/peers/messaging, update/service, doctor/status). Preserve every command, flag, output shape, and import path (`from sightmesh.cli import X` used by tests must keep working via re-exports, or update the tests mechanically).
- No logic edits, no renames of behavior-bearing functions, no dead-code removal beyond what the move makes obviously unreferenced (list any such removal in the PR body).
- Each new module under 800 lines.

## Proof
Full suite green unchanged (297 at base; count must not drop except renamed test imports). `python -c "import sightmesh.cli"` and `sightmesh --help` snapshot identical before/after (diff the help text in your PR body).

## Delivery (lane policy C)
Draft PR then self-ready on green gates, explicit --repo clarkipeng/sightmesh, base main. STATUS to `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-v-status.md` (absolute path, not your worktree) AND `sightmesh parent --message`. Reviewer merges. No background processes; never message retired sessions.
