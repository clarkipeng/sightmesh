Authority: continue owning cdesktop draft PR #5 from exact pushed head `bf2d172a1d8d6b53c13932274adc31a3f73cc75f` and restore the remaining frontend CI baseline on the same shared-file lane.

Independent exact reproduction after the formatting fix:

- `pnpm --filter @vibe/remote-web run build` fails because shared routine navigation paths (`/routines`, `/routines/new`, `/routines/$routineId`, `/workspaces/$workspaceId`) are type-checked against the remote router registry, which does not expose those host-specific routes. It also reports `packages/web-core/src/shared/dialogs/settings/settings/ExecutorConfigForm.tsx:65` because `next.properties` is `unknown`.
- the i18n guard reports only missing `turnNavigation.count_one` in `ja/common`, `ko/common`, and `zh-Hans/common`; the listed Chinese settings extras are warnings, not failures.

Required action:

1. Inspect the registered local and remote router types plus existing cross-app navigation abstractions. Fix the shared-component routing boundary by construction so host-specific routine/workspace navigation does not bind shared source to a router that lacks those routes. Reuse an existing abstraction or inject navigation at the host boundary. Do not use `as any`, `@ts-ignore`, fake remote routes, or string casts that erase route checking.
2. Narrow the executor-config schema value before reading `properties`, following the existing schema utility pattern.
3. Add the three missing plural keys with translations consistent with the owning English key and existing locale conventions. Do not change the warning-only provider preset inventory.
4. Keep the formatter baseline and root UI-format owner from PR #5. Update its title/body from formatting-only to the truthful narrow frontend CI baseline if code changes are required.
5. Run the exact frontend CI command surface: local lint/build/format check, remote build/format check, UI check/lint/format check, web-core check/format check, i18n checks, unused-key check, and legacy-path guard. Run focused tests for any changed navigation/schema utility.
6. Commit, push, keep PR #5 draft, and report exact head and checks.

Exclusions: no backend behavior, cdesktop release-distribution branch or PR #4 edits, generated route/type files, release, dispatch, ready transition, merge, secret action, or primary checkout edit.

Stop when the frontend-checks job is locally reproducible as green and the draft branch is clean and pushed.
