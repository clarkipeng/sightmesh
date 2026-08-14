"""Local web UI for the credential pool.

Stdlib only, bound to loopback. Slow work (browser logins, quota refresh,
probes) runs as background jobs the page polls, so no request hangs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import core

UI = Path(__file__).with_name("ui.html")

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

# The UI mutates credentials, so only loopback names may address it. Without
# this a hostile page could reach the server by rebinding DNS to 127.0.0.1.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def start_job(fn: Callable[[], str | None]) -> str:
    job_id = uuid.uuid4().hex[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {"done": False, "ok": None, "message": "working..."}

    def run() -> None:
        try:
            message = fn() or "done"
            result = {"done": True, "ok": True, "message": message}
        except Exception as exc:  # noqa: BLE001 - surfaced to the page verbatim
            result = {"done": True, "ok": False, "message": str(exc)}
        with JOBS_LOCK:
            JOBS[job_id] = result

    threading.Thread(target=run, daemon=True).start()
    return job_id


# ---------------------------------------------------------------- actions


def job_refresh() -> str:
    pool = core.load_pool()
    for account in pool.get("accounts", []):
        core.quota_cached(account, force=True)
    return "quota refreshed"


def job_verify() -> str:
    pool = core.load_pool()
    problems = []
    seen: dict[str, str] = {}
    for account in pool.get("accounts", []):
        if account.get("provider") == "codex":
            fresh = core.codex_identity(
                os.path.expanduser(account.get("codex_home", ""))
            )
            if fresh:
                account["identity"] = fresh
        key = core.identity_key(account)
        if key and key in seen:
            problems.append(f"{account['id']} duplicates {seen[key]}")
        elif key:
            seen[key] = account["id"]
        ok, reason = core.probe(account)
        core.quota_cached(account, force=True)
        if not ok and reason != "usage limit":
            problems.append(f"{account['id']}: {reason}")
    core.save_pool(pool)
    return "; ".join(problems) if problems else "all accounts distinct and usable"


def job_add_claude(body: dict[str, Any]) -> str:
    account_id = body.get("id", "").strip()
    token = core.normalize_token(body.get("token", ""))
    if not account_id:
        raise ValueError("id is required")
    problem = core.validate_claude_token(token)
    if problem:
        raise ValueError(problem)
    pool = core.load_pool()
    if core.find(pool, account_id):
        raise ValueError(f"'{account_id}' already exists")

    ident = core.ambient_claude_identity()
    if not ident.get("email"):
        raise ValueError(
            ident.get("error", "could not determine the logged-in account")
        )

    candidate = {
        "id": account_id,
        "provider": "claude",
        "kind": "oauth",
        "label": ident["email"],
        "identity": ident,
        "token_fp": core.fingerprint(token),
    }
    duplicate = core.check_duplicate(pool, candidate)
    if duplicate and not body.get("force"):
        raise ValueError(
            f"'{duplicate['id']}' already uses {core.identity_label(duplicate)}"
        )

    core.write_token(account_id, token)
    ok, reason = core.probe(candidate)
    if not ok:
        core.token_path(account_id).unlink(missing_ok=True)
        raise ValueError(f"token rejected: {reason}")

    pool.setdefault("accounts", []).append(candidate)
    core.save_pool(pool)
    return f"added {account_id} ({ident['email']})"


def job_add_codex(body: dict[str, Any]) -> str:
    account_id, mode = body.get("id", "").strip(), body.get("mode")
    if not account_id or mode not in ("sub", "apikey"):
        raise ValueError("id and mode (sub|apikey) are required")
    pool = core.load_pool()
    if core.find(pool, account_id):
        raise ValueError(f"'{account_id}' already exists")

    codex_home = Path(os.path.expanduser(f"~/.codex-{account_id}"))
    codex_home.mkdir(parents=True, exist_ok=True)
    primary = Path.home() / ".codex" / "config.toml"
    if primary.exists() and not (codex_home / "config.toml").exists():
        shutil.copy2(primary, codex_home / "config.toml")

    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    token = None
    if mode == "apikey":
        token = (body.get("key") or "").strip()
        if not token:
            raise ValueError("API key is required")
        run = subprocess.run(
            ["codex", "login", "--with-api-key"],
            env=env,
            input=token,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if run.returncode != 0:
            raise ValueError((run.stderr or "codex login failed").strip()[:200])
    else:
        run = subprocess.run(
            ["codex", "login"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if run.returncode != 0:
            raise ValueError((run.stderr or "codex login failed").strip()[:200])

    candidate = {
        "id": account_id,
        "provider": "codex",
        "kind": "chatgpt" if mode == "sub" else "apikey",
        "codex_home": str(codex_home),
        "identity": core.codex_identity(str(codex_home)),
    }
    if token:
        candidate["token_fp"] = core.fingerprint(token)
    candidate["label"] = core.identity_label(candidate)

    duplicate = core.check_duplicate(pool, candidate)
    if duplicate and not body.get("force"):
        raise ValueError(
            f"'{duplicate['id']}' already uses {core.identity_label(duplicate)}"
        )

    if token:
        core.write_token(account_id, token)
    pool.setdefault("accounts", []).append(candidate)
    core.save_pool(pool)
    return f"added {account_id} ({core.identity_label(candidate)})"


def do_order(body: dict[str, Any]) -> dict[str, Any]:
    pool = core.load_pool()
    error = core.reorder(pool, body["provider"], body["ids"])
    if error:
        return {"ok": False, "message": error}
    core.save_pool(pool)
    return {"ok": True}


def do_remove(body: dict[str, Any]) -> dict[str, Any]:
    pool = core.load_pool()
    account_id = body["id"]
    if not core.find(pool, account_id):
        return {"ok": False, "message": "unknown account"}
    pool["accounts"] = [a for a in pool["accounts"] if a["id"] != account_id]
    core.save_pool(pool)
    core.token_path(account_id).unlink(missing_ok=True)
    core.clear_cooldown(account_id)
    return {"ok": True}


def do_cool(body: dict[str, Any]) -> dict[str, Any]:
    core.set_cooldown(body["id"], int(body.get("seconds", core.DEFAULT_COOLDOWN)))
    return {"ok": True}


def do_clear(body: dict[str, Any]) -> dict[str, Any]:
    core.clear_cooldown(body["id"])
    return {"ok": True}


ROUTES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "/api/order": do_order,
    "/api/remove": do_remove,
    "/api/cool": do_cool,
    "/api/clear": do_clear,
}

JOB_ROUTES: dict[str, Callable[[dict[str, Any]], str]] = {
    "/api/refresh": lambda _body: job_refresh(),
    "/api/verify": lambda _body: job_verify(),
    "/api/add-claude": job_add_claude,
    "/api/add-codex": job_add_codex,
}


# ---------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:
        if not self._host_allowed():
            return self._json({"error": "forbidden host"}, 403)
        if self.path in ("/", "/index.html"):
            self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json(core.snapshot())
        elif self.path.startswith("/api/job/"):
            with JOBS_LOCK:
                self._json(
                    JOBS.get(
                        self.path.rsplit("/", 1)[-1],
                        {"done": True, "ok": False, "message": "unknown job"},
                    )
                )
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._host_allowed():
            return self._json({"error": "forbidden host"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "message": "bad json"}, 400)

        if self.path in JOB_ROUTES:
            handler = JOB_ROUTES[self.path]
            return self._json({"job": start_job(lambda: handler(body))})
        if self.path in ROUTES:
            try:
                return self._json(ROUTES[self.path](body))
            except Exception as exc:  # noqa: BLE001 - surfaced to the page verbatim
                return self._json({"ok": False, "message": str(exc)}, 400)
        self._json({"error": "not found"}, 404)


def serve(port: int = 7878, open_browser: bool = True) -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"sightmesh pool UI  ->  {url}")
    print("ctrl-c to stop")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
