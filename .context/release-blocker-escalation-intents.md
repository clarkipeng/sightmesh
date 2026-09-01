# RELEASE BLOCKER: escalation intent semantics (K/D follow-up)

Date: 2026-08-18 America/Los_Angeles. Filed by hot-swap train manager 5 under root oversight finding.

## Defect, with exact evidence

Routine STATUS/completion callbacks routed through `cmd_parent` -> `escalate()` are delivered with `intent=replace`.
Evidence: Lane D's routine STATUS callback interrupted manager execution process `e5f03f76` during Lane J reconciliation and started `acf97607`.
A routine progress report must never cancel and replace the recipient's active turn.

## Required behavior

- Routine STATUS/completion callbacks: queue with `intent=continue` and a durable acknowledgment record; preserve the recipient's active turn.
- Only explicit BLOCKED/DECISION escalations may use `intent=replace`.
- Both behaviors need focused regression proof; K's existing guarantees (durable parking, never delivering into retired sessions) must stay green.

## Status

- FIX DELIVERED, pending final exact-head review: Lane L draft PR #24, branch `cdt/60de-lane-l-escalatio`, head exactly `8bd82e7c14c4b358da0cb1dfaa34417082500ae4`, stacked on K head `fa2defe1`. Manager independently re-ran the full suite at that head: 204 passed 0 failed. See `.context/lane-l-reconciliation.md`.
- Until the final read-only rereview confirms both behaviors at exact heads, K PR #22 (`fa2defe1`) and D PR #23 (`fdf12e0c`) remain blocked from any ready/merge decision, and their merge order must place L with K.
- K and D stayed retired and uncontacted throughout.
- Related harness evidence: `.context/lane-j-reconciliation.md` (lost parent callback, transient stale-running process).
