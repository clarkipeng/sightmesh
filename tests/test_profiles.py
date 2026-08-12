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


def test_ambient_subscription_cannot_enter_automatic_failover() -> None:
    with pytest.raises(ProfileError, match="never ambient consumer subscriptions"):
        Profile(
            name="consumer",
            executor="CODEX",
            provider_id="default",
            credential_kind="ambient",
            automatic_failover=True,
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
