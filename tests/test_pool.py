from __future__ import annotations

import json
from pathlib import Path

import pytest

from sightmesh.pool import core


@pytest.fixture
def pool_root(monkeypatch, tmp_path: Path) -> Path:
    """Keep every test off the operator's real ~/.config/agent-pool."""
    root = tmp_path / "agent-pool"
    monkeypatch.setattr(core, "default_pool_root", lambda: root)
    return root


def _claude_token(body: str = "a" * 120) -> str:
    return f"sk-ant-oat01-{body}"


# ---------------------------------------------------------------- token intake


def test_wrapped_two_line_token_is_joined_into_one_value() -> None:
    # Regression guard. `claude setup-token` prints the token wrapped across two
    # terminal lines (70 chars then 38), so a real paste always arrives with an
    # interior newline. Stripping only the ends leaves that break embedded in the
    # stored secret, which then fails authentication in a way that looks like a
    # revoked token rather than a copy error. Every whitespace run must collapse.
    first = "sk-ant-oat01-" + "a" * 57
    second = "b" * 38
    assert len(first) == 70

    token = core.normalize_token(f"{first}\n{second}\n")

    assert token == first + second
    assert "\n" not in token
    assert core.validate_claude_token(token) is None


def test_normalize_token_collapses_every_whitespace_form() -> None:
    # Terminals and clipboards introduce \r\n, stray spaces, and trailing tabs
    # depending on how the two wrapped lines were selected. All of them have to
    # disappear, not just the leading and trailing ones.
    raw = f"  sk-ant-oat01-{'a' * 60}\r\n \t{'b' * 60}  \n"

    assert core.normalize_token(raw) == f"sk-ant-oat01-{'a' * 60}{'b' * 60}"


def test_truncated_token_is_rejected_with_the_second_line_hint() -> None:
    # The failure this guards is silent: copying only the first wrapped line
    # yields a token that looks structurally perfect - right prefix, right
    # alphabet - but is short. Storing it produces a confusing auth failure much
    # later, so length is validated at intake and the message names the cause.
    truncated = "sk-ant-oat01-" + "a" * 40

    problem = core.validate_claude_token(truncated)

    assert problem is not None
    assert "wrapped across two lines" in problem
    assert truncated not in problem


def test_validate_claude_token_rejects_wrong_prefix_and_alphabet() -> None:
    # A pasted API key or an OAuth token with shell escaping still has to fail
    # closed, otherwise it is written to disk and only fails at launch time.
    assert core.validate_claude_token("") == "no token provided"
    assert "sk-ant-oat01-" in core.validate_claude_token("sk-ant-api03-" + "a" * 120)
    assert core.validate_claude_token("sk-ant-oat01-" + "a" * 118 + "$!") == (
        "token contains unexpected characters"
    )


# ---------------------------------------------------------------- secrets at rest


def test_stored_token_is_private_to_the_operator(pool_root: Path) -> None:
    # Credentials live outside the repository and must not be group or world
    # readable. The directory matters as much as the file: a 0755 parent lets
    # another local account enumerate which accounts exist.
    core.write_token("work", _claude_token())

    path = core.token_path("work")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert pool_root in path.parents
    assert core.read_token("work") == _claude_token()


def test_shape_describes_a_secret_without_revealing_it() -> None:
    # Every status surface prints shapes rather than values, so this is the
    # function that keeps a token out of logs, terminal scrollback, and the UI.
    token = _claude_token()

    rendered = core.shape(token)

    assert token not in rendered
    assert token[:12] not in rendered
    assert "len=133" in rendered
    assert core.fingerprint(token)[:12] in rendered


def test_saved_pool_records_a_fingerprint_and_never_the_token(pool_root: Path) -> None:
    # The pool registry is plain JSON that gets read by the UI and by support
    # commands. It may reference an account but must never carry its secret.
    token = _claude_token()
    core.write_token("work", token)
    core.save_pool(
        {
            "accounts": [
                {
                    "id": "work",
                    "provider": "claude",
                    "kind": "oauth",
                    "token_fp": core.fingerprint(token),
                }
            ]
        }
    )

    encoded = core.pool_path().read_text(encoding="utf-8")
    assert token not in encoded
    assert core.fingerprint(token) in encoded
    assert core.pool_path().stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- identity


