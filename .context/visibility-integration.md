# Fleet visibility integration contract

`sightmesh.fleet` is a pure projection: it has no network clients, storage, or
ambient clock. CLI and UI composition remain owned by their native lanes.

## Input

The composer reads existing native state and constructs `FleetFacts`:

| Fact | Native owner | Required identity |
| --- | --- | --- |
| workspace and execution | cdesktop | `id`, `workspace_id`, `status`, optional `last_event` |
| approval | cdesktop | `execution_id`, `status` |
| parent relationship | cdesktop / Repowire | `execution_id`, parent summary |
| account and quota | pool | `id`, `provider`, optional `quota` including provider `resetsAt` |
| branch and delivery | Git / GitHub | `execution_id`, `branch`, PR or CI summary |

Pass an explicit UTC `now`, plus the renderer's in-memory `viewed_at`, to
`overview(facts, now=..., viewed_at=...)`. `viewed_at` is presentation state;
do not persist it. Tokens use `token_usage` with provider-reported provenance.
Money is optional `monetary_cost`, supplied by an external billing source with
its provenance; never calculate a price from tokens.

## Output and default overview

Render `FleetOverview.to_dict()` under these headings, in supplied order:

```text
Needs attention
  fleet/payments/run-17  Approval is required.  Review the approval.

Running
  fleet/payments/run-18  Execution is active.  Monitor the next meaningful event.

Done since view
  fleet/payments/run-16  Execution is completed.  Inspect the delivery reference.
```

Each row exposes one reason, one safe next action, stable unique `selector`,
urgency, age, model/provider/account, quota/reset, last event, separate token
and monetary provenance, context, parent, branch, and delivery reference when
the composer supplies them. Renderers must use `to_dict()` rather than raw facts
so secret-shaped keys are excluded. No durable record is needed: every fact is
reconstructable from the native owners above.
