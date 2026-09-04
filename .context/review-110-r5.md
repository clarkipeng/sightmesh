# PR #110 — Round 5 review

**Verdict: APPROVE. P-level findings: none.**

Reviewed exact head `86afe5684bf121b7bf1a156d63f28548480cc803` against `main`. `git diff 88bdf31 86afe56` is limited to the stated admission correction: `send_all` accepts only `state == active` both before and inside its fence, reports the non-active state in its typed refusal, and adds the five-state table test. `origin/main` is an ancestor of the reviewed head.

Validation passed at the exact head:

- `uv run --with-editable . --with pytest pytest -q tests/test_sdk.py::test_only_an_active_task_accepts_mail` — 5 passed. Reverting the SDK admission condition locally made only the `blocked` case fail with “DID NOT RAISE”; the exact head was restored cleanly.
- Round-3/4 races: `test_send_after_terminal_snapshot_is_durably_cancelled_before_capacity_releases` and `test_cancel_during_request_build_never_issues_a_native_launch` — 2 passed.
- Simulator: `uv run --with-editable . --with pytest pytest -q tests/simulator` — 71 passed.
- Full suite: `uv run --with-editable . --with pytest pytest -q` — 699 passed.
- Exact-head GitHub compatibility runs 33883412796 (push) and 33883416464 (PR) are green across the pinned artifact, Python 3.11/3.12/3.13, and advisory edge checks.

Caller audit found managed task mail only through `SightMesh.send/send_all` and the task CLI. The terminal parent-wake path uses `NativeCommandQueue.notify_parent` directly to a live successor, so it does not rely on sending to a blocked managed task. `SightMesh.replace()` remains the explicit resume path. Rounds 1–5 close the kernel side of #105/#108.
