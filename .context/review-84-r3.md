# PR #84 review, round 3

Reviewed `clarkipeng/sightmesh` PR #84 at exact head
`41a9ffc286bda3e101ef7f23775c5ef8c33441c2` against `origin/main` on
2026-09-03. The reviewed commit is rebased: `git merge-base --is-ancestor
origin/main 41a9ffc286bda3e101ef7f23775c5ef8c33441c2` returned 0.

## Verdict

**APPROVE.** No P-level findings. The lazy package-root SDK export retains the
public import surface, and the version-only CLI path remains dependency-light.
The liveness classifier and durable seams preserve the intended safety
invariants: evidence absence remains inert; a silent stretch has one episode
despite reason relabels; the episode is capped at an initial wake plus one
escalation; migrations preserve existing rows and seed the cohort watermark;
and only delivered cohort wakes advance that watermark.

## Regression proof and commands

The lock guard was tested with a real negative control. I temporarily
reverse-applied only `bb1739bd73a6a389a140070801b6808e430fad83`, restored its
new test from `HEAD` so it could exercise the reverted code, ran the test, then
aborted the revert and confirmed a clean target tree:

```sh
git revert --no-commit bb1739bd73a6a389a140070801b6808e430fad83
git restore --source=HEAD -- tests/test_runtime_lock.py
uv run --with pytest pytest \
  tests/test_runtime_lock.py::test_runtime_lock_import_does_not_require_the_sdk_runtime_dependency -q
git restore --source=HEAD --staged --worktree tests/test_runtime_lock.py
git revert --abort
```

Outcome: the guard failed as required. The reverted eager `sightmesh` import
loaded `sdk -> wakes -> cdesktop`, then failed with
`ModuleNotFoundError: No module named 'websockets.exceptions'; 'websockets' is
not a package`. At restored `41a9ffc`, the same guard passed:

```sh
uv run --with pytest pytest \
  tests/test_runtime_lock.py::test_runtime_lock_import_does_not_require_the_sdk_runtime_dependency -q
# 1 passed in 0.08s
```

Independent compatibility probe:

```sh
uv run --with pytest python - <<'PY'
import sightmesh
from sightmesh import BatchResult, Command, SightMesh, SightMeshError, Worker, WorkerSpec
assert sightmesh.SightMesh is SightMesh
assert sightmesh.WorkerSpec is WorkerSpec
assert set(sightmesh.__all__) == {"BatchResult", "Command", "SightMesh", "SightMeshError", "Worker", "WorkerSpec", "__version__"}
from sightmesh import cli
root = cli.parser()
for action in root._actions:
    if hasattr(action, "choices") and action.choices:
        print(",".join(sorted(action.choices)))
PY
```

Outcome: all six SDK convenience exports resolve from `sightmesh`; constructing
the CLI parser resolves all 42 registered subcommands.

Focused liveness and integration sweep:

```sh
uv run --with pytest pytest \
  tests/test_runtime_lock.py::test_runtime_lock_import_does_not_require_the_sdk_runtime_dependency \
  tests/test_sdk.py tests/test_liveness.py tests/test_liveness_detector.py \
  tests/test_task_store.py tests/test_durable.py tests/test_bridge.py \
  tests/simulator/test_scenarios.py -q
# 188 passed in 7.27s

uv run --with pytest pytest tests/simulator -q
# 30 passed in 6.08s

uv run --with pytest pytest -q
# 519 passed in 16.42s
```

`git diff --check origin/main...41a9ffc` was clean.

## CI

`gh-axi pr checks 84` reported 10 passed, 0 failed on the PR head: pinned
cdesktop artifact, advisory cdesktop main source/package edge, and test lanes
for Python 3.11, 3.12, and 3.13 (each duplicated by the provider display).
