# Upgrade notes

Execution routing adds a new local settings file at `~/.config/sightmesh/execution_routing.json`. Existing cdesktop workspaces, profiles, pool accounts, Repowire routes, leases, and active sessions are not migrated into this file.

## From existing SightMesh installs

1. Verify the owned account pool first:

   ```sh
   sightmesh pool list
   sightmesh pool verify
   ```

2. Add subscription routes before metered routes:

   ```sh
   sightmesh routing routes add \
     --id codex-subscriptions \
     --executor CODEX \
     --model gpt-5.6-luna \
     --billing-class subscription \
     --account-pool codex
   ```

3. Choose the metered policy:

   ```sh
   sightmesh routing set-metered ask
   ```

4. Validate and explain the selector without launching work:

   ```sh
   sightmesh routing validate
   sightmesh routing explain --workspace workspace-demo
   ```

No configured route means selection blocks safely. Keep using cdesktop for active workspaces and sessions until executor launch, durable approval, and route-swap recovery integration are released.

## From Conductor migrations

Complete the normal Conductor migration first, then configure execution routing only after the account pool is healthy. Execution routing is independent local policy; it does not import Conductor credentials, alter existing cdesktop sessions, or copy transcripts into routing state.

## Compatibility-only pool UI

The standalone pool UI remains available for local account-pool diagnostics:

```sh
sightmesh pool serve
```

Use it for compatibility and recovery only. It is not a routing-settings editor, route launcher, metered approval UI, or replacement for cdesktop.
