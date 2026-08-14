import json

import pytest

from sightmesh.profiles import Profile, ProfileError, ProfileStore, provider_summary


def test_profile_store_round_trips_without_credentials(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    profile = Profile(
        name="work-api",
        executor="CLAUDE_CODE",
        provider_id="provider-a",
        credential_kind="api",
        model="sonnet",
        automatic_failover=True,
    )

    ProfileStore(path).set(profile)

    assert ProfileStore(path).get("work-api") == profile
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in json.dumps(payload).lower()
    assert path.stat().st_mode & 0o777 == 0o600


def test_ambient_subscription_can_enter_automatic_failover() -> None:
    # This asserts the reverse of what it used to. The old rule barred ambient
    # profiles from failover on the theory that a consumer subscription could
    # only be reached by borrowing someone's session. Credential pools removed
    # that premise: an ambient profile names an account the operator owns and
    # logged into through the provider's own interface, and a successor launches
    # on that account's own normal credentials. Kind now records how a profile
    # was authenticated, not whether it may be selected. Guarding this keeps a
    # future tightening of Profile validation from silently disabling pools of
    # Claude Max and Codex subscription accounts, which is the common case.
    profile = Profile(
        name="consumer",
        executor="CODEX",
        provider_id="default",
        credential_kind="ambient",
        automatic_failover=True,
    )

    assert profile.automatic_failover
    assert profile.credential_kind == "ambient"


def test_profile_still_rejects_unknown_credential_kind() -> None:
    # Lifting the failover restriction must not turn credential_kind into a free
    # text field - the enum is what keeps a typo from being persisted as a new
    # kind that no policy or docs describe.
    with pytest.raises(ProfileError, match="Unsupported credential kind"):
        Profile(
            name="typo",
            executor="CODEX",
            provider_id="default",
            credential_kind="ambiant",
        )


def test_provider_summary_redacts_secret_values() -> None:
    summary = provider_summary(
        {
            "id": "provider-a",
            "name": "API",
            "kind": "Custom",
            "enabled": True,
            "apiKey": "provider-secret",
            "claude": {"apiKey": "claude-secret"},
            "codex": {"apiKey": "codex-secret"},
            "enabledModels": [{"id": "model-a", "displayName": "Model A"}],
        }
    )

    encoded = json.dumps(summary)
    assert "secret" not in encoded
    assert summary["credentials_present"] == {
        "provider": True,
        "claude": True,
        "codex": True,
    }
