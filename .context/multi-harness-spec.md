# Multi-Harness Surface Spec

Status: draft for review.
Owner: sightmesh/cdesktop framework.
Scope: generalized behavior across claude-code, codex, and opencode; adding a fourth harness must require only one adapter plus profile/routing registration.

## Goal and invariant

One framework contract owns queueing, durable dispatch/follow-up, stop, outcome normalization, approvals, and wake/resume semantics.
Harness-specific knowledge lives only inside its adapter module behind the existing `StandardCodingAgentExecutor` trait in cdesktop (`crates/executors/src/executors/`).
No `match executor` branching outside an adapter module; sightmesh never knows harness internals, only profiles and routing tiers.

## What each harness natively provides (verified against installed versions)

| Capability | claude-code (installed CLI) | codex (installed CLI) | opencode 1.15.10 |
|---|---|---|---|
| Headless run | `-p` with `--output-format=stream-json` | `codex exec` with JSON output | `opencode run --format json`, or full HTTP server (`opencode serve`) |
| Structured events | stream-json both directions (`--input-format=stream-json`) | JSONL / app-server jsonrpc peer | SSE event bus + JSON run events |
| Session resume | `--resume <id>`, `--resume-session-at` truncation | `codex exec resume <id>` / `fork` | `run -s <sessionID>` or server API on same session |
| Session listing/fork | `--continue`, `--fork-session` | `resume --last`, `fork`, `archive` | `/session`, `/session/:id/fork`, `/session/status` |
| Permission model | permission modes + `--dangerously-skip-permissions`; approval over stream-json | sandbox modes + `--dangerously-bypass-approvals-and-sandbox`; approval via protocol | config `permission: {edit,bash,webfetch}`; server endpoint `POST /session/:id/permissions/:permissionID` |
| Model selection | `--model`, `--fallback-model` | `-m provider/model` | `-m provider/model`, `/config/providers` |
| Async prompt | background sessions (`--bg`) | exec is batch by design | `POST /session/:id/prompt_async` |
| State location | `~/.claude` projects dir | `~/.codex` rollouts | opencode.db (`OPENCODE_DB` override) |

Key asymmetries the contract must absorb:

1. opencode is uniquely server-shaped: a persistent HTTP server with OpenAPI, SSE, async prompts, and a permissions-respond endpoint. The other two are process-per-turn with streamed stdout. Our adapter already wraps the server; keep that, but the trait boundary must not leak HTTP concepts upward.
2. codex couples approvals to its sandbox policy rather than a discrete permission prompt surface; claude-code and opencode both have explicit approval request/response flows we can map onto metered approvals.
3. Failure shapes differ per harness and must be classified inside each adapter into shared normalized outcome classes (`quota_exhausted`, `auth`, `network_transient`, `unknown`); nothing above the adapter may string-match raw output.

## Unified surface contract

For every harness the framework requires exactly these adapter obligations:

1. Spawn: given worktree, env, profile-resolved model, produce a running execution and a normalized log stream.
2. Follow-up: given a session id, deliver the next turn into the same transcript (durable queue dispatches this).
3. Stop: replay-safe terminal stop of the current turn without corrupting resumability of the session.
4. Resume proof: after any kill/restart, follow-up into the same session id works; this is the Lane Q-standard live gate.
5. Outcomes: map observed failures to the shared outcome classes from captured real output only; unknown shapes classify as `unknown`, never guessed.
6. Approvals: surface permission requests onto the metered auto/ask/never policy where the harness supports explicit requests; document degradation where it does not (codex sandbox coupling).
7. Isolation: no shared mutable global state across fleet sessions; dedicated db/config paths per harness family (the `OPENCODE_DB` precedent becomes a general rule).

Framework-level behaviors stay single-sited: command rows, claim/create/bind ordering, boot terminalization, order expectations, signal policies, merge gating.

## Current gaps

1. cdesktop's opencode executor is inherited and unverified against installed CLI 1.15.10 (Lane W in flight).
2. Outcome classification has no opencode entries (Lane W scope).
3. sightmesh has zero opencode profiles/routing/pool tiers (Lane W scope).
4. Codex approvals ride sandbox policy, so metered ask semantics are weaker there than claude/opencode; needs an honest documented degradation or an adapter-side mapping.
5. Per-executor special-casing outside adapters has not been systematically audited (Lane W records findings; cleanup is a separate lane if the list is long).

## Plan

Lane W (running): verify opencode adapter against 1.15.10, add outcome classes from captured output, dedicated `OPENCODE_DB`, sightmesh `opencode-ox-free` tier, live kill/resume E2E as merge gate. Its PR is reviewed against this spec.

Lane W2 (proposed, small): approvals-parity pass - map opencode's `permissions/:permissionID` endpoint and claude-code's approval flow onto metered approvals uniformly; document codex's sandbox-coupled degradation in the owning docs.

Lane W3 (conditional on W's findings): collapse any discovered cross-adapter duplication into the trait so a fourth harness is adapter-plus-registration only.

Non-goals: wrapping harness interactive TUIs, supporting harness versions older than installed, repairing the user's corrupt shared opencode.db (upstream report instead).
