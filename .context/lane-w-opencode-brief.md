# Lane W - OpenCode executor integration (free ox tier)

## Goal
Make opencode a first-class executor in the sightmesh/cdesktop framework, taking advantage of the currently-free `opencode/x-preview-f-free` model.

## Verified facts from captain's live probe (do not re-derive)
- Installed CLI: opencode 1.15.10 at /opt/homebrew/bin/opencode
- Headless works: `opencode run -m opencode/x-preview-f-free --format json "..."` emits JSON events including `sessionID`
- Session resume works: `opencode run -s <sessionID> ...` carries transcript context
- Free model exists and responded: `opencode/x-preview-f-free` (also hy3-free, mimo-v2.5-free, nemotron variants)
- Auth: env-based (ANTHROPIC_API_KEY / OPENAI_API_KEY visible); opencode auth.json has 0 stored credentials
- CRITICAL environment defect: the shared opencode db at `~/Library/Application Support/com.conductor.app/opencode/opencode.db` is corrupt (schema fully built, `__drizzle_migrations` empty), so EVERY bare `opencode` invocation dies replaying migration #1. Workaround proven: set `OPENCODE_DB=<dedicated path>` env var for all fleet spawns. Do not touch or repair the shared db - Conductor's live sessions use it.

## Existing code state
- cdesktop (fork, base c9e908a3 on origin/main): `crates/executors/src/executors/opencode.rs` (~850 lines) plus `opencode/` module (sdk.rs, models.rs, normalize_logs.rs, types.rs, slash_commands.rs). Wired into BaseCodingAgent enum as `Opencode`. Supports server-based spawn and follow-up resume via session id. NOT verified against CLI 1.15.10 behavior; may target an older opencode API.
- `crates/executors/src/outcome.rs`: no opencode-specific failure classification.
- sightmesh (base 86b5d99 on origin/main): zero opencode references anywhere - no profile tiers, no routing entries, no pool/credential mapping.

## Scope (single writer across both repos; they are coupled by this contract)
1. Audit cdesktop's inherited OpencodeExecutor against real 1.15.10 behavior. Fix what diverges (spawn command shape, session id extraction, log normalization, stop semantics). Keep diffs tight; delete dead compatibility branches rather than preserving them.
2. Outcome classification: enumerate real opencode failure shapes (auth, quota/rate-limit, network, crash) into the normalized outcome classes, derived from observed output - not guessed fixtures. Include at least one test per class using captured real output.
3. Fleet isolation: ensure cdesktop's opencode spawn sets a dedicated OPENCODE_DB (e.g., under the sightmesh data dir) so the corrupt shared db never breaks the fleet, and sessions survive restarts.
4. sightmesh: add opencode to profiles (e.g., `opencode-ox-free` tier pinned to x-preview-f-free), routing policy entry, and redacted credential/env mapping (ANTHROPIC_API_KEY passthrough policy consistent with existing doctrine).
5. Live end-to-end merge gate (same standard as Lane Q): spawn a real opencode worker through sightmesh itself on the free ox model, have it do a trivial repo task, kill it mid-turn, verify durable resume completes the turn. Record evidence in the PR body.

## Guards
- Bloat rules apply: no new daemons/pollers; reuse existing reconciler and pool machinery; smallest robust diff.
- Policy C: run full local gates (fmt, clippy flavors, targeted crate tests, cargo test for executors/db/services), push draft PR on clarkipeng/cdesktop AND clarkipeng/sightmesh (or one PR if sightmesh side is config-only), self-ready when green, send durable completion signal with exact heads and gate evidence.
- Never mark ready without the live E2E evidence recorded.
- Stop condition: both PRs delivered + E2E proof recorded. Report BLOCKED before ending your turn if anything blocks you.
