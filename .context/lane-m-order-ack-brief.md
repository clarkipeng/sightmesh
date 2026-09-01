# Lane M brief: durable order-ack expectations and idle renudge

You are the single Lane M writer.
Objective: kill this recurring operational failure by construction: a manager or worker receives an order (`sightmesh message`), consumes the turn, replies or goes silent, and ends its turn WITHOUT acting. Today nothing notices, and the fleet stalls until a human renudges.

## Base, exact and verified

- Repo: sightmesh. Branch off `cdt/0c5b-release-candidat` at exact head `e87220a` (the rebased experimental release composition; it contains the merged D/K/L machinery plus the composed reconciler).
- Verify HEAD equals that SHA before writing anything. If not, stop and report.
- Your PR base is `cdt/0c5b-release-candidat` (stacked on draft PR #16), repo `clarkipeng/sightmesh`, explicit `--repo` on PR creation.

## Existing machinery you build on (do not fork parallel mechanisms)

- `src/sightmesh/escalation.py` (K/L): durable SQLite `EscalationStore` with `acknowledgments` table, intent classification (`continue`/`replace`), parked decision inbox, launcher identity.
- `src/sightmesh/durable.py`: `DurableExecutionReconciler` (composed head), quarantine via `OwnershipStore`, exactly-once guards via in-memory sets + durable dedupe keys, `NativeCommandQueue.notify_parent` sends dedupe-keyed continue messages.
- cdesktop delivery is already durable: `/sessions/{id}/follow-up` enqueues a SessionCommand and completion-side `dispatch_all_pending_commands` fires pending orders when the recipient goes idle. Delivery is NOT the gap; consumed-but-unacted orders are.

## Invariants to implement

1. `sightmesh message` records a durable order expectation (order id = dedupe key, else a generated one; sender session; recipient session; body digest; created_at). Opt-out flag `--no-expect-ack`; default ON.
2. An expectation is satisfied by any later outbound report from the recipient through the store: `sightmesh parent --message`, `sightmesh respond`, bridge reply, or an explicit `sightmesh ack <order-id>`.
3. Reconciler pass: recipient session idle (no running coding-agent execution) + unmet expectation older than a threshold (default 5 minutes, configurable) -> exactly ONE durable renudge follow-up quoting the original order verbatim with intent=continue and a dedupe key derived from the order id. Restart-safe: renudged state lives in the store, not memory.
4. If the recipient goes idle again with the expectation still unmet after the renudge -> park a durable escalation in the SENDER's decision inbox (existing parked-escalation machinery) exactly once; never nudge a third time; never loop.
5. Quarantined/retired recipients: never renudge, never dispatch; park to sender inbox immediately (reuse quarantine checks; queued delivery must never auto-resume retired sessions).
6. No secrets in expectations, renudges, or inbox rows.

## Proof

Focused pytest, exact counts reported: expectation created/satisfied paths; exactly-once renudge across two reconcile passes AND across a reconciler restart; second-idle-miss parks exactly once; quarantined recipient never renudged; `--no-expect-ack` creates nothing. Full suite `env -u CDESKTOP_SESSION_ID uv run --with pytest --with build pytest -q` must stay green (284 passed at your base).

## Delivery and stop

- Small checkpoint commits, clean pushed branch, DRAFT PR against base `cdt/0c5b-release-candidat` on clarkipeng/sightmesh. Draft only; no merge/ready/publish.
- No detached/background/polling processes, no `sleep` monitors, no hidden subagents. Never message retired or completed sessions.
- When done or blocked: report exact branch, head SHA, PR number, and test counts via `sightmesh parent --message "STATUS: ..."` AND append the same line to `/Users/clarkpeng/Documents/Code/sightmesh/.context/lane-m-status.md`, then stop.
