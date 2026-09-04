# PR #83, round 4 — BLOCK

Reviewed `ac3ce175cdf77eb82f6b30ccb441aa18502d7356` against
`origin/main` (`e73c1f0ca8989c37fd99cb08f3b8bd6ccbd5890b`).
`git merge-base --is-ancestor origin/main ac3ce17` returned 0. GitHub reports
10/10 checks passing for this exact PR head (Python 3.11, 3.12, and 3.13 plus
pinned-artifact and advisory lanes).

## P1 — durable escalation payloads still retain bare bearer values and URL credentials

`src/sightmesh/escalation.py:215-260` walks JSON only by credential-shaped key.
It leaves scalar values below ordinary keys unchanged. The same redactor is
correctly called at every arbitrary-message persistence boundary:

| durable write | redaction boundary |
| --- | --- |
| `escalations.message` | `park`, `src/sightmesh/escalation.py:574-607`, calls `redact_credentials` at 585 |
| `acknowledgments.message` | `acknowledge`, `src/sightmesh/escalation.py:670-703`, calls it at 683 |
| `order_expectations.body` / digest | `expect_order`, `src/sightmesh/escalation.py:734-756`, calls it at 743 before hashing and insert |

The other SQLite writes in this PR's affected store persist validated routing
metadata only (launcher identity, fixed signal-condition tuples, lifecycle
state, and resolution timestamps); file writes introduced by the PR are
owner-only install/lease/state artifacts and do not accept an escalation/order
body. They are not alternate persistence paths for this payload.

Concrete failure: submit this valid JSON message through `park`,
`acknowledge`, or `expect_order`:

```json
{"route":{"session_id":"session-safe","dedupe_key":"dedupe-safe"},"items":[{"AUTHORIZATION":"Bearer nested_AUTHORIZATION_secret"},{"x-api-key":"nested_x_api_key_secret"},{"note":"Bearer bare_bearer_secret"},{"callback":"https://username:url_userinfo_password@example.test/path"}]}
```

The first two secrets are removed; `bare_bearer_secret` and
`url_userinfo_password` remain in all three SQLite rows. Thus a caller can
persist a credential verbatim by using a semantically neutral JSON field or
URL userinfo. This is the same durable-secret class as the previous blocking
finding, so this round remains blocked.

Smallest robust fix: extend the single recursive redactor to redact recognized
credential *values* wherever they occur (Bearer/Basic value forms and URL
userinfo), not only recognized keys; retain nonmatching scalars unchanged.
Add one table-driven boundary test that sends these value shapes through all
three methods and asserts both their absence from the database and preservation
of `dedupe_key` and `session_id`.

## Verification

Commands and outcomes:

* `uv run --with pytest pytest -q tests/test_escalation.py -k 'durable_escalation_records_never_retain_credential_values or durable_records_redact_nested_json_credentials_at_every_message_boundary'` — `3 passed, 23 deselected` at the exact head.
* I locally removed only the three calls at the `park`, `acknowledge`, and
  `expect_order` persistence boundaries, reran that command, and got
  `3 failed, 23 deselected`; then restored the exact head (`git diff --quiet --
  src/sightmesh/escalation.py` returned 0) and reran it successfully.
* An independent Python/SQLite probe with nested lists/dicts, mixed-case
  `AUTHORIZATION`, `x-api-key`, bare `Bearer` under `note`, and URL userinfo
  reported: nested-key secrets `false`, bare bearer `true`, URL password
  `true` for `persisted_in_all_boundaries`; `routing_fields_preserved: true`;
  `body_digest_matches_redacted_body: true`.
* `uv run --with pytest pytest -q tests/simulator` — `31 passed in 8.54s`.
* `uv run --with pytest pytest -q` — `574 passed in 21.33s`.
* `gh-axi pr checks 83` — `10 passed, 0 failed, 10 total`.

Verdict: **BLOCK** for the P1 durable credential disclosure above. The claimed
JSON-key redaction and the added locking tests work, but value-shaped bearer
credentials and URL-embedded credentials remain persistable at every durable
escalation boundary.
