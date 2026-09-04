# PR #110 round-4 review — BLOCK

Reviewed `clarkipeng/sightmesh` PR #110 at exact head
`88bdf31478c6d6ab51292341c14668d35881ae11` against `origin/main`.
`origin/main` is an ancestor of that head.

## P1 — `send_all()` silently admits mail to a blocked task

`src/sightmesh/sdk.py:267` and `src/sightmesh/sdk.py:289` explicitly treat
`blocked` as mail-admissible. This violates the required admission contract:
reserved, blocked, and terminal tasks must receive a typed refusal, rather
than a silent drop or native enqueue.

Failure path:

1. A worker with a live cdesktop session reports a typed block (for example,
   an approval or unrecoverable execution outcome), transitioning its managed
   row to `blocked`.
2. A caller runs `send_all([Command(worker, prompt)])`.
3. Both preflight and fenced reload accept `blocked`; `client.send()` persists
   a continuation for work declared blocked, and `BatchResult.ok` is true.

An independent exact-head probe called `mesh.blocked(...)` and then
`send_all(...)`; it returned `ok` and recorded the native send. The smallest
robust fix is to make `active` the only mail-admissible lifecycle state in
both checks, retaining the existing `SightMeshError`/`BatchResult.errors`
path as the typed refusal. Add state-table coverage for reserved, blocked,
and each terminal state.

## Verified behavior

The round-3 post-snapshot race is now closed. A barrier probe paused native
enqueue until terminalization had snapshotted an empty queue: the outgoing
durable row kept `count_running() == 1`; after enqueue, it became a cleanup
intent, was acknowledged through the cancel endpoint, and no native pending
row remained. The mirror interleaving—terminalization after the durable send
record but before native enqueue—also finished with the only native row
cancelled and `count_running() == 0` only after acknowledgement.

The added running-process fake request overrides call
`assert_external_io_allowed()`, and inspected new executor calls are inside
`TaskFence.external_io()`. Round-1–3 coverage is present and passed: loss
sweep across scopes, exclusion of terminal current-epoch effects, durable
unacknowledged outgoing/cleanup intent capacity, reservation expiry/reissue,
named activation blockers with transient probe retry, and missing cancel
route (`404`) remaining unacknowledged.

Validation at the exact head:

- `uv run --python 3.13 --with pytest --with-editable . pytest -q` — 694 passed.
- `uv run --python 3.13 --with pytest --with-editable . pytest tests/simulator -m simulator -q` — 64 passed, 7 deselected.
- Editable import printed `/private/tmp/sightmesh-pr110-r4/src/sightmesh/__init__.py`.
- `git diff --check origin/main...88bdf31` — clean.
- Remote branch resolves to exact `88bdf31`; `origin/main` is its ancestor.
- Remote CI is not yet green: 7 passed, 3 pending (the three current Python
  test jobs; earlier duplicate jobs passed).

Verdict: **BLOCK** — P1 must be fixed before merge. The round-3 resurrection
and external-I/O-boundary findings are closed, but the required blocked-state
typed-refusal contract is not. Consequently rounds 1–4 do not yet close the
kernel side of #105/#108.