def test_same_claude_account_added_twice_is_detected_as_a_duplicate() -> None:
    # Two entries for one account silently defeat the whole point of a pool: the
    # "fallback" lands on the same exhausted quota. Identity, not account id, is
    # what makes them equal, so a second entry under a new name still collides.
    pool = {
        "accounts": [
            {
                "id": "personal",
                "provider": "claude",
                "kind": "oauth",
                "identity": {"email": "a@example.com", "orgId": "org-1"},
            }
        ]
    }
    candidate = {
        "id": "personal-again",
        "provider": "claude",
        "kind": "oauth",
        "identity": {"email": "a@example.com", "orgId": "org-1"},
    }

    duplicate = core.check_duplicate(pool, candidate)

    assert duplicate is not None
    assert duplicate["id"] == "personal"


def test_distinct_accounts_and_distinct_orgs_are_not_duplicates() -> None:
    # The same email can hold separate personal and organization seats with
    # separate quota, so the org is part of identity. Over-matching here would
    # wrongly block a legitimate second account.
    pool = {
        "accounts": [
            {
                "id": "personal",
                "provider": "claude",
                "kind": "oauth",
                "identity": {"email": "a@example.com", "orgId": "org-1"},
            }
        ]
    }

    other_org = dict(
        pool["accounts"][0],
        id="work",
        identity={"email": "a@example.com", "orgId": "org-2"},
    )
    other_user = dict(
        pool["accounts"][0],
        id="other",
        identity={"email": "b@example.com", "orgId": "org-1"},
    )

    assert core.check_duplicate(pool, other_org) is None
    assert core.check_duplicate(pool, other_user) is None


def test_codex_identity_reads_the_account_out_of_its_own_auth_json(
    tmp_path: Path,
) -> None:
    # Codex holds exactly one auth mode per CODEX_HOME, so the home directory is
    # the account boundary. Identity has to come from that home's auth.json or a
    # pool of Codex accounts cannot tell its members apart.
    import base64

    claims = {
        "email": "codex@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-123",
            "chatgpt_plan_type": "pro",
        },
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": f"header.{payload}.signature"}}),
        encoding="utf-8",
    )

    identity = core.codex_identity(str(home))

    assert identity["email"] == "codex@example.com"
    assert identity["chatgpt_account_id"] == "acct-123"
    assert identity["plan"] == "pro"


def test_ambient_claude_lookup_strips_inherited_token_env(monkeypatch) -> None:
    # Regression guard for a silent account misbinding. With any token env var
    # set, `claude auth status` reports authMethod "oauth_token" and no email, so
    # a newly minted token would be filed under whatever stray token was in the
    # environment - the wrong account, with no visible error. The lookup must run
    # in an environment scrubbed of all three variables.
    seen: dict[str, str] = {}

    class Result:
        returncode = 0
        stdout = json.dumps({"loggedIn": True, "email": "real@example.com"})
        stderr = ""

    def fake_run(cmd, env=None, **kwargs):
        seen.update(env or {})
        return Result()

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-stray")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stray")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stray")
    monkeypatch.setattr(core.subprocess, "run", fake_run)

    identity = core.ambient_claude_identity()

    assert identity["email"] == "real@example.com"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen
    assert "ANTHROPIC_AUTH_TOKEN" not in seen
    assert "ANTHROPIC_API_KEY" not in seen


def test_ambient_claude_lookup_reports_presence_without_an_account(monkeypatch) -> None:
    # `claude auth status` reports loggedIn on mere token presence, so loggedIn
    # alone must never be treated as a usable identity. Returning the reason
    # keeps this distinguishable from being logged out entirely.
    class Result:
        returncode = 0
        stdout = json.dumps({"loggedIn": True, "authMethod": "oauth_token"})
        stderr = ""

    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: Result())

    identity = core.ambient_claude_identity()

    assert "email" not in identity
    assert "oauth_token" in identity["error"]


# ---------------------------------------------------------------- ordering


