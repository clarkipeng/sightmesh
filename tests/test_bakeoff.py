from __future__ import annotations

import json
import os
import importlib.util
from pathlib import Path


RUNNER = Path("scripts/bakeoff/run_bakeoff.py")
spec = importlib.util.spec_from_file_location("run_bakeoff", RUNNER)
assert spec is not None and spec.loader is not None
run_bakeoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_bakeoff)

SCHEMA_VERSION = run_bakeoff.SCHEMA_VERSION
isolated_env = run_bakeoff.isolated_env
validate_manifest = run_bakeoff.validate_manifest
validate_results = run_bakeoff.validate_results


def test_manifest_schema_contains_required_scenarios() -> None:
    manifest = json.loads(Path("benchmarks/bakeoff_manifest.json").read_text(encoding="utf-8"))
    validate_manifest(manifest)
    assert {item["id"] for item in manifest["competitors"]} == {
        "local_agent_deck",
        "ashesh_agent_deck",
        "ccb",
    }


def test_isolated_env_redirects_home_and_tmux(tmp_path) -> None:
    env = isolated_env(tmp_path)
    assert env["HOME"].startswith(str(tmp_path))
    assert env["XDG_CONFIG_HOME"].startswith(str(tmp_path))
    assert env["XDG_DATA_HOME"].startswith(str(tmp_path))
    assert env["XDG_CACHE_HOME"].startswith(str(tmp_path))
    assert env["TMUX_TMPDIR"].startswith(str(tmp_path))
    assert "TMUX" not in env
    assert env["HOME"] != os.path.expanduser("~")


def test_result_schema_rejects_missing_scenario() -> None:
    valid_scenario = {
        "status": "pass",
        "score": 1.0,
        "evidence_type": "observed_static",
        "summary": "ok",
        "commands": [],
    }
    results = {
        "schema_version": SCHEMA_VERSION,
        "competitors": [
            {
                "id": "x",
                "scenarios": {
                    "launch_claude_worker": valid_scenario,
                    "launch_codex_worker": valid_scenario,
                    "human_visibility_takeover": valid_scenario,
                    "cross_agent_request_reply": valid_scenario,
                    "isolated_worktrees": valid_scenario,
                    "dirty_work_refusal": valid_scenario,
                    "crash_restart_recovery": valid_scenario,
                    "local_only_operation": valid_scenario,
                    "install_uninstall_containment": valid_scenario,
                },
            }
        ],
    }
    validate_results(results)
    del results["competitors"][0]["scenarios"]["dirty_work_refusal"]
    try:
        validate_results(results)
    except ValueError as exc:
        assert "missing results" in str(exc)
    else:
        raise AssertionError("validate_results accepted an incomplete result set")
