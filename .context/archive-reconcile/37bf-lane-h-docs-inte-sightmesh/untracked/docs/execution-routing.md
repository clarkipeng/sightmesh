# Execution routing

Execution routing is a local, subscription-first selector over the authoritative operator-owned account pool. It is intentionally policy-only at this checkpoint: it validates and explains the target route, but it does not launch an executor, resolve credentials, create cdesktop approvals, or recover a route swap.

The public settings shape is frozen from checkpoint `e3cab92cee77a4dbc2dde0fe522d518d6b9986da` and PR #18. C2 corrections do not change this schema.

## Settings schema

Settings live at `~/.config/sightmesh/execution_routing.json` and use owner-only file permissions when written by SightMesh:

```json
{
  "version": 1,
  "executionRouting": {
    "enabled": true,
    "routes": [
      {
        "id": "codex-subscriptions",
        "executor": "CODEX",
        "model": "gpt-5.6-luna",
        "billingClass": "subscription",
        "accountPool": "codex"
      },
      {
        "id": "claude-subscriptions",
        "executor": "CLAUDE_CODE",
        "model": "opus",
        "billingClass": "subscription",
        "accountPool": "claude"
      },
      {
        "id": "codex-metered-api",
        "executor": "CODEX",
        "model": "gpt-5.6-luna",
        "billingClass": "metered",
        "account": "codex-api-primary"
      }
    ],
    "meteredFallback": "ask",
    "sameRouteRetries": 2,
    "transientBackoffSeconds": [5, 20],
    "approvalTimeoutMinutes": 0,
    "allRoutesExhausted": "block",
    "notifyOnSwap": true,
    "exposeAccountAlias": true
  }
}
```

The complete secret-free example is [examples/execution-routing.subscription-first.json](../examples/execution-routing.subscription-first.json).

Routes are ordered. A `subscription` route names `accountPool` and must not name `account`. A `metered` route names one fixed `account` alias and must not name `accountPool`. Route IDs must be unique. Supported executors are `CODEX` and `CLAUDE_CODE`; supported billing classes are `subscription` and `metered`; supported account pools are `codex` and `claude`.

`meteredFallback` controls what happens only after earlier subscription routes do not resolve:

- `auto` selects the first eligible metered route.
- `ask` returns an `approval_needed` selection result for the first eligible metered route.
- `never` skips metered routes and blocks if no subscription route resolves.

`approvalTimeoutMinutes` is part of the frozen public shape, but durable cdesktop approval wiring is not implemented at this checkpoint. Treat `ask` as a selector outcome that must become a durable visible approval before metered execution is considered shipped.

## Selection behavior

Selection reads the pool and pool state every time. It does not mirror account inventory in routing settings, so account additions, order changes, quota updates, cooldowns, and disabled flags take effect without rewriting this file.

The selector walks routes in order and skips accounts that are disabled, cooling, missing their normal credential binding, exhausted, or explicitly excluded after a prior failed binding. Subscription routes consider non-API accounts from the named provider pool in existing pool order. Metered routes consider only their fixed account alias.

A resolved target contains route metadata plus an opaque `auth_binding_id`. That value is the pool account ID, not a secret. Credential resolution remains the launcher's responsibility and must happen only through the provider's normal logged-in CLI or configured provider path.

## Safe visibility

`exposeAccountAlias` defaults to `true`, which lets selection output include a safe account alias for diagnostics. Set it to `false` to suppress the display alias in the selected target. This does not change route order, eligibility, or the opaque binding passed to a future launcher.

Aliases such as `codex-api-primary` and route IDs such as `codex-metered-api` are operational labels. Do not put tokens, cookies, authorization headers, credential paths, provider response bodies, or credential-shaped sample values in settings, examples, logs, traces, release notes, or orchestration state.

## CLI workflow

Configure routes explicitly:

```sh
sightmesh routing routes add \
  --id codex-subscriptions \
  --executor CODEX \
  --model gpt-5.6-luna \
  --billing-class subscription \
  --account-pool codex

sightmesh routing routes add \
  --id claude-subscriptions \
  --executor CLAUDE_CODE \
  --model opus \
  --billing-class subscription \
  --account-pool claude

sightmesh routing routes add \
  --id codex-metered-api \
  --executor CODEX \
  --model gpt-5.6-luna \
  --billing-class metered \
  --account codex-api-primary
```

Review without launching:

```sh
sightmesh routing show
sightmesh routing validate
sightmesh routing explain --workspace workspace-demo
sightmesh routing set-metered ask
```

Use `sightmesh routing routes order` with every route ID exactly once when changing priority. Put subscription routes first when subscription capacity should be preferred across providers and models.

## Current release boundary

This checkpoint ships the settings contract, validation, route ordering, non-launching explanation, and safe selection result. It does not ship executor launch integration, credential resolution, durable cdesktop approval creation for `meteredFallback=ask`, or route-swap recovery. cdesktop remains the primary workspace/session UI.

`sightmesh pool serve` is retained as a loopback recovery and compatibility view for account-pool diagnostics. It is not an execution-routing UI, route launcher, approval surface, or cdesktop replacement.
