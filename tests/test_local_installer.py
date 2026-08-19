from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv.log"
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_LOG"\n', encoding="utf-8")
    uv.chmod(0o755)
    return bin_dir, log


def run_script(script: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, log = fake_uv(tmp_path)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_LOG": str(log),
    }
    return (
        subprocess.run(
            [str(ROOT / "scripts" / script)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        ),
        log,
    )


def test_install_does_not_uninstall_upstream_agent_deck(tmp_path: Path) -> None:
    result, log = run_script("install-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "tool install --editable" in commands
    assert "uninstall agent-deck" not in commands


def test_install_migrates_only_the_documented_legacy_skill_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = home / ".local/share/agent-deck/skills/orchestrate-visible-agents"
    destination = home / ".claude/skills/orchestrate-visible-agents"
    legacy.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    destination.symlink_to(legacy)

    result, _ = run_script("install-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    assert destination.readlink() == ROOT / "skills/orchestrate-visible-agents"


def test_uninstall_refuses_unrelated_skill_links_without_running_uv(tmp_path: Path) -> None:
    home = tmp_path / "home"
    destination = home / ".codex/skills/orchestrate-visible-agents"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(tmp_path / "someone-else")

    result, log = run_script("uninstall-local.sh", tmp_path)

    assert result.returncode != 0
    assert "Refusing to remove unrelated" in result.stderr
    assert destination.is_symlink()
    assert not log.exists()


def test_uninstall_removes_only_its_own_skill_links(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for skill_name in ("orchestrate-visible-agents", "reconcile-agent-work"):
        for root in (home / ".claude/skills", home / ".codex/skills"):
            root.mkdir(parents=True, exist_ok=True)
            (root / skill_name).symlink_to(ROOT / "skills" / skill_name)

    result, log = run_script("uninstall-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    assert "tool uninstall sightmesh" in log.read_text(encoding="utf-8")
    assert not list(home.glob(".claude/skills/*"))
    assert not list(home.glob(".codex/skills/*"))
