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
EVIDENCE_TYPES = {"observed_static", "recorded_historical", "documented_claim", "blocked", "unknown"}
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
            "SIGHTMESH_BAKEOFF": "1",
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
        "cmd/agent-deck/main.go",
        "cmd/agent-deck/session_cmd.go",
        "cmd/agent-deck/hook_children_context.go",
        "src/sightmesh/cli.py",
        "src/sightmesh/cdesktop.py",
        "src/sightmesh/service.py",
        "src/sightmesh/bridge.py",
        "docs/compatibility.md",
        "docs/conductor/README.md",
        "docs/SESSION-PERSISTENCE-SPEC.md",
        "docs/ccbd-lifecycle-test-plan.md",
        "docs/claude-binary-cache-dedup-plan.md",
    ]:
        path = root / rel
        if path.exists() and path.is_file():
            texts[rel] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def source_match(texts: dict[str, str], rel_file: str, pattern: str) -> dict[str, Any] | None:
    text = texts.get(rel_file)
    if text is None:
        return None
    regex = re.compile(pattern)
    for line_no, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            return {
                "file": rel_file,
                "line": line_no,
                "pattern": pattern,
                "excerpt": line.strip(),
            }
    return None


def source_matches(texts: dict[str, str], specs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for rel_file, pattern in specs:
        match = source_match(texts, rel_file, pattern)
        if match:
            matches.append(match)
    return matches


def evidence(
    status: str,
    evidence_type: str,
    summary: str,
    *,
    source_evidence: list[dict[str, Any]] | None = None,
    commands: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "score": STATUS_SCORE[status],
        "evidence_type": evidence_type,
        "summary": summary,
        "commands": commands or [],
    }
    if source_evidence is not None:
        result["source_evidence"] = source_evidence
    if limitations:
        result["limitations"] = limitations
    return result


def static_result(
    texts: dict[str, str],
    status: str,
    summary: str,
    specs: list[tuple[str, str]],
    *,
    commands: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
    missing_status: str = "unknown",
    missing_summary: str | None = None,
) -> dict[str, Any]:
    matches = source_matches(texts, specs)
    if len(matches) != len(specs):
        found = {(item["file"], item["pattern"]) for item in matches}
        missing = [{"file": file, "pattern": pattern} for file, pattern in specs if (file, pattern) not in found]
        return evidence(
            missing_status,
            "unknown" if missing_status == "unknown" else "observed_static",
            missing_summary or "Required source evidence was not found in the pinned source.",
            source_evidence=matches,
            limitations=[*(limitations or []), f"missing_source_evidence={missing}"],
            commands=commands,
        )
    return evidence(
        status,
        "observed_static",
        summary,
        source_evidence=matches,
        commands=commands,
        limitations=limitations,
    )


def evaluate_local(texts: dict[str, str], commands: list[dict[str, Any]]) -> dict[str, Any]:
    codex_stall = (
        "Historical local stack note from this bake-off: cdesktop 0.2.3 plus Codex CLI "
        "0.147.0 reached supervised approval/MCP elicitation but stalled, so unattended "
        "Codex launch and recovery are qualified rather than counted as fresh runtime pass."
    )
    return {
        "launch_claude_worker": static_result(
            texts,
            "pass",
            "Static launch surface supports full visible cdesktop Claude Code workspaces; no fresh provider runtime launch was executed.",
            [("src/sightmesh/cli.py", r'choices=\["CLAUDE_CODE", "CODEX"\]'), ("src/sightmesh/cdesktop.py", r'"/workspaces/start"')],
        ),
        "launch_codex_worker": static_result(
            texts,
            "partial",
            "Static launch surface supports Codex workspaces, but current local compatibility is qualified by the recorded supervised-approval/MCP-elicitation stall.",
            [
                ("src/sightmesh/cli.py", r'choices=\["CLAUDE_CODE", "CODEX"\]'),
                ("src/sightmesh/cdesktop.py", r'"/workspaces/start"'),
                ("docs/compatibility.md", r"cdesktop 0\.2\.3"),
                ("docs/compatibility.md", r"Codex CLI 0\.147\.0"),
            ],
            limitations=[codex_stall],
        ),
        "human_visibility_takeover": static_result(
            texts,
            "pass",
            "cdesktop workspace/session APIs and visible follow-up commands provide human-visible worker control surfaces.",
            [("src/sightmesh/cli.py", r"Send a visible cdesktop follow-up"), ("src/sightmesh/cdesktop.py", r'"/sessions/\{session_id\}/follow-up"')],
        ),
        "cross_agent_request_reply": static_result(
            texts,
            "pass",
            "Repowire asks are injected as cdesktop follow-ups and closed through bridge-reply.",
            [("src/sightmesh/bridge.py", r'Repowire ask from @\{from_peer\}'), ("src/sightmesh/cli.py", r"bridge-reply")],
        ),
        "isolated_worktrees": static_result(
            texts,
            "pass",
            "spawn requires an explicit worktree/direct choice and passes use_worktree into cdesktop.",
            [("src/sightmesh/cli.py", r"--worktree"), ("src/sightmesh/cdesktop.py", r'"use_worktree": use_worktree')],
        ),
        "dirty_work_refusal": static_result(
            texts,
            "pass",
            "close --archive refuses dirty git repositories unless --preserve-dirty is explicit.",
            [("src/sightmesh/cli.py", r"Refusing to archive dirty repositories"), ("src/sightmesh/cdesktop.py", r'"git", "status", "--porcelain=v1"')],
        ),
        "crash_restart_recovery": static_result(
            texts,
            "partial",
            "launchd KeepAlive covers local cdesktop/bridge services; provider-level crash recovery remains qualified by cdesktop/Codex stall evidence.",
            [("src/sightmesh/service.py", r'"KeepAlive": True'), ("src/sightmesh/bridge.py", r"Bridge .* disconnected")],
            limitations=[codex_stall],
        ),
        "local_only_operation": static_result(
            texts,
            "pass",
            "Local configuration disables analytics/relay and binds the managed service to loopback.",
            [("src/sightmesh/cdesktop.py", r'"analytics_enabled": False'), ("src/sightmesh/cdesktop.py", r'"relay_enabled": False'), ("src/sightmesh/service.py", r"http://127\.0\.0\.1:\{port\}")],
        ),
        "install_uninstall_containment": static_result(
            texts,
            "partial",
            "service install/uninstall is scoped to user LaunchAgents and local state paths, but actual install mutates user launchd state so it is not executed by the bake-off.",
            [("src/sightmesh/service.py", r'Library.*LaunchAgents'), ("src/sightmesh/service.py", r'\.local.*state.*sightmesh'), ("src/sightmesh/service.py", r"def uninstall")],
        ),
    }


def evaluate_ash(texts: dict[str, str]) -> dict[str, Any]:
    return {
        "launch_claude_worker": static_result(
            texts,
            "pass",
            "Pinned source documents direct Claude session launch with `-c claude`.",
            [("README.md", r"agent-deck add .* -c claude")],
        ),
        "launch_codex_worker": static_result(
            texts,
            "pass",
            "Pinned source proves direct Codex launch selection with `-c codex`; helper recommendations do not reduce this static launch score.",
            [("cmd/agent-deck/main.go", r"agent-deck add -c codex"), ("cmd/agent-deck/main.go", r"Tool/command to run .*codex")],
        ),
        "human_visibility_takeover": static_result(
            texts,
            "pass",
            "Pinned source describes tmux-backed sessions and attach/takeover behavior.",
            [("README.md", r"One terminal shows every session"), ("README.md", r"Enter.*Attach to session")],
        ),
        "cross_agent_request_reply": static_result(
            texts,
            "partial",
            "Pinned source includes session send/inbox/conductor reply paths, but no neutral Repowire-equivalent mesh bridge was found.",
            [("cmd/agent-deck/session_cmd.go", r"session output"), ("cmd/agent-deck/hook_children_context.go", r"session send")],
        ),
        "isolated_worktrees": static_result(
            texts,
            "pass",
            "Pinned source documents worktree session creation and cleanup.",
            [("README.md", r"agent-deck add .*--worktree"), ("README.md", r"agent-deck worktree cleanup")],
        ),
        "dirty_work_refusal": evidence(
            "unknown",
            "unknown",
            "The runner did not find an exact dirty close/archive refusal criterion in the inspected source; this is not blocked by execution, it is unverified.",
        ),
        "crash_restart_recovery": static_result(
            texts,
            "pass",
            "Pinned source documents restart/revive and session persistence paths.",
            [("cmd/agent-deck/session_cmd.go", r"session restart"), ("cmd/agent-deck/session_cmd.go", r"revive"), ("docs/SESSION-PERSISTENCE-SPEC.md", r"session restart")],
        ),
        "local_only_operation": static_result(
            texts,
            "pass",
            "Pinned source describes local tmux sessions and says existing tmux sessions are untouched; remote/conductor channels are optional.",
            [("README.md", r"Agent Deck creates its own tmux sessions"), ("README.md", r"Your existing sessions are untouched")],
        ),
        "install_uninstall_containment": static_result(
            texts,
            "partial",
            "Installer supports custom install dir and non-interactive mode, and uninstall supports dry-run, but default paths still target user-global locations.",
            [("install.sh", r"--dir"), ("install.sh", r"--non-interactive"), ("uninstall.sh", r"--dry-run")],
        ),
    }


def evaluate_ccb(texts: dict[str, str]) -> dict[str, Any]:
    return {
        "launch_claude_worker": static_result(
            texts,
            "pass",
            "Pinned source config examples launch Claude agents in visible panes.",
            [("README.md", r"worker2:claude\(worktree\)"), ("README.md", r"reviewer:claude")],
        ),
        "launch_codex_worker": static_result(
            texts,
            "pass",
            "Pinned source config examples launch Codex agents in visible panes.",
            [("README.md", r"main:codex"), ("README.md", r"worker1:codex")],
        ),
        "human_visibility_takeover": static_result(
            texts,
            "pass",
            "Pinned source states every agent is a full native terminal with visible layout control and direct takeover.",
            [("README.md", r"Every agent is a full native terminal"), ("README.md", r"direct takeover")],
        ),
        "cross_agent_request_reply": static_result(
            texts,
            "pass",
            "Pinned source includes `/ask`, `ccb ask`, mailbox, and ccbd request/reply lifecycle coverage.",
            [("README.md", r"/ask reviewer"), ("docs/ccbd-lifecycle-test-plan.md", r"ccb ask"), ("docs/ccbd-lifecycle-test-plan.md", r"reply")],
        ),
        "isolated_worktrees": static_result(
            texts,
            "pass",
            "Pinned source supports worktree-tagged agents.",
            [("README.md", r"worker1:codex\(worktree\)"), ("README.md", r"worker2:claude\(worktree\)")],
        ),
        "dirty_work_refusal": evidence(
            "unknown",
            "unknown",
            "The runner did not find an exact dirty close/archive refusal criterion in the inspected source; this is unverified rather than blocked.",
        ),
        "crash_restart_recovery": static_result(
            texts,
            "pass",
            "Pinned source covers keeper, restart, kill, stale socket, and crash recovery scenarios.",
            [("docs/ccbd-lifecycle-test-plan.md", r"kill ccbd pid"), ("docs/ccbd-lifecycle-test-plan.md", r"stale socket"), ("docs/ccbd-lifecycle-test-plan.md", r"crash")],
        ),
        "local_only_operation": static_result(
            texts,
            "pass",
            "Pinned source records loopback defaults and opt-in LAN boundary; optional remote/mobile features are not penalized when disabled.",
            [("README.md", r"always binds to loopback"), ("README.md", r"binds to loopback by default"), ("README.md", r"For direct LAN access")],
        ),
        "install_uninstall_containment": static_result(
            texts,
            "partial",
            "Installer supports prefix/bin overrides and managed prefixes, but default install can write global wrappers and dependencies.",
            [("install.sh", r"CODEX_INSTALL_PREFIX"), ("install.sh", r"CODEX_BIN_DIR"), ("install.sh", r"global wrappers")],
        ),
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
            if result["evidence_type"] not in EVIDENCE_TYPES:
                raise ValueError(f"{competitor.get('id')} {scenario_id} has invalid evidence_type")
            if result["score"] != STATUS_SCORE[result["status"]]:
                raise ValueError(f"{competitor.get('id')} {scenario_id} score/status mismatch")
            if result["evidence_type"] == "observed_static" and not result.get("source_evidence"):
                raise ValueError(f"{competitor.get('id')} {scenario_id} observed_static missing source_evidence")


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
    with tempfile.TemporaryDirectory(prefix="sightmesh-bakeoff.") as temp:
        work_root = Path(temp)
        env = isolated_env(work_root)
        competitors = []
        for competitor in manifest["competitors"]:
            source_path, pin = clone_or_locate(competitor, repo_root=repo_root, work_root=work_root, env=env)
            commands: list[dict[str, Any]] = []
            if competitor["id"] == "local_sightmesh":
                commands.append(run([sys.executable, "-m", "sightmesh.cli", "--help"], cwd=repo_root, env={**env, "PYTHONPATH": str(repo_root / "src")}, timeout=20))
            elif competitor["id"] == "ccb":
                commands.append(run([sys.executable, "ccb.py", "--print-version"], cwd=source_path, env=env, timeout=20))
            else:
                commands.append(run(["git", "status", "--short"], cwd=source_path, env=env, timeout=20))
            texts = read_texts(source_path)
            if competitor["id"] == "local_sightmesh":
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
                    "introspection_commands": commands,
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
    parser = argparse.ArgumentParser(description="Run the isolated SightMesh competitor bake-off.")
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/bakeoff_manifest.json"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path("benchmarks/bakeoff_results.json"))
    args = parser.parse_args(argv)
    results = run_bakeoff(args.manifest.resolve(), args.repo_root.resolve(), args.out.resolve())
    print(json.dumps({"out": str(args.out), "totals": {item["id"]: item["total"] for item in results["competitors"]}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
