# Lane C2 — narrow security and validation corrections

Start from exact PR #18 head `e3cab92cee77a4dbc2dde0fe522d518d6b9986da` (remote `origin/cdt/5709-lane-c-settings`). Public settings schema is frozen: do not change its shape. Review the authoritative F report at `/Users/clarkpeng/.local/share/sightmesh/.cdesktop-workspaces/5509-lane-f-adversari/sightmesh/.context/lane-f-adversarial-review.md` before editing.

Own only `src/sightmesh/execution_routing.py`, routing CLI validation in `src/sightmesh/cli.py`, and routing tests. Implement exactly these regressions:
1. When `exposeAccountAlias=false`, every selection trace/explanation redacts account identifiers.
2. Selection and metered `ask` are non-secret eligibility decisions only: never invoke `env_for`, `read_token`, or equivalent launch-material resolution before launch/approval.
3. `routing validate` reports routes with zero eligible accounts rather than unconditional valid=true.

Do not edit pool/auth inventory sources, spawn/reconciler code, docs, UI, provider secrets, or settings schema. Add focused regression tests. Commit/push a draft stacked PR based on #18 (or update its branch only after proving it is exclusively yours); report exact head, PR, checks, and no-secret-selection proof to `sightmesh parent --message`, then stop.
