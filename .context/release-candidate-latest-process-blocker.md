Authority: resume ownership of draft PR #16 from exact head `6480a63712eda0b206f7671d7fe880986c1e32da` and fix one independently reproduced blocker.

Finding: `src/sightmesh/cli.py` shared `_latest_process` filters eligible rows and returns `eligible[-1]`. Live audit found 12 of 128 visible sessions with out-of-order eligible event times and 3 sessions where that row was not the maximum event-time process. Minimal reproduction is an eligible list ordered `[newer completed_at, older completed_at]`; current code selects older.

Required root fix: make the shared latest-process invariant explicitly select the greatest native meaningful event time, with a deterministic tie-breaker and sensible missing-time behavior. Reuse one time parser for `_latest_process` and overview rather than maintaining two order definitions. Add focused unsorted, tie, dropped, devserver, and missing-time tests at the owning helper boundary. Prove peers, peek, status consumers retain compatible behavior. Rerun focused tests, full suite, Ruff, package smoke, live aggregate proof, and secret check. Push the exact checkpoint and update draft PR #16.

Exclusions: no merge or ready transition, no cdesktop changes, no new persistence, no source-order assumption, and no unrelated cleanup.

Stop when the independent reproduction passes, the live audit reports zero sessions where selection differs from the max event-time invariant, exact-head checks pass, and the branch is clean and pushed.
