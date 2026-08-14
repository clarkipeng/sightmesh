"""Credential pool model, identity, quota, and selection.

A pool is an ordered list of accounts the operator owns, per provider. Order is
list position - there are no tier numbers to collide. Selection prefers real
quota data over probing, because an account can answer a request while its
weekly window sits at zero.

Each account is used with its own normal credentials. An exhausted account is
cooled until the provider's reported reset time rather than retried, so the pool
moves to the next owned account instead of pushing past a limit.

Secrets live in credentials/<id>.token (0600) outside the repository and are
never printed - only length and fingerprint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROBE_TTL = 60
QUOTA_TTL = 120
DEFAULT_COOLDOWN = 5 * 3600

LIMIT_PATTERNS = [
    r"usage limit reached",
    r"rate.?limit",
    r"limit reached",
    r"quota",
    r"insufficient[_ ]quota",
    r"resets? at",
    r"\b429\b",
]

PROVIDERS = ("claude", "codex")

POOL_VERSION = 2


class PoolError(RuntimeError):
    pass


# ---------------------------------------------------------------- storage


def default_pool_root() -> Path:
    """Pool state root, always outside the repository."""
    override = os.environ.get("AGENT_POOL_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "agent-pool"


def pool_path() -> Path:
    return default_pool_root() / "pool.json"


def state_path() -> Path:
    return default_pool_root() / "state.json"


def credentials_dir() -> Path:
    return default_pool_root() / "credentials"


def token_path(account_id: str) -> Path:
    return credentials_dir() / f"{account_id}.token"


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_pool() -> dict[str, Any]:
    pool = _read(pool_path(), {"version": POOL_VERSION, "accounts": []})
    # v1 used explicit tier numbers, which could collide. Order is now list
    # position, so tiers are dropped once and the existing order preserved.
    if pool.get("version", 1) < POOL_VERSION:
        pool["accounts"] = sorted(
            pool.get("accounts", []),
            key=lambda a: (a.get("tier", 99), a.get("id", "")),
        )
        for account in pool["accounts"]:
            account.pop("tier", None)
        pool["version"] = POOL_VERSION
        _write(pool_path(), pool)
    return pool


def save_pool(pool: dict[str, Any]) -> None:
    pool["version"] = POOL_VERSION
    _write(pool_path(), pool)


def load_state() -> dict[str, Any]:
    return _read(state_path(), {"cooldowns": {}, "probes": {}, "quota": {}})


def save_state(state: dict[str, Any]) -> None:
    _write(state_path(), state)


def read_token(account_id: str) -> str | None:
    try:
        return token_path(account_id).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_token(account_id: str, token: str) -> None:
    directory = credentials_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = token_path(account_id)
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    path.write_text(token.strip() + "\n", encoding="utf-8")


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.strip().encode()).hexdigest()[:12]


def shape(secret: str) -> str:
    """Describe a secret without disclosing it."""
    if not secret:
        return "empty"
    return f"len={len(secret)} fp={fingerprint(secret)}"


# ---------------------------------------------------------------- model


def accounts_for(
    pool: dict[str, Any], provider: str | None = None
) -> list[dict[str, Any]]:
    """Provider accounts in pool order - position is priority."""
    return [
        a
        for a in pool.get("accounts", [])
        if not provider or a.get("provider") == provider
    ]


def find(pool: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    return next(
        (a for a in pool.get("accounts", []) if a.get("id") == account_id), None
    )


def cooling_until(state: dict[str, Any], account_id: str) -> float:
    until = state.get("cooldowns", {}).get(account_id, 0)
    return until if until > time.time() else 0


def set_cooldown(account_id: str, seconds: int = DEFAULT_COOLDOWN) -> float:
    state = load_state()
    until = time.time() + seconds
    state.setdefault("cooldowns", {})[account_id] = until
    state.setdefault("probes", {}).pop(account_id, None)
    save_state(state)
    return until


def cool_until_timestamp(account_id: str, when: float) -> float:
    state = load_state()
    state.setdefault("cooldowns", {})[account_id] = when
    state.setdefault("probes", {}).pop(account_id, None)
    save_state(state)
    return when


def clear_cooldown(account_id: str) -> None:
    state = load_state()
    state.setdefault("cooldowns", {}).pop(account_id, None)
    state.setdefault("probes", {}).pop(account_id, None)
    save_state(state)


def fmt_delta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def parse_duration(text: str) -> int:
    """Parse a cooldown like `5h`, `30m`, or a bare number of seconds."""
    match = re.fullmatch(r"(\d+)([smhd]?)", text.strip())
    if not match:
        raise PoolError(f"Cannot read duration: {text}")
    scale = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int(match.group(1)) * scale


def parse_iso(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def looks_limited(text: str) -> bool:
    low = text.lower()
    return any(re.search(pattern, low) for pattern in LIMIT_PATTERNS)


# ---------------------------------------------------------------- identity


def identity_key(account: dict[str, Any]) -> str | None:
    """Stable value that is equal for two entries backed by the same account."""
    provider, kind = account.get("provider"), account.get("kind")
    ident = account.get("identity") or {}
    if provider == "claude":
        if ident.get("email"):
            return f"claude:{ident['email']}:{ident.get('orgId', '?')}"
        return (
            f"claude-token:{account['token_fp']}" if account.get("token_fp") else None
        )
    if provider == "codex" and kind == "chatgpt":
        return f"codex:{ident.get('chatgpt_account_id') or ident.get('email')}"
    if provider == "codex" and kind == "apikey":
        # auth.json is authoritative; the stored copy is only a fallback.
        return f"codex-key:{ident.get('key_fp') or account.get('token_fp')}"
    return None


def identity_label(account: dict[str, Any]) -> str:
    ident = account.get("identity") or {}
    email = ident.get("email")
    plan = ident.get("plan") or ident.get("subscription")
    if email and plan:
        return f"{email} ({plan})"
    if email:
        return email
    if account.get("kind") == "apikey":
        return f"api key {ident.get('key_fp') or account.get('token_fp') or '?'}"
    return "unidentified"


CLAUDE_TOKEN_RE = re.compile(r"^sk-ant-oat01-[A-Za-z0-9_\-]+$")
CLAUDE_TOKEN_MIN = 100


def normalize_token(raw: str) -> str:
    """Collapse all whitespace.

    `claude setup-token` prints the token wrapped across two terminal lines, so a
    copied token arrives with an interior newline. Stripping only the ends would
    leave that break in place; a line-at-a-time read would drop the tail entirely.
    """
    return re.sub(r"\s+", "", raw or "")


def validate_claude_token(token: str) -> str | None:
    """Reject a malformed token before it is stored. None means valid."""
    if not token:
        return "no token provided"
    if not token.startswith("sk-ant-oat01-"):
        return "expected a token starting with sk-ant-oat01-"
    if not CLAUDE_TOKEN_RE.match(token):
        return "token contains unexpected characters"
    if len(token) < CLAUDE_TOKEN_MIN:
        return (
            f"token is only {len(token)} characters. `claude setup-token` prints it "
            "wrapped across two lines - copy BOTH lines, not just the first."
        )
    return None


def ambient_claude_identity() -> dict[str, Any]:
    """Who `claude` is logged in as right now, in the default config dir.

    setup-token mints for the ambient session and the resulting token exposes no
    identity endpoint, so this is the only trustworthy binding available. On
    failure the reason is returned rather than swallowed - an empty result used
    to be indistinguishable from being logged out.

    Token env vars are stripped first: with one set, `claude auth status` reports
    authMethod "oauth_token" and no email at all, which would silently bind the
    new token to whatever account that stray token belongs to.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    }
    try:
        run = subprocess.run(
            ["claude", "auth", "status"],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "`claude auth status` timed out"}
    except FileNotFoundError:
        return {"error": "`claude` is not on PATH"}

    try:
        data = json.loads(run.stdout)
    except json.JSONDecodeError:
        detail = (run.stderr or run.stdout or "").strip()[:160]
        return {
            "error": f"could not read `claude auth status`: {detail or 'no output'}"
        }

    # `claude auth status` reports loggedIn on mere token presence, so the email
    # is what actually identifies the account.
    if not data.get("loggedIn"):
        return {"error": "`claude` is not logged in - run `claude auth login` first"}
    if not data.get("email"):
        return {
            "error": (
                f"`claude auth status` reported no account "
                f"(authMethod: {data.get('authMethod')}). Run `claude auth login`."
            )
        }
    return {
        "email": data.get("email"),
        "orgId": data.get("orgId"),
        "subscription": data.get("subscriptionType"),
    }


