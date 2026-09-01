## Summary

- Adds the cdesktop `/agents` destination and Settings > Execution Routing shell.
- Integrates the Agents view with Lane B's durable metered approval API at exact head `fa9600cf34c67d89ff82287f76f1cd6cd35116ed`.
- Supports pending approve/deny (optional reason) and displays approved, denied, auto-started, and blocked states without projecting opaque auth-binding IDs or secrets.

## Scope

- Base: `cdt/13da-cdesktop-format`; implementation continued from `ecc986b7736c233bf4cd7b25a273eddaf4b38a14`.
- Integration delta is frontend-only under `packages/web-core/**`.
- No Rust, migrations, backend contracts, workflows, runtime locks, or generated `shared/types.ts` changed.

## Honest evidence

- `git diff --check`: passed.
- Web-core/local-web checks and formatting: not run because `tsc` and `prettier` are unavailable in the worktree (`spawn ENOENT`).
- GitHub shows no CI because `test.yml` limits `pull_request` to base branch `main`; this stacked draft targets `cdt/13da-cdesktop-format`, so jobs never start.

## Remaining contract gap

Lane B persists and types normalized execution outcomes but exposes no outcome read route at `96960fbe`; the execution-process API does not return them. Outcome display remains blocked on that backend surface and is not simulated here.

Draft only. No merge, ready, release, or workflow-dispatch authority.
