# Lane Z - last known inconsistencies

Base: cdesktop origin/main (post #17), sightmesh origin/main (post #51).

Three loose ends, one writer, all small. Each is independent; deliver as one PR per repo or stacked commits, your call.

## 1. Adapter-owned state isolation for all harnesses
Today opencode pins its session database inside its adapter (OPENCODE_DB), but claude-code (CLAUDE_CONFIG_DIR) and codex (CODEX_HOME) isolation depends on the launcher remembering to pass env. Move both to adapter-owned setup beside the existing pattern in the opencode adapter, so isolation holds no matter who spawns the process. Sightmesh's launcher-side pinning can stay (harmless redundancy) or be simplified to rely on the adapters - state which you chose and why.

## 2. Steer atomicity
cdesktop steer is interrupt+enqueue as two steps; under load a crash between them leaves a session interrupted with the replacement prompt lost. Make it one durable step: if the interrupt lands, the replacement command must already be durably queued before the interrupt is sent, or the whole operation fails cleanly with nothing half-applied. Reuse existing transaction patterns from the dispatch-fix work (#12).

## 3. Dashboard settings read route
The Settings > Execution Routing section still renders honestly-labeled fixtures because cdesktop has no settings bridge. Add a minimal read-only route projecting sightmesh routing settings (the same secrets-free projection style as the outcomes route), and render real values when present, keeping the fixture label only where a value genuinely does not exist. Do NOT add write-back; that is a separate decision.

## Guards
Bloat rules apply. Tests from captured real output where applicable. Policy C: fmt, clippy workspace qa-mode -D warnings, cargo test -p executors -p db -p services (+ sightmesh suite if touched), generated-types check if shared types change, draft PRs, self-ready on green, durable completion signal with exact heads and evidence. Report BLOCKED before ending your turn.