def test_reorder_rewrites_priority_without_disturbing_the_other_provider() -> None:
    # Order is list position, and the two providers are interleaved in one list.
    # Reordering Claude must not move or drop the Codex entries sharing it.
    pool = {
        "accounts": [
            {"id": "claude-a", "provider": "claude"},
            {"id": "codex-a", "provider": "codex"},
            {"id": "claude-b", "provider": "claude"},
        ]
    }

    assert core.reorder(pool, "claude", ["claude-b", "claude-a"]) is None

    assert [a["id"] for a in pool["accounts"]] == ["claude-b", "codex-a", "claude-a"]
    assert [a["id"] for a in core.accounts_for(pool, "claude")] == [
        "claude-b",
        "claude-a",
    ]
    assert [a["id"] for a in core.accounts_for(pool, "codex")] == ["codex-a"]


def test_reorder_refuses_a_partial_list_instead_of_dropping_accounts() -> None:
    # A partial list used to be the easy way to silently lose an account from the
    # pool: it would keep its credential on disk but never be selected again.
    pool = {
        "accounts": [
            {"id": "claude-a", "provider": "claude"},
            {"id": "claude-b", "provider": "claude"},
        ]
    }

    error = core.reorder(pool, "claude", ["claude-b"])

    assert error is not None
    assert "exactly once" in error
    assert [a["id"] for a in pool["accounts"]] == ["claude-a", "claude-b"]


# ---------------------------------------------------------------- selection


def test_selection_skips_the_exhausted_account_and_takes_the_next(
    pool_root: Path, monkeypatch
) -> None:
    # The core behaviour of a pool. The first account reports zero remaining, so
    # selection must move to the second rather than probing or retrying the
    # exhausted one. Quota is consulted before the probe because an account can
    # still answer a request while its window reads zero.
    core.save_pool(
        {
            "accounts": [
                {
                    "id": "first",
                    "provider": "codex",
                    "kind": "chatgpt",
                    "codex_home": str(pool_root / "a"),
                },
                {
                    "id": "second",
                    "provider": "codex",
                    "kind": "chatgpt",
                    "codex_home": str(pool_root / "b"),
                },
            ]
        }
    )
    quotas = {
        "first": {"known": True, "remaining": 0, "resetsAt": "2099-01-01T00:00:00Z"},
        "second": {"known": True, "remaining": 62},
    }
    probed: list[str] = []

    monkeypatch.setattr(core, "quota", lambda account: quotas[account["id"]])
    monkeypatch.setattr(
        core,
        "probe",
        lambda account, timeout=90: (probed.append(account["id"]), (True, "ok"))[1],
    )

    chosen, notes = core.select("codex")

    assert chosen is not None
    assert chosen["id"] == "second"
    assert probed == ["second"]
    assert any("first" in note and "out of quota" in note for note in notes)


def test_exhausted_account_is_cooled_until_its_reported_reset(
    pool_root: Path, monkeypatch
) -> None:
    # Selection honours the provider's own reset time instead of retrying the
    # account. This is what keeps the pool from pushing requests at an account
    # that has already reported a limit.
    reset = "2099-01-01T00:00:00Z"
    core.save_pool(
        {
            "accounts": [
                {
                    "id": "first",
                    "provider": "codex",
                    "kind": "chatgpt",
                    "codex_home": str(pool_root / "a"),
                }
            ]
        }
    )
    monkeypatch.setattr(
        core,
        "quota",
        lambda account: {"known": True, "remaining": 0, "resetsAt": reset},
    )
    monkeypatch.setattr(core, "probe", lambda account, timeout=90: (True, "ok"))

    chosen, _ = core.select("codex")

    assert chosen is None
    cooldown = core.load_state()["cooldowns"]["first"]
    assert cooldown == pytest.approx(core.parse_iso(reset))


def test_selection_skips_an_account_with_no_stored_credential(
    pool_root: Path, monkeypatch
) -> None:
    # An account can be listed before its credential is added. It must be skipped
    # with a stated reason rather than selected and failed at launch.
    core.save_pool(
        {
            "accounts": [
                {"id": "empty", "provider": "claude", "kind": "oauth"},
                {"id": "ready", "provider": "claude", "kind": "oauth"},
            ]
        }
    )
    core.write_token("ready", _claude_token())
    monkeypatch.setattr(core, "quota", lambda account: {"known": False})
    monkeypatch.setattr(core, "probe", lambda account, timeout=90: (True, "ok"))

    chosen, notes = core.select("claude")

    assert chosen is not None
    assert chosen["id"] == "ready"
    assert any("empty" in note and "no credential" in note for note in notes)


