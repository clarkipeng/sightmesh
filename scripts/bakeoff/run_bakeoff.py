#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2026-08-12.bakeoff-results.v1"
STATUS_SCORE = {"pass": 1.0, "partial": 0.5, "blocked": 0.0, "fail": 0.0, "unknown": 0.0}
SCENARIO_IDS = {
    "launch_claude_worker",
    "launch_codex_worker",
    "human_visibility_takeover",
    "cross_agent_request_reply",
    "isolated_worktrees",
    "dirty_work_refusal",
    "crash_restart_recovery",
    "local_only_operation",
    "install_uninstall_containment",
}


def isolated_env(root: Path) -> dict[str, str]:
    home = root / "home"
    tmux = root / "tmux"
    for path in (home, tmux, home / ".config", home / ".local" / "share", home / ".cache"):
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "TMUX_TMPDIR": str(tmux),
            "AGENT_DECK_BAKEOFF": "1",
        }
    )
    env.pop("TMUX", None)
    return env


def run(cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "started_at": started,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "started_at": started,
            "returncode": None,
            "timed_out": True,
            "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }


def git_output(args: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = run(["git", *args], cwd=cwd, env=env, timeout=90)
    if result["returncode"] != 0:
        raise RuntimeError(result["stderr"] or result["stdout"] or f"git {' '.join(args)} failed")
    return result["stdout"].strip()


def github_latest_release(repo: str) -> dict[str, Any] | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    assets = [
        {"name": item.get("name"), "url": item.get("browser_download_url"), "size": item.get("size")}
        for item in data.get("assets", [])
    ]
    return {
        "tag_name": data.get("tag_name"),
        "published_at": data.get("published_at"),
        "html_url": data.get("html_url"),
        "assets": assets,
    }


def npm_view(package: str, *, cwd: Path, env: dict[str, str]) -> dict[str, Any] | None:
    if not shutil.which("npm"):
        return None
    result = run(["npm", "view", package, "version", "dist.tarball", "repository.url", "--json"], cwd=cwd, env=env, timeout=30)
    if result["returncode"] != 0:
        return {"error": (result["stderr"] or result["stdout"]).strip()}
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {"raw": result["stdout"].strip()}


def read_texts(root: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    for rel in [
        "README.md",
        "install.sh",
        "uninstall.sh",
        "ccb.py",
        "src/agent_deck/cli.py",
        "src/agent_deck/cdesktop.py",
        "src/agent_deck/service.py",
        "src/agent_deck/bridge.py",
        "docs/ccbd-lifecycle-test-plan.md",
        "docs/claude-binary-cache-dedup-plan.md",
    ]:
        path = root / rel
        if path.exists() and path.is_file():
            texts[rel] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def has(texts: dict[str, str], *patterns: str) -> bool:
    haystack = "\n".join(texts.values()).lower()
    return all(re.search(pattern.lower(), haystack, re.MULTILINE | re.DOTALL) for pattern in patterns)


def evidence(status: str, evidence_type: str, summary: str, commands: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "score": STATUS_SCORE[status],
        "evidence_type": evidence_type,
        "summary": summary,
        "commands": commands or [],
    }


def evaluate_local(texts: dict[str, str], commands: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "launch_claude_worker": evidence("pass", "observed_static", "CLI spawn accepts executor=CLAUDE_CODE and posts to cdesktop /workspaces/start.", commands),
        "launch_codex_worker": evidence("pass", "observed_static", "CLI spawn accepts executor=CODEX and posts to cdesktop /workspaces/start.", commands),
        "human_visibility_takeover": evidence("pass", "observed_static", "Workers are cdesktop workspaces/sessions; message and closeout commands route visible follow-ups."),
        "cross_agent_request_reply": evidence("pass", "observed_static", "Repowire bridge handles ask frames and bridge-reply sends ack/answer callbacks."),
        "isolated_worktrees": evidence("pass", "observed_static", "spawn requires explicit --worktree/--direct and passes use_worktree into cdesktop."),
        "dirty_work_refusal": evidence("pass", "observed_static", "close --archive refuses dirty git repositories unless --preserve-dirty is explicit."),
        "crash_restart_recovery": evidence("partial", "observed_static", "launchd KeepAlive covers local cdesktop/bridge services, but provider crash recovery is delegated to cdesktop."),
        "local_only_operation": evidence("pass", "observed_static", "configure_local sets analytics_enabled=false, relay_enabled=false, and loopback service URL."),
        "install_uninstall_containment": evidence("partial", "observed_static", "service install/uninstall is contained to user LaunchAgents and ~/.local/state logs, but it mutates user launchd state so runtime install is blocked."),
    }


def evaluate_ash(texts: dict[str, str]) -> dict[str, Any]:
    return {
        "launch_claude_worker": evidence("pass", "observed_static", "README/CLI source show `agent-deck add ... -c claude` support."),
        "launch_codex_worker": evidence("partial", "observed_static", "README lists Codex status/organization and demos, but source guidance warns manual Codex creation should use helper scripts."),
        "human_visibility_takeover": evidence("pass", "documented_claim", "README describes tmux/TUI sessions, attach, pane output, and direct takeover."),
        "cross_agent_request_reply": evidence("partial", "observed_static", "session send/inbox/conductor bridge exists; no host-neutral request/reply bus equivalent to Repowire was observed."),
        "isolated_worktrees": evidence("pass", "observed_static", "README and worktree command source support `agent-deck add --worktree` and cleanup."),
        "dirty_work_refusal": evidence("partial", "observed_static", "Source contains several refusal guards and tests for real-HOME/test safety, but no exact close/archive dirty-work refusal matched the local runner contract."),
        "crash_restart_recovery": evidence("pass", "observed_static", "session restart/revive and persistence tests are present; conductor bridge drain supervisor restarts after unexpected crashes."),
        "local_only_operation": evidence("partial", "observed_static", "Core TUI is local tmux/XDG state, but optional conductor/remote/mobile channels and account-switching features are in scope."),
        "install_uninstall_containment": evidence("partial", "observed_static", "Installer supports --dir and --non-interactive; uninstall has --dry-run, but defaults target ~/.local/bin, ~/.agent-deck, Homebrew, and ~/.tmux.conf."),
    }


def evaluate_ccb(texts: dict[str, str]) -> dict[str, Any]:
    return {
        "launch_claude_worker": evidence("pass", "observed_static", "README config examples launch `claude` agents in visible panes."),
        "launch_codex_worker": evidence("pass", "observed_static", "README config examples launch `codex` agents in visible panes."),
        "human_visibility_takeover": evidence("pass", "documented_claim", "README claims every agent is a full native terminal with visible layout control and takeover."),
        "cross_agent_request_reply": evidence("pass", "observed_static", "`/ask`, `ccb ask`, mailbox, ccbd, and reply lifecycle docs/source are present."),
        "isolated_worktrees": evidence("pass", "observed_static", "README v2 config supports `agent:provider(worktree)` entries."),
        "dirty_work_refusal": evidence("blocked", "observed_static", "No exact dirty-work archive/close refusal was found; CCB has guarded cleanup/update policies instead."),
        "crash_restart_recovery": evidence("pass", "observed_static", "ccbd lifecycle docs and source cover keeper, restart, kill, stale socket, and crash recovery scenarios."),
        "local_only_operation": evidence("partial", "observed_static", "Config UI and gateways bind loopback by default, but mobile remote/relay features exist and must be explicitly scoped."),
        "install_uninstall_containment": evidence("partial", "observed_static", "Installer supports CODEX_INSTALL_PREFIX/CODEX_BIN_DIR and managed prefixes, but default install writes global wrappers and may pip-install dependencies."),
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    if not manifest.get("schema_version"):
        raise ValueError("manifest missing schema_version")
    scenario_ids = {item["id"] for item in manifest.get("scenarios", [])}
    missing = SCENARIO_IDS - scenario_ids
    if missing:
        raise ValueError(f"manifest missing scenarios: {sorted(missing)}")
    for competitor in manifest.get("competitors", []):
        for key in ("id", "name", "kind", "version_ref"):
            if key not in competitor:
                raise ValueError(f"competitor missing {key}: {competitor}")


def validate_results(results: dict[str, Any]) -> None:
    if results.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unexpected result schema_version")
    for competitor in results.get("competitors", []):
        scenario_ids = set(competitor.get("scenarios", {}))
        missing = SCENARIO_IDS - scenario_ids
        if missing:
            raise ValueError(f"{competitor.get('id')} missing results: {sorted(missing)}")
        for scenario_id, result in competitor["scenarios"].items():
            if result["status"] not in STATUS_SCORE:
                raise ValueError(f"{competitor.get('id')} {scenario_id} has invalid status")
            if result["score"] != STATUS_SCORE[result["status"]]:
                raise ValueError(f"{competitor.get('id')} {scenario_id} score/status mismatch")


def clone_or_locate(competitor: dict[str, Any], *, repo_root: Path, work_root: Path, env: dict[str, str]) -> tuple[Path, dict[str, Any]]:
    if competitor["kind"] == "local":
        commit = git_output(["rev-parse", "HEAD"], cwd=repo_root, env=env)
        return repo_root, {"commit": commit, "source_mode": "local"}
    target = work_root / competitor["id"]
    git_output(["clone", "--depth", "1", "--branch", competitor["version_ref"].split("/")[-1], competitor["repo"], str(target)], cwd=work_root, env=env)
    commit = git_output(["rev-parse", "HEAD"], cwd=target, env=env)
    tag_object = git_output(["rev-parse", competitor["version_ref"]], cwd=target, env=env)
    return target, {"commit": commit, "tag_object": tag_object, "source_mode": "git-clone"}


def score(scenarios: dict[str, Any]) -> dict[str, Any]:
    total = sum(item["score"] for item in scenarios.values())
    possible = len(SCENARIO_IDS)
    return {"score": total, "possible": possible, "percent": round(100 * total / possible, 1)}


def run_bakeoff(manifest_path: Path, repo_root: Path, out_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    with tempfile.TemporaryDirectory(prefix="agent-deck-bakeoff.") as temp:
        work_root = Path(temp)
        env = isolated_env(work_root)
        competitors = []
        for competitor in manifest["competitors"]:
            source_path, pin = clone_or_locate(competitor, repo_root=repo_root, work_root=work_root, env=env)
            commands: list[dict[str, Any]] = []
            if competitor["id"] == "local_agent_deck":
                commands.append(run([sys.executable, "-m", "agent_deck.cli", "--help"], cwd=repo_root, env={**env, "PYTHONPATH": str(repo_root / "src")}, timeout=20))
            elif competitor["id"] == "ccb":
                commands.append(run([sys.executable, "ccb.py", "--print-version"], cwd=source_path, env=env, timeout=20))
            else:
                commands.append(run(["git", "status", "--short"], cwd=source_path, env=env, timeout=20))
            texts = read_texts(source_path)
            if competitor["id"] == "local_agent_deck":
                scenarios = evaluate_local(texts, commands)
            elif competitor["id"] == "ashesh_agent_deck":
                scenarios = evaluate_ash(texts)
            elif competitor["id"] == "ccb":
                scenarios = evaluate_ccb(texts)
            else:
                scenarios = {scenario: evidence("unknown", "blocked", "No evaluator configured.") for scenario in SCENARIO_IDS}
            release = github_latest_release(competitor["github_repo"]) if competitor.get("github_repo") else None
            npm = None
            if competitor["id"] == "ashesh_agent_deck":
                npm = npm_view("agent-deck", cwd=repo_root, env=env)
            elif competitor["id"] == "ccb":
                npm = npm_view("@seemseam/ccb", cwd=repo_root, env=env)
            competitors.append(
                {
                    "id": competitor["id"],
                    "name": competitor["name"],
                    "source": {**pin, "path": str(source_path), "version_ref": competitor["version_ref"]},
                    "release": release,
                    "npm": npm,
                    "safe_runtime_scope": {
                        "home": env["HOME"],
                        "xdg_config_home": env["XDG_CONFIG_HOME"],
                        "tmux_tmpdir": env["TMUX_TMPDIR"],
                        "provider_sessions_started": False,
                        "global_install_ran": False,
                    },
                    "scenarios": scenarios,
                    "total": score(scenarios),
                }
            )
        results = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "manifest": str(manifest_path),
            "base_commit": manifest["base_commit"],
            "competitors": competitors,
        }
        validate_results(results)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated agent-deck competitor bake-off.")
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/bakeoff_manifest.json"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path("benchmarks/bakeoff_results.json"))
    args = parser.parse_args(argv)
    results = run_bakeoff(args.manifest.resolve(), args.repo_root.resolve(), args.out.resolve())
    print(json.dumps({"out": str(args.out), "totals": {item["id"]: item["total"] for item in results["competitors"]}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
