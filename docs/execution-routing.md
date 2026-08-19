# Execution routing

Execution routing is a local, subscription-first selection policy. It chooses a safe execution target from the operator-owned account pool; it does not store or display a credential, and it does not replace cdesktop as the workspace and session UI.

## Frozen settings contract

Settings live at `~/.config/sightmesh/execution_routing.json` with owner-only permissions. They contain policy and non-secret identifiers only:

- routes are ordered. The selector evaluates them from first to last;
- a `subscription` route names an `accountPool` (`claude` or `codex`) and considers its non-API accounts in their existing pool order;
- a `metered` route names one fixed `account` alias;
- each route also names the executor and exact model to launch later;
- `meteredFallback` defaults to `auto` and accepts only `auto`, `ask`, or `never`;
- `exposeAccountAlias` defaults to `true`.

The selector skips disabled, cooling, credential-unavailable, exhausted, and previously failed bindings. A model preference filters routes before selection. It rereads the pool for every selection, so account ordering and health changes take effect without a settings rewrite.

For example, this is safe route-shape configuration: it contains aliases only, never credentials.

```sh
sightmesh routing routes add \
  --id codex-luna-subscriptions \
  --executor CODEX \
  --model gpt-5.6-luna \
  --billing-class subscription \
  --account-pool codex

sightmesh routing routes add \
  --id codex-luna-metered \
  --executor CODEX \
  --model gpt-5.6-luna \
  --billing-class metered \
  --account codex-api-primary
```

Keep subscription routes before metered routes when subscription capacity should be preferred. `sightmesh routing routes order` must receive every configured route ID exactly once; that makes priority explicit and stable.

## Metered policy and current boundary

`auto` selects the first eligible metered route after earlier routes cannot resolve. `ask` returns an `approval_needed` outcome for the first eligible metered route. `never` skips every metered route and returns blocked when no subscription route resolves; it does not inspect the metered account credential.

These are durable selector guarantees: the settings are persisted and each selection produces a resolved, approval-needed, or blocked result. The executor-launch handoff is not integrated yet. In particular, `ask` does not yet create a durable cdesktop approval, and neither `auto` nor `never` has cdesktop route-swap recovery behavior. Do not describe those integration behaviors as shipped.

Use the non-launching commands to review the policy:

```sh
sightmesh routing show
sightmesh routing validate
sightmesh routing explain --workspace workspace-demo
sightmesh routing set-metered ask
```

## Authentication boundary and visibility

A resolved target carries an opaque `auth_binding_id`: a reference to the selected pool account, not a credential. The reference is handed to the future executor launcher; only that launcher may resolve the account's normal provider credentials, immediately before the executor starts.

Never place credential paths, headers, cookies, tokens, provider responses, or credential-shaped sample values in routing settings, selection traces, UI, logs, or examples. Route IDs and account aliases are safe operational labels, such as `codex-api-primary`; they are not authentication material.

With `exposeAccountAlias=true`, the selected target includes that safe alias for operator diagnostics. With it disabled, the selected target returns `account_alias: null`; the opaque binding remains available to the launcher. Disabling the display alias does not change route order, eligibility, or which binding the selector chooses. In this mode, selection traces use the generic label `account` and do not identify account IDs or aliases; traces must never include credentials or their locations.

## Upgrade and compatibility

This settings file is versioned independently and starts with routing enabled, no routes, and `meteredFallback: auto`. No route means selection is safely blocked. Add routes deliberately, validate them, and inspect the non-launching explanation before connecting later execution integration.

Existing cdesktop workspaces, profiles, and active sessions continue to be managed in cdesktop. Do not use this policy to imply that an existing session will be interrupted, recovered, or approved automatically. cdesktop is the primary UI for those activities. `sightmesh pool serve` is retained only as a loopback recovery/compatibility view of pool state; it is not a route launcher, route-approval UI, or substitute for cdesktop.
