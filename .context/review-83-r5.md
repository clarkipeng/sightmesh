# PR #83 review — round 5

Reviewed `clarkipeng/sightmesh` PR #83 at exact head
`062028f76f93fbe784dbaa1f1fa6154118b7cfcf` against `origin/main` on
2026-09-03. `origin/main` is an ancestor of that commit, and
`origin/cp/observability` resolves to the same exact head.

## Verdict

**APPROVE — no P-level findings.**

Round 4's persistence leak is closed. I generated randomly nested JSON
payloads containing Bearer, Basic, `sk-ant-`, `ghp_`, `pypi-`, `xoxb-`,
`AKIA`, JWT-shaped, and URL-userinfo credential values, plus an opaque
high-entropy value under `apiKey`. Direct inspection of the SQLite
`escalations.message`, `acknowledgments.message`, and
`order_expectations.body` rows found none of those original values.
Checkpoint files likewise contained none. The intentionally neutral
high-entropy control remained visible, as did UUID, dedupe key, session ID,
Git SHA, and checkpoint file path controls in all persisted payloads; the
task SQLite row stores only the safe checkpoint reference.

The durable-write audit found each of the three message-table writers at
`src/sightmesh/escalation.py:646`, `:744`, and `:806`, immediately preceded
by the single redactor at `:640`, `:738`, and `:798`. The sole checkpoint
content writer is `src/sightmesh/sdk.py:283`, preceded by the same redactor
at `:273`; `TaskStore.checkpoint` receives only the relative reference.
No bypassing durable content writer was found by source grep.

Mutation proof: I locally disabled `_CREDENTIAL_VALUE_PATTERNS` after
`src/sightmesh/escalation.py:224`. The new neutral-key bearer regression
test failed with the unredacted value, then I restored the source exactly.

Verification:

- `uv run --with pytest pytest -q tests/test_escalation.py tests/test_sdk.py`:
  59 passed.
- `uv run --with pytest pytest -q`: 577 passed.
- Hosted checks for PR #83: 10 passed, 0 failed (including Python 3.11,
  3.12, and 3.13 test jobs), checked while the PR branch was at the exact
  reviewed head.
