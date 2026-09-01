# Lane K brief: Conductor-safe durable parent escalation

You are the single Lane K writer.
Objective: SightMesh must escalate safely when its launcher is external (for example Conductor) and no cdesktop parent session exists. Capture the external launcher identity at spawn time, provide a durable parent fallback, and a durable decision inbox so blocked workers' escalations are never lost when there is no live parent to receive them.

## Base, exact and verified

- Repo: sightmesh. Branch off `main` at exact head `5622486f923a4276b4e4aa4fb20f2f8067d7bf1e`.
- Verify HEAD equals that SHA before writing anything. If it does not, stop and report.

## Ownership

- Yours alone: external launcher identity capture, durable parent fallback, decision inbox.
- Not yours: `src/sightmesh/execution_routing.py` and routing settings/CLI (Lane C), spawn/failover reconciler integration (Lane D), docs (Lane H).
- Design note carried from the release gate: superseded or completed sessions must never be auto-resumed by queued delivery. Your inbox/fallback design must not deliver into retired sessions; park undeliverable escalations durably instead.

## Proof

- Focused pytest tests: launcher identity captured and persisted, fallback engages when no cdesktop parent exists, decisions survive restart, nothing delivers to a retired session. Report exact pass/fail counts.

## Delivery and stop

- Small checkpoint commits, clean pushed branch, DRAFT PR against `main` on clarkipeng/sightmesh. Draft only: no merge, ready, publish, workflow dispatch, or secret mutation.
- No detached/background/polling processes, no `sleep` monitors. Never message retired or completed sessions.
- When done or blocked: report exact branch, head SHA, PR number, and test counts to your parent with `sightmesh parent --message "STATUS: ..."`, then stop.
