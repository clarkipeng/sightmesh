# SightMesh durable-execution convergence

Final ownership is intentionally narrow: cdesktop owns durable command rows,
execution processes, and keyed stop idempotency; SightMesh owns one in-service
reconciler and invokes those native seams. Git owns source and `.context` is
workspace-local handoff only.

| Component | Final owner | Interim deletion state |
|---|---|---|
| `queued -> claimed -> done | interrupted` | cdesktop command queue; `NativeCommandQueue` is the adapter | PR #6 queue preservation is retained; no parallel SightMesh store |
| Dead process, dead stream, suite-aware stall | `DurableExecutionReconciler`, run at bridge start and periodic tick | PR #9 `RecoveryIntentStore` and direct parent wake are retired from the active path |
| Connectivity gate | bounded local cdesktop health probe with exponential offline backoff | no provider mutation and no second scheduler |
| Stream death | reconciler interrupts and requeues the same command/dedupe key | no network-specific recovery branch |
| Child terminal notification | durable parent `continue` command, delivered by normal reconciliation | direct parent send/dedupe recovery is deleted after cutover |
| Cdesktop stop outcomes | native keyed stop contract: 424 interrupted, 409 rejected, 425 pending | no repeated stop after 424; 409/425 preserve native retry semantics |

Root sessions and `--no-bridge` children remain eligible for liveness
observation; only bridge task creation is skipped. The interim supervisor
`caffeinate`, polling monitors, and manual liveness convention can be removed
once the seam tests and in-service proof remain green.
