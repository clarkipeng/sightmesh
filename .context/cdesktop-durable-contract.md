# cdesktop durable delivery boundary

Verified against cdesktop `0.2.5` and the `0.2.6` source at commit
`62cbae3d`.

cdesktop 0.2.6 owns durable `session_commands` rows and exposes their history,
process-scoped requeue, and explicit dispatch. Its native states are `pending`,
`claimed`, `done`, `failed`, and `cancelled`; claimed rows carry an
`execution_process_id`, and keyed follow-ups are unique per session. SightMesh
therefore derives `queued`, `claimed`, `observed/running`, `terminal`, and
`rejected` from those rows and execution records. It does not copy them.

The native API does not expose an expiry fact or mutation. It also does not
expose a general command metadata/recovery mutation, so SightMesh cannot
durably record a retry counter on a command. The safe fallback is bounded by
the keyed stop record cdesktop owns: replay the same stop key for HTTP 425,
rotate only after a definitive HTTP 409 rejection, and stop retrying after an
accepted or causally interrupted result. `expired` remains unimplemented until
cdesktop supplies a native deadline/expiry state.

cdesktop 0.2.5 does not expose the command history/requeue/dispatch boundary;
manager reconciliation therefore requires 0.2.6. The 0.2.6 routes are:

- `GET /api/sessions/{session_id}/commands`
- `POST /api/sessions/{session_id}/commands/requeue` with
  `execution_process_id`
- `POST /api/sessions/{session_id}/commands/dispatch`

Parent wake-up uses an ordinary keyed cdesktop follow-up. The key is derived
from the child command ID and terminal state, so restart reconciliation can
observe the same terminal row repeatedly without creating another parent
command or turn.