def codex_identity(codex_home: str) -> dict[str, Any]:
    """Read identity out of a Codex home's auth.json."""
    try:
        data = json.loads((Path(codex_home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tokens = data.get("tokens") or {}
    if tokens.get("id_token"):
        try:
            payload = tokens["id_token"].split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            auth = claims.get("https://api.openai.com/auth") or {}
            return {
                "email": claims.get("email"),
                "chatgpt_account_id": auth.get("chatgpt_account_id"),
                "plan": auth.get("chatgpt_plan_type"),
            }
        except (IndexError, ValueError, json.JSONDecodeError):
            pass
    key = data.get("OPENAI_API_KEY")
    if key and data.get("auth_mode") == "apikey":
        return {"plan": "api", "key_fp": fingerprint(key)}
    return {}


# ---------------------------------------------------------------- quota


def env_for(account: dict[str, Any]) -> dict[str, str]:
    """Environment that runs a worker on this account's own credentials."""
    env: dict[str, str] = {}
    provider = account.get("provider")
    if provider == "claude":
        token = read_token(account["id"])
        if not token:
            return {}
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    elif provider == "codex":
        home = os.path.expanduser(account.get("codex_home", ""))
        if not home:
            return {}
        # Codex holds exactly one auth mode per CODEX_HOME, so each account owns one.
        env["CODEX_HOME"] = home
        if account.get("kind") == "apikey":
            key = read_token(account["id"])
            if key:
                env["OPENAI_API_KEY"] = key
    return env


UNKNOWN_QUOTA = {"known": False, "reason": "no quota source"}


def quota(account: dict[str, Any]) -> dict[str, Any]:
    """Live quota for one account.

    Only Codex subscriptions expose a per-account source. API keys are metered,
    and Claude setup-tokens lack the user:profile scope so /api/oauth/usage
    returns 403 - both are reported honestly as unknown rather than guessed at.
    """
    if account.get("kind") == "apikey":
        return {"known": False, "metered": True, "reason": "metered billing"}
    if account.get("provider") == "claude":
        return {
            "known": False,
            "reason": "no per-account usage endpoint for setup-tokens",
        }

    home = os.path.expanduser(account.get("codex_home", ""))
    if not home:
        return dict(UNKNOWN_QUOTA)
    try:
        run = subprocess.run(
            ["quota-axi", "--provider", "codex", "--json"],
            env={**os.environ, "CODEX_HOME": home},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        data = json.loads(run.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return {"known": False, "reason": f"quota lookup failed: {exc}"}

    provider = next(
        (p for p in data.get("providers", []) if p.get("provider") == "codex"), None
    )
    if not provider:
        return dict(UNKNOWN_QUOTA)

    # A "cache" source is another account's numbers bleeding through; refusing
    # it is what keeps one account's exhaustion from being blamed on another.
    if provider.get("source") != "oauth":
        return {
            "known": False,
            "reason": f"no live source (got {provider.get('source')})",
        }

    windows = [
        {
            "label": w.get("label"),
            "remaining": w.get("percentRemaining"),
            "resetsAt": w.get("resetsAt"),
            "resetsIn": (parse_iso(w.get("resetsAt")) or 0) - time.time(),
        }
        for w in provider.get("windows", [])
    ]
    effective = (provider.get("quotaSemantics") or {}).get(
        "effectiveAvailability"
    ) or []
    remaining = min(
        (
            e.get("effectivePercentRemaining")
            for e in effective
            if e.get("effectivePercentRemaining") is not None
        ),
        default=None,
    )
    limiting = min(
        (w for w in windows if w["remaining"] is not None),
        key=lambda w: (w["remaining"], w["resetsIn"]),
        default=None,
    )
    return {
        "known": remaining is not None,
        "remaining": remaining,
        "plan": provider.get("plan"),
        "windows": windows,
        "resetsAt": (limiting or {}).get("resetsAt"),
        "resetsIn": (limiting or {}).get("resetsIn"),
    }


def quota_cached(account: dict[str, Any], force: bool = False) -> dict[str, Any]:
    state = load_state()
    entry = state.get("quota", {}).get(account["id"])
    if not force and entry and time.time() - entry.get("at", 0) < QUOTA_TTL:
        return entry["data"]
    data = quota(account)
    state = load_state()
    state.setdefault("quota", {})[account["id"]] = {"at": time.time(), "data": data}
    save_state(state)
    return data


# ---------------------------------------------------------------- probe


def probe(account: dict[str, Any], timeout: int = 90) -> tuple[bool, str]:
    """Real request against the account. Presence of a token proves nothing."""
    overlay = env_for(account)
    if not overlay:
        return False, "no credential stored"
    env = {**os.environ, **overlay}

    if account.get("provider") == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(default_pool_root() / "probe" / account["id"])
        cmd = ["claude", "-p", "ok"]
    else:
        cmd = ["codex", "exec", "--skip-git-repo-check", "ok"]

    try:
        run = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return False, f"{cmd[0]} not on PATH"
    except subprocess.TimeoutExpired:
        return False, "probe timed out"

    output = (run.stdout or "") + (run.stderr or "")
    if looks_limited(output):
        return False, "usage limit"
    if run.returncode == 0:
        return True, "ok"
    reason = next((ln for ln in output.strip().splitlines() if ln.strip()), "failed")
    return False, reason.strip()[:90]


def probe_cached(account: dict[str, Any]) -> tuple[bool, str]:
    state = load_state()
    entry = state.get("probes", {}).get(account["id"])
    if entry and time.time() - entry.get("at", 0) < PROBE_TTL and entry.get("ok"):
        return True, "ok (cached)"

    ok, reason = probe(account)
    state = load_state()
    state.setdefault("probes", {})[account["id"]] = {
        "at": time.time(),
        "ok": ok,
        "reason": reason,
    }
    save_state(state)
    if not ok and reason == "usage limit":
        set_cooldown(account["id"])
    return ok, reason


# ---------------------------------------------------------------- selection


def select(
    provider: str, verify: bool = True
) -> tuple[dict[str, Any] | None, list[str]]:
    """First account in order with credential, headroom, and a working probe."""
    pool, state = load_pool(), load_state()
    notes: list[str] = []

    for account in accounts_for(pool, provider):
        aid = account["id"]
        until = cooling_until(state, aid)
        if until:
            notes.append(f"skip {aid}: cooling {fmt_delta(until - time.time())}")
            continue
        if not env_for(account):
            notes.append(f"skip {aid}: no credential stored")
            continue

        # Quota is cheaper and more truthful than a probe: an account can answer
        # a request while its weekly window reads zero.
        usage = quota_cached(account)
        if usage.get("known") and usage.get("remaining") == 0:
            # Honour the provider's own reset instead of retrying the account.
            reset = parse_iso(usage.get("resetsAt"))
            if reset:
                cool_until_timestamp(aid, reset)
                notes.append(
                    f"skip {aid}: out of quota, resets in {fmt_delta(reset - time.time())}"
                )
            else:
                set_cooldown(aid)
                notes.append(f"skip {aid}: out of quota")
            state = load_state()
            continue

        if not verify:
            return account, notes
        ok, reason = probe_cached(account)
        if ok:
            return account, notes
        notes.append(f"skip {aid}: {reason}")
        state = load_state()

    return None, notes


def check_duplicate(
    pool: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any] | None:
    key = identity_key(candidate)
    if not key:
        return None
    for existing in pool.get("accounts", []):
        if existing.get("id") != candidate.get("id") and identity_key(existing) == key:
            return existing
    return None


def reorder(pool: dict[str, Any], provider: str, wanted: list[str]) -> str | None:
    """Rearrange one provider's accounts in place. Returns an error message."""
    current = [a["id"] for a in accounts_for(pool, provider)]
    if sorted(wanted) != sorted(current):
        return (
            f"must list every {provider} account exactly once "
            f"(have: {' '.join(current)})"
        )
    ordered = [find(pool, i) for i in wanted]
    slots = [i for i, a in enumerate(pool["accounts"]) if a.get("provider") == provider]
    for slot, account in zip(slots, ordered):
        pool["accounts"][slot] = account
    return None


def snapshot() -> dict[str, Any]:
    """Everything the UI needs in one payload."""
    pool, state = load_pool(), load_state()
    out: dict[str, Any] = {"providers": {}, "generatedAt": time.time()}
    for provider in PROVIDERS:
        rows = []
        for position, account in enumerate(accounts_for(pool, provider), 1):
            until = cooling_until(state, account["id"])
            info = state.get("probes", {}).get(account["id"], {})
            cached = (state.get("quota", {}).get(account["id"]) or {}).get("data", {})
            ident = account.get("identity") or {}
            rows.append(
                {
                    "id": account["id"],
                    "position": position,
                    "provider": provider,
                    "kind": account.get("kind"),
                    "label": identity_label(account),
                    "email": ident.get("email"),
                    "plan": ident.get("plan") or ident.get("subscription"),
                    "hasCredential": bool(env_for(account)),
                    "coolingFor": (until - time.time()) if until else 0,
                    "health": (
                        "cooling"
                        if until
                        else "ok"
                        if info.get("ok")
                        else "unhealthy"
                        if info
                        else "unprobed"
                    ),
                    "healthReason": info.get("reason"),
                    "quota": cached,
                }
            )
        out["providers"][provider] = rows
    return out
