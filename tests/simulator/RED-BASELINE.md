# Red baseline

Run against pre-transplant code, commit `42c7736` (docs-only; identical
`src/` and `tests/` trees to `77f66eb`, the commit named in
`docs/kernel-spec.md`'s red-first requirement - `git diff --stat 77f66eb
42c7736 -- src/ tests/` is empty).

Command:

```
uv run --with pytest pytest tests/simulator -m simulator
```

## Result

```
9 failed, 3 passed in 3.67s
```

| id | result | notes |
|---|---|---|
| S1 | FAIL (required) | duplicate `complete()` overwrote the first completion's recorded result (`'late duplicate summary' == 'original summary'` failed) |
| S2 | FAIL (required) | `task_wakes` table does not exist yet |
| S3 | FAIL (required) | `task_effects` table does not exist yet |
| S4 | PASS (optional) | 100 concurrent `start()` calls on one key converged on exactly one native launch |
| S5 | FAIL (required) | 100 child completions produced 100 separate parent messages, not one wake (`100 == 1` failed) |
| S6 | FAIL (required) | blocked-child notification used `intent="replace"`, not `intent="continue"` |
| S7 | FAIL (required) | a raw self-parent insert into `managed_tasks` did not raise `sqlite3.IntegrityError` (no CHECK constraint yet, and FK enforcement is off) |
| S8 | FAIL (optional) | `sightmesh.task_store.StaleTransition` does not exist yet |
| S9 | PASS (optional) | child budget exceeded was rejected at `reserve_all`, before any native launch |
| S10 | FAIL (optional) | `task_effects` table does not exist yet, so no typed outcome can be recorded on the effect |
| S11 | PASS (optional) | `show(key)` made zero calls into the fake client's call log with 1,000 terminal tasks present |
| S12 | FAIL (required) | `sightmesh.task_store.StaleTransition` does not exist yet |

Required-red set from `docs/kernel-spec.md` ("Red-first requirement"): S1,
S2, S3, S5, S6, S7, S12 - all seven failed, exactly as required. S4, S8, S9,
S10, S11 are recorded honestly and were not forced either way.

## Full pytest output

```
$ uv run --with pytest pytest tests/simulator -m simulator -v

============================= test session starts ==============================
platform darwin -- Python 3.13.12, pytest-9.1.1, pluggy-1.6.0
rootdir: /private/tmp/sm-sim-work
configfile: pyproject.toml
collected 12 items

tests/simulator/test_scenarios.py::test_s1_duplicate_late_complete_on_a_blocked_task FAILED
tests/simulator/test_scenarios.py::test_s2_crash_between_state_change_and_notify_is_impossible_by_construction FAILED
tests/simulator/test_scenarios.py::test_s3_kill_between_native_launch_and_activation_retries_onto_the_same_effect FAILED
tests/simulator/test_scenarios.py::test_s4_a_hundred_concurrent_starts_on_one_key_launch_exactly_once PASSED
tests/simulator/test_scenarios.py::test_s5_a_hundred_duplicate_child_completions_produce_one_parent_wake FAILED
tests/simulator/test_scenarios.py::test_s6_one_child_blocking_mid_cohort_survives_with_intent_continue FAILED
tests/simulator/test_scenarios.py::test_s7_self_parent_insert_is_unrepresentable FAILED
tests/simulator/test_scenarios.py::test_s8_stale_epoch_writer_after_transfer_is_rejected FAILED
tests/simulator/test_scenarios.py::test_s9_child_budget_exceeded_is_rejected_at_reserve PASSED
tests/simulator/test_scenarios.py::test_s10_typed_429_is_recorded_as_a_typed_outcome_never_inferred_from_text FAILED
tests/simulator/test_scenarios.py::test_s11_show_with_a_thousand_terminal_tasks_performs_zero_fleet_scans PASSED
tests/simulator/test_scenarios.py::test_s12_two_concurrent_replace_on_one_task_yield_one_winner_one_stale FAILED

=================================== FAILURES ===================================
______________ test_s1_duplicate_late_complete_on_a_blocked_task _______________
E       AssertionError: assert 'late duplicate summary' == 'original summary'
E
E         - original summary
E         + late duplicate summary
tests/simulator/test_scenarios.py:53: AssertionError

_ test_s2_crash_between_state_change_and_notify_is_impossible_by_construction __
Failed: kernel v1 not implemented: task_wakes (atomic wake outbox) does not exist yet

_ test_s3_kill_between_native_launch_and_activation_retries_onto_the_same_effect _
Failed: kernel v1 not implemented: task_effects (effects journal) does not exist yet

____ test_s5_a_hundred_duplicate_child_completions_produce_one_parent_wake _____
E       AssertionError: assert 100 == 1
tests/simulator/test_scenarios.py:196: AssertionError

_____ test_s6_one_child_blocking_mid_cohort_survives_with_intent_continue ______
E       AssertionError: assert 'replace' == 'continue'
E
E         - continue
E         + replace
tests/simulator/test_scenarios.py:222: AssertionError

________________ test_s7_self_parent_insert_is_unrepresentable _________________
E       Failed: DID NOT RAISE IntegrityError
tests/simulator/test_scenarios.py:240: Failed

____________ test_s8_stale_epoch_writer_after_transfer_is_rejected _____________
Failed: kernel v1 not implemented: sightmesh.task_store.StaleTransition does not exist yet

__ test_s10_typed_429_is_recorded_as_a_typed_outcome_never_inferred_from_text __
Failed: kernel v1 not implemented: task_effects (effects journal) does not exist yet to
record a typed 429 outcome

____ test_s12_two_concurrent_replace_on_one_task_yield_one_winner_one_stale ____
Failed: kernel v1 not implemented: sightmesh.task_store.StaleTransition does not exist yet

=========================== short test summary info ============================
FAILED tests/simulator/test_scenarios.py::test_s1_duplicate_late_complete_on_a_blocked_task
FAILED tests/simulator/test_scenarios.py::test_s2_crash_between_state_change_and_notify_is_impossible_by_construction
FAILED tests/simulator/test_scenarios.py::test_s3_kill_between_native_launch_and_activation_retries_onto_the_same_effect
FAILED tests/simulator/test_scenarios.py::test_s5_a_hundred_duplicate_child_completions_produce_one_parent_wake
FAILED tests/simulator/test_scenarios.py::test_s6_one_child_blocking_mid_cohort_survives_with_intent_continue
FAILED tests/simulator/test_scenarios.py::test_s7_self_parent_insert_is_unrepresentable
FAILED tests/simulator/test_scenarios.py::test_s8_stale_epoch_writer_after_transfer_is_rejected
FAILED tests/simulator/test_scenarios.py::test_s10_typed_429_is_recorded_as_a_typed_outcome_never_inferred_from_text
FAILED tests/simulator/test_scenarios.py::test_s12_two_concurrent_replace_on_one_task_yield_one_winner_one_stale
========================= 9 failed, 3 passed in 3.67s ==========================
```

## Regression check

`uv run --with pytest pytest tests/ -x -q --ignore=tests/simulator` still
passes in full: `340 passed`. The simulator adds tests only; nothing
outside `tests/simulator/` was touched.

## Notes on scenario mechanics

- S2, S3, S8, S10, S12 fail today because they depend on kernel v1
  primitives that do not exist yet on this branch (`task_wakes`,
  `task_effects`, `sightmesh.task_store.StaleTransition`,
  `sightmesh.effects.EffectJournal`). Each of these tests does the schema
  or import check first and fails cleanly via `pytest.fail` (see
  `fail_missing_kernel_v1` in `conftest.py`) rather than erroring out of
  collection, so the same, unedited test body is expected to exercise the
  real mechanism and pass once the transplant lands.
- S1, S5, S6, S7 fail today for a directly observable behavioral reason
  that does not require guessing any new API surface: a blind `UPDATE` with
  no state/version guard (S1), one parent-mail message per child instead of
  one consolidated wake (S5), `intent="replace"` on a blocked child instead
  of `intent="continue"` (S6, the exact bug named in `docs/kernel-spec.md`),
  and no schema-level CHECK constraint against self-parenting (S7).