def test_cooling_account_is_not_selected_until_the_cooldown_expires(
    pool_root: Path, monkeypatch
) -> None:
    # A cooldown recorded by one launch has to be respected by the next one, or
    # every launch would re-probe an account already known to be exhausted.
    core.save_pool(
        {
            "accounts": [
                {"id": "cooling", "provider": "claude", "kind": "oauth"},
                {"id": "warm", "provider": "claude", "kind": "oauth"},
            ]
        }
    )
    core.write_token("cooling", _claude_token())
    core.write_token("warm", _claude_token("b" * 120))
    core.set_cooldown("cooling", 3600)
    monkeypatch.setattr(core, "quota", lambda account: {"known": False})
    monkeypatch.setattr(core, "probe", lambda account, timeout=90: (True, "ok"))

    chosen, notes = core.select("claude")

    assert chosen is not None
    assert chosen["id"] == "warm"
    assert any("cooling" in note for note in notes)

    core.clear_cooldown("cooling")
    recovered, _ = core.select("claude")
    assert recovered is not None
    assert recovered["id"] == "cooling"


# ---------------------------------------------------------------- quota source


def test_quota_refuses_a_cached_source_from_another_account(monkeypatch) -> None:
    # Regression guard. `quota-axi` may answer from a cache populated by a
    # different CODEX_HOME. Trusting it makes one account's exhaustion look like
    # another's, which sends selection to the wrong account. Only a live oauth
    # source counts; anything else is reported as unknown.
    class Result:
        returncode = 0
        stdout = json.dumps(
            {
                "providers": [
                    {
                        "provider": "codex",
                        "source": "cache",
                        "windows": [{"label": "weekly", "percentRemaining": 0}],
                    }
                ]
            }
        )
        stderr = ""

    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: Result())

    usage = core.quota(
        {"id": "a", "provider": "codex", "kind": "chatgpt", "codex_home": "/tmp/x"}
    )

    assert usage["known"] is False
    assert "cache" in usage["reason"]


def test_quota_reads_the_limiting_window_from_a_live_oauth_source(monkeypatch) -> None:
    # The effective figure drives selection, and the reported reset must come
    # from the window that is actually limiting rather than the first listed.
    class Result:
        returncode = 0
        stdout = json.dumps(
            {
                "providers": [
                    {
                        "provider": "codex",
                        "source": "oauth",
                        "plan": "pro",
                        "windows": [
                            {
                                "label": "5h",
                                "percentRemaining": 80,
                                "resetsAt": "2099-01-01T00:00:00Z",
                            },
                            {
                                "label": "weekly",
                                "percentRemaining": 4,
                                "resetsAt": "2099-02-01T00:00:00Z",
                            },
                        ],
                        "quotaSemantics": {
                            "effectiveAvailability": [{"effectivePercentRemaining": 4}]
                        },
                    }
                ]
            }
        )
        stderr = ""

    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: Result())

    usage = core.quota(
        {"id": "a", "provider": "codex", "kind": "chatgpt", "codex_home": "/tmp/x"}
    )

    assert usage["known"] is True
    assert usage["remaining"] == 4
    assert usage["resetsAt"] == "2099-02-01T00:00:00Z"


def test_claude_quota_is_reported_unknown_rather_than_guessed() -> None:
    # Setup-tokens lack the user:profile scope, so /api/oauth/usage returns 403
    # for them. There is no honest per-account number to report, and inventing
    # one would make selection skip a perfectly usable account.
    usage = core.quota({"id": "a", "provider": "claude", "kind": "oauth"})

    assert usage["known"] is False
    assert "setup-token" in usage["reason"]


