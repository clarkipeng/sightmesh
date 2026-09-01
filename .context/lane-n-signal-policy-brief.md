# Lane N brief: per-worker signal policy (issue #33)

You are the single Lane N writer. Implement clarkipeng/sightmesh#33 exactly; read it first.

## Base

Repo sightmesh, branch off current `main` (must contain 6a6a396). Verify before writing.

## Scope, deliberately minimal

1. A per-session `signal_policy` record in the existing escalation SQLite store (no new store, no new daemon). Settable by the session itself or any live session (manager): `sightmesh policy show @agent`, `sightmesh policy set @agent --signal-on <cond>[,<cond>]`, `sightmesh policy clear @agent`.
2. v1 conditions, all already observable by the reconciler/CLI without new infrastructure:
   - `terminal` - the session's execution reaches a terminal state.
   - `context-pressure:<0..1>` - peek-reported context pressure crosses the threshold.
   - `idle:<seconds>` - idle longer than the given duration with a non-terminal assignment.
3. Enforcement inside the EXISTING durable reconciler sweep: when a condition first becomes true, send ONE durable dedupe-keyed intent=continue message to the session's parent (or park in the decision inbox when no live parent, reusing Lane K machinery). Dedupe key per (session, condition instance) so a condition can never spam.
4. Default policy is empty = today's behavior unchanged. No implicit policies.

## Explicitly out of scope (bloat guards)

- No CI/GitHub condition types - repo-specific watchers stay repo-side scripts.
- No renudge loops, retries, or escalation chains beyond the single dedupe-keyed signal.
- No semantic conditions (the model's own judgment stays in briefs).
- No new background processes, threads, or pollers.

## Proof

Focused pytest with why-docstrings: policy CRUD by self and by peer, each condition fires exactly once across repeated sweeps AND across reconciler restart, no-parent parks exactly once, empty policy is a no-op. Full suite green (`env -u CDESKTOP_SESSION_ID uv run --with pytest --with build pytest -q`, 289 at base).

## Delivery (lane policy C)

Open a PR on clarkipeng/sightmesh (explicit --repo) targeting main. The moment your full local gates pass, mark it ready yourself and report: append "STATUS: ..." with branch, exact head, PR number, test counts to `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-n-status.md` AND send `sightmesh parent --message`. The reviewer merges; you never merge. No detached/background processes; never message retired sessions.
