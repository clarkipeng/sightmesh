# cdesktop task launch contract v1

This contract is the capability boundary between SightMesh task ownership and
cdesktop's native workspace, session, transcript, and executor ownership. A
SightMesh task launch must fail closed until the pinned cdesktop advertises the
complete capability below. The legacy workspace-start endpoint is not a
fallback for a task-ID launch.

Implementation and runtime-lock progress are tracked in [SightMesh issue
#68](https://github.com/clarkipeng/sightmesh/issues/68). The cdesktop repository
currently has GitHub Issues disabled, so that issue is the durable external
dependency record rather than a pretend in-repository cdesktop ticket.

## Capability

`GET /api/info` advertises:

```json
{
  "capabilities": {
    "task_launch": {
      "contract_version": 1,
      "features": [
        "create_or_return",
        "lookup",
        "typed_outcomes",
        "content_addressed_history"
      ],
      "writer_limits": [
        "transcript_bytes",
        "fork_bytes",
        "free_disk_bytes"
      ]
    }
  }
}
```

Partial or later incompatible capability advertisements are unsupported. This
makes a runtime-lock upgrade an explicit release prerequisite.

## Identity and operations

`POST /api/task-launches` atomically creates or returns one native effect for:

```json
{
  "contract_version": 1,
  "task_id": "stable logical task",
  "incarnation_generation": 2,
  "attempt_id": "durable authorization ID",
  "idempotency_key": "task-launch:task-id:2:attempt-id",
  "launch_fingerprint": "sha256 of canonical launch parameters",
  "launch": {"executor-owned launch parameters": "..."}
}
```

`GET /api/task-launches/by-key?idempotency_key=...` returns the same record, or
404 if cdesktop has never accepted the key. SightMesh performs this lookup
before every create-or-return call, including recovery from an ambiguous
timeout or crash. Reservation or capability rotation in SightMesh never
authorizes blind native recreation.

Requests are at least once. cdesktop guarantees one logical effect per
idempotency key. SightMesh computes the lowercase SHA-256 fingerprint from
canonical sorted-key compact JSON and verifies it on lookup. Reusing a key with
different task, generation, attempt, fingerprint, or launch parameters returns
409. A generation older than cdesktop's accepted
generation for the task is rejected. Concurrent callers converge on the same
record. A new creation attempt is counted by SightMesh only when a distinct
durable attempt ID is authorized; lookup and return of an existing effect do
not consume an attempt.

## Result and terminal evidence

Both operations return a version-1 record with the request identity plus:

- `phase`: `pending`, `active`, `terminal`, or `refused`;
- `effect`: `created`, `existing`, or `none`;
- native `workspace_id` and `session_id` when active;
- optional `history_ref` formatted as `sha256:<64 lowercase hex>`;
- optional typed `outcome` with `kind`, and opaque `provider_id`, `account_id`,
  and numeric `retry_at` when applicable.

Terminal outcome kinds are `completed`, `failed`, `quota_exhausted`,
`approval_timeout`, `storage_refused`, and `lost`. `terminal` and `refused`
records require an outcome. SightMesh routes these fields without inferring
quota, account, or completion from transcript text.

The history reference is content addressed and opaque to SightMesh. Transcript
bytes and forked rollout data remain in cdesktop/executor storage and are never
copied into SightMesh state.

## Writer safety

cdesktop and its executor must enforce configured maximum transcript bytes,
fork bytes, and minimum free disk before a harmful write or history copy. A
`storage_refused` outcome is valid only with `refused_before_write: true`.
SightMesh can park and surface the refusal but cannot enforce a limit after the
writer has consumed the disk.

An unavailable, malformed, conflicting, stale-generation, or ambiguous result
never falls through to legacy spawn. SightMesh keeps the durable task attempt
non-active and parks the reason through its existing escalation path with
bounded retries.
