# Stall detection handoff

- Branch: `cp/stall-detection`, based on `origin/main`.
- The bridge supervisor scans enabled sessions. A running non-dev-server execution
  with no new normalized event snapshot for `SIGHTMESH_STALL_THRESHOLD_MINUTES`
  (default 30) is stopped through cdesktop's normal killed-child recovery path.
- Active process/tool records in the normalized snapshot suppress detection;
  parent wakeups use the existing `parent_session_id` edge and a process-scoped
  durable dedupe key.
- Only spawned children with a parent edge are eligible. Detection runs for all
  active children even when their workspace opted out of Repowire routing.
- The first snapshot is only a warm baseline; partial snapshots thereafter can
  prove silence, while active tool/process events reset the clock. The managed
  LaunchAgent receives a validated 1-1440 minute threshold environment value;
  invalid values safely use 30 minutes.
- Stop attempts distinguish retryable intent from durable `accepted` and
  `uncertain` outcomes. Accepted/ambiguous calls only poll authoritative
  process state (with bounded escalation) and never repeat the stop;
  definitive structured rejection remains retryable. Parent wakes happen only
  after a confirmed terminal state and retain their durable process dedupe key.
- Crash recovery reuses deterministic `stall:<process-id>:stop` keys. It
  requires cdesktop's stop endpoint to dedupe that key per execution process
  and return the original outcome, so a restarted bridge can safely replay a
  `stopping` request without another destructive stop. This is a separate
  cdesktop service contract; this workspace does not modify that repository.
- cdesktop PR #8 refines keyed outcomes: 424 `Interrupted` is terminal
  unknown causality, so SightMesh makes no further stop call and wakes the
  durable parent path; 409 `Rejected` proves no side effect, so the next
  recovery rotates to a fresh attempt key; 425 retries that same pending key.
- No `2min verify / 30min silence` rule was present in tracked repository text.
