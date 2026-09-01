# Lane R: adversarial framework-consistency sweep

Scope: sightmesh `cdt/1615-lane-r-consisten` and cdesktop `main` at `/Users/clarkpeng/Documents/Code/cdesktop`. Confirmed findings are evidence-backed against the checked-in server route registrations, not a running release.

## Critical

1. **Confirmed — durable recovery calls three cdesktop routes the real server does not serve.** `src/sightmesh/cdesktop.py:258`, `src/sightmesh/cdesktop.py:268`, and `src/sightmesh/cdesktop.py:274` call `/sessions/{id}/commands`, `/commands/requeue`, and `/commands/dispatch`; cdesktop registers only `/sessions/{id}/queue` in `crates/server/src/routes/sessions/mod.rs:505-510` and `crates/server/src/routes/sessions/queue.rs:123-135`. The contract document claims the absent routes at `.context/cdesktop-durable-contract.md:21-27`, and `tests/test_cdesktop.py:307-318` fakes them.
   - Scenario: a server truthfully reporting 0.2.6 receives the bridge reconciler's command-history request and returns 404, so recovery/parent delivery never starts despite passing SightMesh tests.

2. **Confirmed — the recovery capability gate treats a version string as proof of the missing API surface.** `src/sightmesh/durable.py:35-42` and `256-274` enable reconciliation when `/info.version` parses as at least 0.2.6; that value is compiled package metadata (`cdesktop/crates/server/src/routes/config.rs:155-160`), not an attestation of the three required routes. The same source has none of those registrations (only two internal `dispatch_pending_commands` uses: `routes/sessions/mod.rs:350-353`, `routes/sessions/queue.rs:53-56`).
   - Scenario: a fork/rebuild keeps `CARGO_PKG_VERSION=0.2.6` while omitting the routes, passes the gate, and then fails every recovery call at runtime.

## High

3. **Confirmed — one terminal-child wake bypasses retirement quarantine.** `src/sightmesh/durable.py:435-463` correctly resolves a live successor before notifying a parent, but `_wake_parent_for_terminal_commands` sends directly to `parent` at `465-486`. cdesktop has no knowledge of SightMesh's local ownership records, so its keyed `follow-up` endpoint accepts that target (`cdesktop/crates/server/src/routes/sessions/mod.rs:169-174`, `319-353`).
   - Scenario: parent P is explicitly retired/superseded, a child command reaches `done`, and the periodic reconciliation injects `CHILD_DELIVERY` into P instead of its successor (or parks it).

4. **Confirmed — pool credentials are persisted and resolved long before launch.** `src/sightmesh/pool/core.py:12-13` explicitly defines on-disk token storage; `124-138` reads/writes `credentials/<id>.token`, and `400-405` resolves it while building an environment. This conflicts with the stated immediate-before-launch/no-persistence secret doctrine, regardless of the 0600 permissions.
   - Scenario: a setup token remains on disk after the launching process exits and can be read by any later process with that account's filesystem access.

## Medium

5. **Confirmed — quota state is inferred from arbitrary text, then persisted as routing truth.** `src/sightmesh/pool/core.py:33-41` defines regexes such as `rate.?limit` and `quota`; `529-553` classifies combined stdout/stderr with them and cools the account. This is string matching rather than provider-authenticated quota evidence, and the result is cached in `state.json` (`492-501`).
   - Scenario: a successful model/probe output that quotes “rate limit” is marked exhausted and skipped, even though the authoritative quota endpoint did not report exhaustion.

6. **Confirmed — the supposedly retired recovery store is still shipped as live compatibility machinery.** `src/sightmesh/bridge.py:25-27` imports `RecoveryIntentStore` with `# noqa: F401`; the complete persistent store, including confirmation-poll accounting, remains in `src/sightmesh/stalls.py:105-202`. That contradicts `.context/sightmesh-harness-convergence.md:11`, which says the store is retired from the active path.
   - Scenario: an extension imports the documented compatibility symbol and resumes writing a second recovery-accounting store alongside cdesktop command state, recreating the duplicate mechanism the convergence contract says was removed.

7. **Confirmed — ownership retirement is not coupled to the ordinary archive path.** Archive stops and archives the workspace, disables routing, and releases a lease at `src/sightmesh/cli.py:1824-1854`, but never records its sessions in `OwnershipStore`; that record is the only quarantine source (`src/sightmesh/succession.py:82-127`) used by the bridge (`src/sightmesh/bridge.py:275-281`).
   - Scenario: a race delivers a Repowire message after archive starts but before the bridge's next workspace refresh; there is no ownership quarantine record to reject the target session.

## Plausible

8. **Plausible — periodic reconciliation performs semantic stall detection and recovery rather than only durable wake handling.** The bridge polls every two seconds (`src/sightmesh/bridge.py:236-265`); the reconciler hashes normalized log entries to infer staleness (`src/sightmesh/durable.py:181-191`) and then stops/requeues the process (`317-355`, `496-530`). This looks like harness-side compensation for liveness/model behavior, although whether the intended doctrine permits this explicit, server-observation-based recovery needs an owner decision.
   - Scenario: a quiet but valid long-running model turn has an unchanged normalized snapshot past the threshold and is stopped/requeued by the harness.

STATUS: 7 confirmed, 1 plausible

## Operator dispositions (2026-08-21)

- F1/F2: artifacts of a stale audit base (spawn --base resolved a two-week-old local main; issue #37, Lane T fixing). The real routes exist on current main; the version-gate point stands conceptually and is mitigated by checksum-verified activation.
- F3: fix in flight, Lane S.
- F4: pool credential persistence -> issue #38 (needs explicit doctrine exception wording; founder call).
- F5: regex quota classification -> issue #39 (structured detectors first, regex as labeled fallback).
- F6: dead RecoveryIntentStore -> Lane S deletes it.
- F7: archive-without-retirement -> Lane T fixes it.
- F8 RULING: stall detection/requeue is delivery-layer infrastructure recovery (process liveness), not semantic compensation for model behavior - permitted by doctrine. The quiet-valid-turn scenario is bounded by is_active_suite_work plus the threshold; no change.
- New confirmed during the program: dispatch FK ordering (cdesktop #12, merged), non-atomic steer under load (interrupt landed, follow-up 500ed, session left killed), control-plane CLI reads time out under 4-worker load, spawn stale-base (#37).
