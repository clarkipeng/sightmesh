# PR #110 round-3 review — BLOCK

Reviewed `clarkipeng/sightmesh` PR #110 at exact head
`867ad237616ca5d0dcb25c1ff850fe980d9e4f74` against `origin/main`.
`origin/main` is an ancestor of that head.

## P1 — a concurrent `send()` can still create an untracked pending command after terminal cleanup

`src/sightmesh/sdk.py:262-288` reads the worker and posts its command without
the task lifecycle fence.  Terminalization snapshots the native commands at
`src/sightmesh/sdk.py:424-443`, records intents for only that snapshot, then
commits the terminal effect/task and eventually releases capacity.

Failure path:

1. `send_all()` reads an active task at line 266.
2. `cancel()` (or `complete()`/loss) snapshots an empty queue, records no
   command-cleanup intent, and terminalizes the task.
3. The already-authorized `send_all()` reaches `client.send()` at line 282,
   so cdesktop persists a new native `pending` command after the snapshot.
4. No durable cleanup row names that command.  `reconcile_cleanup_intents()`
   can only replay existing rows, and `count_running()` now excludes the
   terminal effect.  cdesktop can dispatch the new command once capacity
   frees: the #105 resurrection is still possible.

The durable intent repair fixes a command that existed in the terminal
snapshot, but not a command concurrently admitted after it.  The smallest
robust fix is to make command enqueue and terminalization one fenced protocol:
after an enqueue returns, re-read the task under its fence; if it became
terminal, snapshot/record durable cleanup for that command before returning.
If terminalization wins after the enqueue is published, its snapshot captures
the command; if it wins first, the enqueue-side compensation does.  Add a
two-thread regression that pauses `send()` after its active read and proves no
post-terminal queued row remains untracked.

## P2 — new cdesktop fakes do not enforce the requested external-I/O boundary

`tests/test_cdesktop.py:107-114` and `130-145` override `FakeClient.request()`
but omit `assert_external_io_allowed()`, unlike the base fake at lines 92-94
and the lifecycle fakes.  Thus the two new running-process endpoint tests can
pass even if their executor call is made while a task fence is held.  This does
not itself reopen a task, but it fails the review requirement that every
touched fake assert the boundary and leaves the new endpoint unguarded.

Smallest robust fix: call `assert_external_io_allowed()` in both overrides (and
invoke them through the appropriate external-I/O context, or factor the test
fake so it inherits the assertion).

## Verified behavior

The round-2 no-execution-id probe now passes: a terminal task with native
`pending` command `queued-1` is cancelled before capacity is released.  The
404 compatibility probe also passes: a fake route returning 404 leaves the
stored command-cancel intent `pending`, `count_running() == 1`, and a newly
constructed reconciler retries from the store and reaches cancellation; the
former `_cancelled` in-memory set is absent.  The claimed-execution stop/ack
probe passes.

Round-1/2 surface passed: loss sweep across scopes before admission, terminal
current-epoch effects excluded from admission count, expired reservations
reissued with a new epoch/specification, and activation's transient
running-process probe retried while preserving named blocker data.  New
terminal-cleanup executor calls are wrapped in `TaskFence.external_io`; the
P2 fake omissions are the remaining test-boundary exception.

Validation at the exact head:

- `uv run --python 3.13 --with pytest --with-editable . pytest -x -q` — 693 passed.
- `uv run --python 3.13 --with pytest --with-editable . pytest -q -m simulator` — 64 passed, 629 deselected.
- `uv run --python 3.13 --with pytest --with-editable . python -c 'import sightmesh; print(sightmesh.__file__)'` — `/private/tmp/sightmesh-pr110-r3/src/sightmesh/__init__.py`.
- Targeted round-2/3 and cdesktop tests — 54 passed.
- `git diff --check origin/main...867ad23` — clean.
- PR CI for remote exact head `867ad23` — 10/10 passed.

Verdict: **BLOCK** — P1 must be fixed before merge; P2 should be fixed with
the regression coverage.
