# SightMesh release candidate live review

Status: BLOCKING

Reviewed exact head: `6480a63712eda0b206f7671d7fe880986c1e32da`
Prior reviewed head: `f827f425a024813e8f29d39e24420fbc81fe1838`
Branch: `cdt/30c5-release-candidat`

## Scope

Reviewed the exact diff from prior reviewed head to the candidate head:

- `docs/operations.md`: 3 additions, 1 deletion
- `src/sightmesh/cli.py`: 117 additions, 33 deletions
- `src/sightmesh/fleet.py`: 4 additions, 2 deletions
- `tests/test_cli.py`: 100 additions, 21 deletions
- `tests/test_fleet.py`: 25 additions, 0 deletions

Source was not modified. This report is the only file written.

## Blocking finding

1. `overview` does not reliably select the latest eligible process per visible session.

   File/line: `src/sightmesh/cli.py:363-369`

   The candidate filters eligible processes but then returns `eligible[-1]`. The live cdesktop fleet does not guarantee eligible processes are already ordered by event time. Aggregate-only live proof at this head:

   - eligible sessions: 128
   - sessions with out-of-order eligible process event times: 12
   - sessions where `eligible[-1]` was not the max event-time process: 3
   - selected process total age behind the max event-time process across those mismatches: 377 seconds
   - mismatches with different status: 0

   Minimal reproduction:

   ```python
   from sightmesh.cli import _latest_process

   selected = _latest_process([
       {"id": "newer", "status": "completed", "completed_at": "2026-08-18T12:00:00Z"},
       {"id": "older", "status": "completed", "completed_at": "2026-08-18T11:59:00Z"},
   ])
   assert selected["id"] == "newer"
   ```

   Actual candidate behavior selects `older`, because it is the final eligible row. This blocks the required proof that overview selects one latest eligible process per visible session. The smallest robust fix is to select the eligible process with the greatest `_overview_event_time`, with a stable tie-breaker, rather than relying on native list order.

## Non-blocking evidence

The rest of the changed overview path is consistent with the intended privacy and schema boundaries:

- Active latest cards are retained before time filtering at `src/sightmesh/cli.py:1926-1930`.
- Default inactive retention uses `DEFAULT_OVERVIEW_HOURS = 24` and computes `cutoff = viewed_at or current_time - timedelta(hours=DEFAULT_OVERVIEW_HOURS)` at `src/sightmesh/cli.py:44` and `src/sightmesh/cli.py:1902-1903`.
- Explicit `--since` is parsed and passed as the cutoff at `src/sightmesh/cli.py:1972-1976`.
- Normalized snapshots are read only after the cutoff check at `src/sightmesh/cli.py:1926-1930`, so filtered historical processes are not snapshotted.
- Model comes from native execution action/config at `src/sightmesh/cli.py:1846-1853`.
- Provider ID is used only to join provider kind, and `account_id` remains `None`, at `src/sightmesh/cli.py:1887-1891`.
- Token/context data comes only from normalized `token_usage_info` entries and carries explicit provenance at `src/sightmesh/cli.py:1857-1885`.
- `quota` and `monetary_cost` are not inferred by the changed overview collection path.
- Public projection limits fields through `src/sightmesh/fleet.py:259-282`.

Changed tests cover most of these invariants:

- `tests/test_cli.py:804-923` proves active/latest grouping, native model/provider facts, null account/quota/cost, redaction of raw process/snapshot secrets, no default historical process in output, and no snapshot call for the filtered historical process.
- `tests/test_cli.py:925-932` proves explicit `--since` expands historical inactive failures and then snapshots the newly eligible process.
- `tests/test_fleet.py:44-55` proves `killed` is terminal while `failed` still needs attention.
- `tests/test_fleet.py:68-76` proves selectors can be stable on session identity across execution IDs.

## Live aggregate evidence

Default `uv run sightmesh --json overview` against the local fleet:

- total cards: 5
- groups: running 3, done_since_view 2, needs_attention 0
- status counts: running 3, completed 2
- stale failure-like cards older than 24h: 0
- populated field counts: model 2, provider 1, token_usage 5, context 5, branch 5, parent 0, delivery 0
- null field counts: account_id 5, quota 5, monetary_cost 5
- token usage rows with `cdesktop normalized snapshot` provenance: 5
- context rows with numeric pressure: 5
- provider-present rows with null account_id: 1

Explicit `uv run sightmesh --json overview --since 1970-01-01T00:00:00Z`:

- total cards: 128
- groups: running 3, done_since_view 124, needs_attention 1
- status counts: running 3, completed 124, failed 1
- stale failure-like cards older than 24h: 1
- populated field counts: model 120, provider 113, token_usage 126, context 126, branch 128, parent 68, delivery 0
- null field counts: account_id 128, quota 128, monetary_cost 128

Retention aggregate from live native processes:

- visible sessions: 128
- sessions with latest eligible process: 128
- expected retained active cards: 3
- expected retained inactive cards within default 24h: 2
- inactive latest eligible processes older than default 24h: 123
- latest processes without event time: 0
- default overview cards: 5
- duplicate selectors in overview: 0

## Checks

- `uv run --with pytest pytest tests/test_cli.py::test_overview_groups_native_processes_and_projects_private_fields tests/test_fleet.py::test_groups_deterministically_and_orders_attention_by_urgency_then_age tests/test_fleet.py::test_killed_is_terminal_while_failed_still_needs_attention tests/test_fleet.py::test_native_session_identity_keeps_selector_stable_across_executions tests/test_fleet.py::test_optional_facts_are_absent_without_affecting_projection tests/test_fleet.py::test_projection_carries_supplied_native_details_without_reading_them tests/test_fleet.py::test_tokens_and_external_cost_keep_separate_provenance_without_price_guessing`: passed, 7 tests.
- `uv run --with pytest pytest tests/test_fleet.py tests/test_cli.py -k overview`: passed, 1 selected test.
- `uv run --with ruff ruff check src/sightmesh/cli.py src/sightmesh/fleet.py tests/test_cli.py tests/test_fleet.py`: passed.
- `git diff --check f827f425a024813e8f29d39e24420fbc81fe1838..HEAD`: passed.
- Full `uv run --with ruff ruff check .`: failed on pre-existing lint findings outside the reviewed changed files (`scripts/bakeoff/run_bakeoff.py`, `scripts/migration-dry-run.py`, `src/sightmesh/bridge.py`, `src/sightmesh/migration.py`, `src/sightmesh/updates.py`, `tests/test_bakeoff.py`).
- Secret-pattern scan of ordinary and JSON overview output from `uv run sightmesh overview` and `uv run sightmesh --json overview`: 0 OpenAI/Anthropic key-like strings, 0 Bearer tokens, 0 JWT-like strings.

## Verdict

BLOCKING. The candidate satisfies the privacy/schema hardening checks reviewed here, but it cannot pass the required latest-process proof while `src/sightmesh/cli.py:363-369` depends on returned list order.
