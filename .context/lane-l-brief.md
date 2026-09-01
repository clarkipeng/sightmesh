# Lane L brief: escalation intent semantics fix (release blocker)

You are the single Lane L writer, successor owner of the `src/sightmesh/escalation.py` surface (Lane K is retired; never contact it).
Objective: routine STATUS/completion callbacks must queue with durable acknowledgment and PRESERVE the recipient's active turn; only explicit BLOCKED/DECISION escalations may replace it.

## Defect you are fixing, proven in production tonight

`cmd_parent` -> `escalate()` delivers routine STATUS callbacks with `intent=replace`. Lane D's routine STATUS callback interrupted manager execution process `e5f03f76` mid-task and started `acf97607`. Full note: `/Users/clarkpeng/Documents/Code/sightmesh/.context/release-blocker-escalation-intents.md`.

## Base, exact and verified

- Repo: sightmesh. Branch off `cdt/fb85-lane-k-parent-es` at exact head `fa2defe148e06e3e2f6ba4df45dc3b5b7b973f0d` (Lane K delivered head, draft PR #22).
- Verify HEAD equals that SHA before writing anything. If it does not, stop and report.

## Required behavior

- Classify escalations: routine STATUS/completion vs explicit BLOCKED/DECISION.
- Routine: enqueue with `intent=continue` plus a durable acknowledgment record; must not cancel or replace the recipient's active turn.
- BLOCKED/DECISION: may use `intent=replace` (interrupting is correct there).
- Preserve all existing Lane K guarantees: durable parking when no live parent, never delivering into archived/retired sessions.

## Ownership

- Yours alone: `escalation.py`, `cmd_parent` wiring, and the intent classification path.
- Not yours: Lane D's reconciler/quarantine code on `cdt/17e1-lane-d-reconcile` (read it for interface truth if needed, do not modify), routing selector/settings (C/C2), docs (H), cdesktop Rust (A/B/I/J).

## Proof

- Focused pytest, both directions: routine STATUS queues durably with an ack record and does not interrupt an active turn; BLOCKED/DECISION replaces; retired-session non-delivery stays green. Run with `env -u CDESKTOP_SESSION_ID uv run --with pytest pytest -q` (known CDESKTOP_SESSION_ID leak breaks 4 unrelated pre-existing spawn tests otherwise). Report exact pass/fail counts.

## Delivery and stop

- Small checkpoint commits, clean pushed branch, DRAFT PR against `cdt/fb85-lane-k-parent-es` with explicit `--repo clarkipeng/sightmesh`. Draft only: no merge, ready, publish, workflow dispatch, or secret mutation.
- No detached/background/polling processes, no `sleep` monitors. Never message retired or completed sessions.
- When done or blocked: report exact branch, head SHA, PR number, and test counts via `sightmesh parent --message "STATUS: ..."`, then stop.
