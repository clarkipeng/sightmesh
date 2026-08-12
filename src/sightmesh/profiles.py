from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROFILE_VERSION = 1
EXECUTORS = {"CLAUDE_CODE", "CODEX"}
CREDENTIAL_KINDS = {"ambient", "api", "enterprise"}
AUTO_FAILOVER_KINDS = {"api", "enterprise"}


class ProfileError(RuntimeError):
    pass


def default_profile_path() -> Path:
    return Path.home() / ".config" / "sightmesh" / "profiles.json"


@dataclass(frozen=True)
class Profile:
    name: str
    executor: str
    provider_id: str
    credential_kind: str = "ambient"
    model: str | None = None
    reasoning: str | None = None
    automatic_failover: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ProfileError("Profile name must not be empty")
        if self.executor not in EXECUTORS:
            raise ProfileError(f"Unsupported executor: {self.executor}")
        if not self.provider_id.strip():
            raise ProfileError("Profile must reference a cdesktop provider id")
        if self.credential_kind not in CREDENTIAL_KINDS:
            raise ProfileError(f"Unsupported credential kind: {self.credential_kind}")
        if self.automatic_failover and self.credential_kind not in AUTO_FAILOVER_KINDS:
            raise ProfileError(
                "Automatic failover is allowed only for explicitly configured API or "
                "enterprise profiles, never ambient consumer subscriptions"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_profile_path()

    def list(self) -> list[Profile]:
        payload = self._read()
        profiles = payload.get("profiles", {})
        if not isinstance(profiles, dict):
            raise ProfileError(f"Invalid profile registry: {self.path}")
        return [self._decode(name, value) for name, value in sorted(profiles.items())]

    def get(self, name: str) -> Profile:
        for profile in self.list():
            if profile.name == name:
                return profile
        raise ProfileError(f"Unknown SightMesh profile: {name}")

    def set(self, profile: Profile) -> Profile:
        payload = self._read()
        profiles = payload.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            raise ProfileError(f"Invalid profile registry: {self.path}")
        value = profile.to_dict()
        value.pop("name")
        profiles[profile.name] = value
        self._write(payload)
        return profile

    def remove(self, name: str) -> Profile:
        payload = self._read()
        profiles = payload.get("profiles", {})
        if not isinstance(profiles, dict) or name not in profiles:
            raise ProfileError(f"Unknown SightMesh profile: {name}")
        profile = self._decode(name, profiles.pop(name))
        self._write(payload)
        return profile

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": PROFILE_VERSION, "profiles": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(
                f"Cannot read profile registry {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != PROFILE_VERSION:
            raise ProfileError(f"Unsupported profile registry version: {self.path}")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".profiles.", dir=self.path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _decode(name: str, value: Any) -> Profile:
        if not isinstance(value, dict):
            raise ProfileError(f"Invalid profile entry: {name}")
        try:
            return Profile(name=name, **value)
        except TypeError as exc:
            raise ProfileError(f"Invalid profile entry {name}: {exc}") from exc


def provider_summary(provider: dict[str, Any]) -> dict[str, Any]:
    per_agent = provider.get("perAgentEnabled")
    models = provider.get("enabledModels")
    return {
        "id": provider.get("id"),
        "name": provider.get("name"),
        "kind": provider.get("kind"),
        "enabled": bool(provider.get("enabled")),
        "preset_id": provider.get("presetId"),
        "per_agent_enabled": per_agent if isinstance(per_agent, dict) else {},
        "models": [
            {"id": model.get("id"), "display_name": model.get("displayName")}
            for model in models or []
            if isinstance(model, dict)
        ],
        "credentials_present": {
            "provider": bool(provider.get("apiKey")),
            "claude": bool((provider.get("claude") or {}).get("apiKey")),
            "codex": bool((provider.get("codex") or {}).get("apiKey")),
        },
    }


def validate_provider(profile: Profile, providers: list[dict[str, Any]]) -> None:
    provider = next(
        (item for item in providers if item.get("id") == profile.provider_id), None
    )
    if not provider:
        raise ProfileError(
            f"Profile {profile.name} references missing cdesktop provider {profile.provider_id}"
        )
    if not provider.get("enabled"):
        raise ProfileError(
            f"Profile {profile.name} references a disabled cdesktop provider"
        )
    if provider.get("kind") != "Default":
        enabled = (provider.get("perAgentEnabled") or {}).get(profile.executor)
        if enabled is not True:
            raise ProfileError(
                f"Provider {profile.provider_id} is not enabled for {profile.executor}"
            )
