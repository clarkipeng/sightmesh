# PR #83 round 3 adversarial review — BLOCK

Reviewed `cp/observability` at exact head `c96c0547ff10b0bad133d1581553aa078779c38d` against `origin/main` `5f07e610517c880c64328b7de53b2e41c5d98248` on 2026-09-03. `origin/main` is an ancestor of the reviewed head (merge-base is `5f07e610…`), so the branch is rebased. `gh-axi pr checks 83` reported 10 passed / 0 failed on that PR head.

## Finding

### P1 — JSON-shaped credentials still reach all three durable message records

`redact_credentials` only recognizes header syntax and unquoted `key=value` / `key:value` text. It does not recognize a structured credential-bearing field embedded in an otherwise valid JSON message, such as an `authorization` member. `EscalationStore.park`, `.acknowledge`, and `.expect_order` all send their free-form persisted value through that incomplete redactor at [src/sightmesh/escalation.py:537](../src/sightmesh/escalation.py#L537), [src/sightmesh/escalation.py:635](../src/sightmesh/escalation.py#L635), and [src/sightmesh/escalation.py:695](../src/sightmesh/escalation.py#L695); the faulty matcher is [src/sightmesh/escalation.py:198](../src/sightmesh/escalation.py#L198).

Concrete failure path: a cdesktop-originated callback/error payload represented as JSON contains `{"authorization":"Bearer <secret>"}`. It is parked when no parent is live, acknowledged after delivery, or retained as an order body. Each durable SQLite column stores the raw secret. Direct head probe confirmed `secret_persisted=True` for `escalations.message`, `acknowledgments.message`, and `order_expectations.body` (the secret itself was not printed). Existing locking coverage only uses line-style `Authorization:` values, so it misses the structured path.

Smallest robust fix: make the durable boundary use one structured redactor that recursively replaces values of credential-bearing map keys (including `authorization`, `cookie`, token/secret/password/credential/API-key variants) before serializing recognized JSON, while retaining the header-line redaction for prose. Apply it uniformly to all durable free-form message/body writes and add persisted-record tests for JSON nested objects and arrays, not only header text.

## Required verification

All commands ran at the exact reviewed head unless noted.

- Mutation: reverse only `c96c054` source hunks for `escalation.py`, `updates.py`, and `cli/diagnostics.py`, retaining head tests; `uv run --with pytest pytest -q` on the added credential, parked-newest, doctor non-object, and update-state tests returned **7 failed**. The failures included both park/ack credential tests, newest parked row (`child-99` rather than `child-249`), and array/null doctor/update handling. Forward-applied the same patch; working tree clean.
- Mutation: removed the positive guard in `_task_limit`, then ran `uv run --with pytest pytest -q tests/test_observability.py::test_task_limit_must_be_positive_for_every_bounded_surface`; **1 failed** (the code attempted a read with limit 1 instead of rejecting zero). Restored `src/sightmesh/cli/diagnostics.py` from `c96c054`; working tree clean.
- Head locking tests: `uv run --with pytest pytest -q tests/test_escalation.py::test_durable_escalation_records_never_retain_credential_values tests/test_observability.py::test_unacked_deliveries_keep_the_newest_parked_escalations_at_the_bound tests/test_observability.py::test_doctor_treats_non_object_update_state_as_unreadable tests/test_updates.py::test_read_state_rejects_valid_json_that_is_not_an_object` → **7 passed**. They lock the claimed header, newest-N, and `[]`/`null` repairs, but not the P1 structured credential path.
- Focused changed-area suite: `uv run --with pytest pytest -q tests/test_cli.py tests/test_escalation.py tests/test_local_installer.py tests/test_observability.py tests/test_updates.py` → **158 passed** (not the claimed 189; this is the count produced by the current exact-head command).
- Refuse-not-steal and drain-first reprobes: `uv run --with pytest pytest -q tests/test_local_installer.py::test_install_refuses_to_steal_a_skill_link_it_does_not_own tests/test_local_installer.py::test_install_refuses_before_it_installs_anything tests/test_updates.py::test_activation_drains_first_then_reports_a_bounded_timeout tests/test_updates.py::test_activation_converges_once_the_drain_empties_the_fleet tests/test_updates.py::test_activation_re_arms_the_drain_inside_the_executor_ttl tests/test_updates.py::test_activation_keeps_the_bridge_up_until_the_fleet_has_drained` → **6 passed**.
- Update-state shape probe: object, array, null, string, number, boolean. The object followed ordinary skew evaluation; each non-object returned the stable unreadable-state result without a crash. This covers every JSON top-level shape a cdesktop state file can emit. The parser check is [src/sightmesh/updates.py:69](../src/sightmesh/updates.py#L69).
- Simulator: `uv run --with pytest pytest -q tests/simulator` → **25 passed**.
- Full suite: `uv run --with pytest pytest -q` → **474 passed**.

Verdict: **BLOCK** for the P1 durable credential leak above. The prior doctor-shape, newest-N parked escalation, zero-limit, refuse-not-steal, and drain-first issues are fixed and independently exercised; CI is green, but the durable persistence guarantee remains false for structured credential fields.