def test_api_key_account_is_reported_as_metered_not_exhausted() -> None:
    # A metered key has no window to run out of. Reporting it as unknown-but-
    # metered keeps selection from cooling it as though it were exhausted.
    usage = core.quota({"id": "a", "provider": "codex", "kind": "apikey"})

    assert usage["known"] is False
    assert usage["metered"] is True


# ---------------------------------------------------------------- launch env


def test_env_for_account_carries_only_that_accounts_own_credential(
    pool_root: Path,
) -> None:
    # Each worker runs on the selected account's own normal credentials. Codex
    # accounts are separated by CODEX_HOME because auth.json holds exactly one
    # auth mode, so the home is what keeps two accounts from overwriting login.
    core.write_token("claude-a", _claude_token())

    claude_env = core.env_for({"id": "claude-a", "provider": "claude", "kind": "oauth"})
    codex_env = core.env_for(
        {
            "id": "codex-a",
            "provider": "codex",
            "kind": "chatgpt",
            "codex_home": "/tmp/codex-a",
        }
    )

    assert claude_env == {"CLAUDE_CODE_OAUTH_TOKEN": _claude_token()}
    assert codex_env == {"CODEX_HOME": "/tmp/codex-a"}
    assert "OPENAI_API_KEY" not in codex_env


def test_account_without_a_credential_yields_no_launch_env(pool_root: Path) -> None:
    # An empty overlay is what makes selection skip the account instead of
    # launching a worker that would inherit some other ambient login.
    assert core.env_for({"id": "missing", "provider": "claude", "kind": "oauth"}) == {}


def test_limit_detection_matches_real_provider_refusals() -> None:
    # These strings are what selection reads to decide an account is exhausted.
    # Missing one means a limited account keeps being chosen and every launch on
    # it fails.
    assert core.looks_limited("Claude usage limit reached. Resets at 3pm")
    assert core.looks_limited("HTTP 429 Too Many Requests")
    assert core.looks_limited("insufficient_quota")
    assert not core.looks_limited("compilation finished with 2 warnings")


# ---------------------------------------------------------------- local UI


@pytest.fixture
def ui_server(pool_root: Path):
    """Run the pool UI on an ephemeral loopback port for the duration of a test."""
    import threading
    from http.server import ThreadingHTTPServer

    from sightmesh.pool import server as pool_server

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), pool_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_ui_serves_the_page_and_pool_state(ui_server: str, pool_root: Path) -> None:
    # The UI is packaged data rather than a generated string, so this also proves
    # ui.html is installed alongside the module and not left behind by packaging.
    import urllib.request

    core.save_pool(
        {"accounts": [{"id": "work", "provider": "claude", "kind": "oauth"}]}
    )

    with urllib.request.urlopen(f"{ui_server}/") as response:
        page = response.read().decode()
    with urllib.request.urlopen(f"{ui_server}/api/state") as response:
        state = json.loads(response.read())

    assert "<title>sightmesh pool</title>" in page
    assert [row["id"] for row in state["providers"]["claude"]] == ["work"]


def test_ui_state_never_carries_a_token_value(ui_server: str, pool_root: Path) -> None:
    # The page renders account health and quota, so its payload is the most
    # likely place for a secret to leak into a browser, an extension, or a
    # screenshot. It may carry the fingerprint but never the token.
    import urllib.request

    token = _claude_token()
    core.save_pool(
        {
            "accounts": [
                {
                    "id": "work",
                    "provider": "claude",
                    "kind": "oauth",
                    "token_fp": core.fingerprint(token),
                }
            ]
        }
    )
    core.write_token("work", token)

    with urllib.request.urlopen(f"{ui_server}/api/state") as response:
        body = response.read().decode()

    assert token not in body
    assert json.loads(body)["providers"]["claude"][0]["hasCredential"] is True


def test_ui_refuses_a_request_that_did_not_address_loopback(ui_server: str) -> None:
    # The UI adds and removes credentials with no authentication, so the Host
    # header is the only thing separating it from a page the operator happens to
    # be browsing. Without this check a hostile site could rebind DNS to
    # 127.0.0.1 and drive the API from the operator's own browser.
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{ui_server}/api/state", headers={"Host": "attacker.example.com"}
    )

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)

    assert caught.value.code == 403
