# Bridge Delivery State

The cdesktop to Repowire bridge keeps a local SQLite delivery store at:

```text
~/.local/state/agent-deck/delivery.sqlite3
```

The store uses SQLite WAL mode and short process-safe transactions so bridge restarts and concurrent local inspectors see a consistent queue. Due records are atomically claimed before any cdesktop send, so competing local bridge workers cannot intentionally inject the same row at the same time.

## Record Format

Each inbound Repowire `ask`, `notify`, or `broadcast` is assigned a deterministic `idempotency_key`:

```text
sha256(target_session_id + message_type + (delivery_id or correlation_id))
```

If Repowire did not provide either identifier, the bridge uses a SHA-256 digest of the target session, message type, sender, and text as the source identifier. The table stores delivery metadata, retry counters, timestamps, status, the Repowire `delivery_id`, the `correlation_id`, and the cdesktop session target.

Statuses:

- `pending`: the bridge has not yet received a successful cdesktop follow-up acknowledgement.
- `inflight`: a local bridge worker owns a bounded send attempt using a private claim token.
- `injected`: cdesktop accepted the follow-up. Duplicate inbound deliveries acknowledge Repowire but are not injected again.
- `dead`: retry attempts were exhausted and the item needs operator inspection.

Delivery is locally deduplicated and at-least-once. It is not exactly-once: if the bridge crashes after cdesktop accepts a follow-up but before the SQLite `injected` commit, the claim eventually expires and the record can be retried.

## Retention

The bridge stores no Repowire auth material, provider credentials, account selection data, or headers.

The generated cdesktop follow-up prompt is retained only while local retry may still need it:

- `pending` records retain the prompt for automatic retry.
- `dead` records retain the prompt so an operator can explicitly retry the exact item.
- `injected` records clear the prompt and retain only metadata.

Operators should purge stale `dead` records after inspection or after deciding not to retry them.

## Capacity And Backoff

Pending records are bounded by count, aggregate prompt bytes, and individual prompt bytes. If capacity is exhausted, the bridge fails the Repowire delivery visibly with a `delivery_ack` status of `failed` and, for correlated asks, a structured Repowire error.

Transient cdesktop send failures are retried with capped exponential backoff. Exhausted records move to `dead` and remain inspectable.

Claims expire after 120 seconds by default. Expired `inflight` records return to `pending` without increasing `attempt_count`; attempts increase only after a claimed send returns an actual cdesktop failure. Only the holder of the current claim token may mark a record `injected` or failed.

## Commands

Read-only inspection:

```sh
agent-deck delivery status
agent-deck delivery list --status pending
agent-deck delivery list --status inflight
agent-deck delivery list --status dead --session-id <cdesktop-session-id>
```

Explicit operations require exact idempotency keys:

```sh
agent-deck delivery retry <idempotency-key>
agent-deck delivery purge <idempotency-key>
```

`retry` moves a retryable non-injected record back to `pending` and resets its retry counter. `purge` deletes only the exact keys provided.
