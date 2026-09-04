from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "uv.log"
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/sh\n'
        'if [ "$1 $2" = "tool dir" ]; then printf "%s\\n" "$UV_TOOL_DIR"; exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$UV_LOG"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir, log


def run_script(script: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, log = fake_uv(tmp_path)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UV_LOG": str(log),
        "UV_TOOL_DIR": str(tmp_path / "uv-tools"),
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


def test_install_refuses_to_steal_a_skill_link_it_does_not_own(tmp_path: Path) -> None:
    """Install used to silently `rm` agent-deck's own skill link and put its
    own there, with no record and no way back. Refusing instead is what keeps
    uninstall a pure delete of created paths: nothing was ever displaced, so
    nothing has to be restored.
    """
    home = tmp_path / "home"
    foreign = home / ".local/share/agent-deck/skills/orchestrate-visible-agents"
    destination = home / ".claude/skills/orchestrate-visible-agents"
    foreign.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    destination.symlink_to(foreign)

    result, _ = run_script("install-local.sh", tmp_path)

    assert result.returncode != 0
    assert "does not own" in result.stderr
    assert destination.readlink() == foreign


def test_install_records_every_path_it_created(tmp_path: Path) -> None:
    """Uninstall can only be reversible if install leaves a record of what it
    owns; an implicit list spread across three scripts is not one.
    """
    home = tmp_path / "home"

    result, _ = run_script("install-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    manifest_path = home / ".local/state/sightmesh/install-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert oct(manifest_path.stat().st_mode & 0o777) == "0o600"
    assert set(manifest["created_paths"]) == {
        str(home / f".{tool}/skills/{skill}")
        for tool in ("claude", "codex")
        for skill in ("orchestrate-visible-agents", "reconcile-agent-work")
    }
    assert manifest["repo_root"] == str(ROOT)


def test_install_is_idempotent_over_its_own_links(tmp_path: Path) -> None:
    """Refuse-not-steal must not refuse *this* installation's own links, or a
    re-install of an already-installed host would fail.
    """
    first, _ = run_script("install-local.sh", tmp_path)
    second, _ = run_script("install-local.sh", tmp_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr


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


def test_uninstall_leaves_an_unverified_tool_installed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for skill_name in ("orchestrate-visible-agents", "reconcile-agent-work"):
        for root in (home / ".claude/skills", home / ".codex/skills"):
            root.mkdir(parents=True, exist_ok=True)
            (root / skill_name).symlink_to(ROOT / "skills" / skill_name)

    result, log = run_script("uninstall-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Left uv tool sightmesh installed" in result.stdout
    assert not log.exists()
    assert not list(home.glob(".claude/skills/*"))
    assert not list(home.glob(".codex/skills/*"))


def test_uninstall_removes_a_tool_owned_by_this_repo(tmp_path: Path) -> None:
    metadata = (
        tmp_path
        / "uv-tools/sightmesh/lib/python3.13/site-packages/sightmesh-0.9.3.dist-info/direct_url.json"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f'{{"url":"file://{ROOT}"}}', encoding="utf-8")

    result, log = run_script("uninstall-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    assert "tool uninstall sightmesh" in log.read_text(encoding="utf-8")


def test_install_refuses_before_it_installs_anything(tmp_path: Path) -> None:
    """Validate first, act second.

    Install used to run `uv tool install` and only then discover a skill
    path it does not own, so a refusal left a half-installed machine: the
    tool present, the skills missing, and nothing recording either. Every
    ownership check now runs before the first side effect.
    """
    home = tmp_path / "home"
    destination = home / ".codex/skills/reconcile-agent-work"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(tmp_path / "someone-elses-skill")

    result, log = run_script("install-local.sh", tmp_path)

    assert result.returncode != 0
    assert "does not own" in result.stderr
    assert not log.exists()
    assert not (home / ".local/state/sightmesh/install-manifest.json").exists()
    # The check that failed is the second skill, so the first must not have
    # been linked either.
    assert not (home / ".claude/skills/orchestrate-visible-agents").exists()


def test_the_manifest_is_moved_into_place_not_written_in_pieces(
    tmp_path: Path,
) -> None:
    """The manifest is the record uninstall acts on, so a half-written one is
    worse than none: it would name a subset of the paths to remove.
    """
    home = tmp_path / "home"

    result, _ = run_script("install-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    state = home / ".local/state/sightmesh"
    assert [path.name for path in state.iterdir()] == ["install-manifest.json"]
    assert json.loads((state / "install-manifest.json").read_text(encoding="utf-8"))


def test_uninstall_removes_exactly_what_the_manifest_records(tmp_path: Path) -> None:
    """Install promised the manifest was what uninstall reverses while
    uninstall actually reversed a hardcoded list; a path install recorded
    under any other name survived forever.
    """
    home = tmp_path / "home"
    install, _ = run_script("install-local.sh", tmp_path)
    assert install.returncode == 0, install.stderr

    manifest_path = home / ".local/state/sightmesh/install-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = home / ".claude/skills/renamed-skill"
    recorded.symlink_to(ROOT / "skills" / "orchestrate-visible-agents")
    manifest["created_paths"] = [*manifest["created_paths"], str(recorded)]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result, _ = run_script("uninstall-local.sh", tmp_path)

    assert result.returncode == 0, result.stderr
    assert not recorded.is_symlink()
    assert not list(home.glob(".claude/skills/*"))
    assert not list(home.glob(".codex/skills/*"))
    assert not manifest_path.exists()


def test_uninstall_refuses_a_recorded_path_that_is_no_longer_ours(
    tmp_path: Path,
) -> None:
    """A recorded path someone replaced with their own link is not ours to
    delete, however the manifest describes it.
    """
    home = tmp_path / "home"
    install, _ = run_script("install-local.sh", tmp_path)
    assert install.returncode == 0, install.stderr
    hijacked = home / ".claude/skills/orchestrate-visible-agents"
    hijacked.unlink()
    hijacked.symlink_to(tmp_path / "someone-else")

    result, log = run_script("uninstall-local.sh", tmp_path)

    assert result.returncode != 0
    assert "Refusing to remove unrelated" in result.stderr
    assert hijacked.is_symlink()
    # Nothing was removed, and the tool was never touched.
    assert (home / ".codex/skills/orchestrate-visible-agents").is_symlink()
    assert "uninstall" not in log.read_text(encoding="utf-8")
