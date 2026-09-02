# Execution routing

Execution routing is a local, subscription-first selection policy. It chooses a safe execution target from the operator-owned account pool; it does not store or display a credential, and it does not replace cdesktop as the workspace and session UI.

## Frozen settings contract

Settings live at `~/.config/sightmesh/execution_routing.json` with owner-only permissions. They contain policy and non-secret identifiers only:

- routes are grouped into **classes**, and each class holds one ordered chain. `standard` is ordinary work; `deep` is for a top-level supervised manager that fans work out. `defaultClass` names the class used when a task does not choose one;
- within a class, routes are ordered. The selector evaluates them from first to last and never leaves the class it was asked for;
- a `subscription` route names an `accountPool` (`claude` or `codex`) and considers its non-API accounts in their existing pool order;
- a `metered` route names one fixed `account` alias;
- a `free` route names neither, because it bills nothing and owns no account;
- each route also names the executor and exact model to launch later;
- `meteredFallback` defaults to `auto` and accepts only `auto`, `ask`, or `never`;
- `exposeAccountAlias` defaults to `true`;
- `fallbackOnFreeFailure` defaults to `false`.

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

Every `routing routes` command acts on one class - `--class` selects it, and omitting it means the default class. `sightmesh routing routes order` must receive every route ID in that class exactly once; that makes priority explicit and stable. Keep subscription routes before metered routes when subscription capacity should be preferred.

Route IDs are unique within a class, not across classes: two chains may both end on a hop named `sol`.

A settings file written before classes existed loads unchanged - its single ordered list becomes the `standard` chain, and the next save rewrites the file in the current shape.

## Metered policy and current boundary

`auto` selects the first eligible metered route after earlier routes cannot resolve. `ask` returns an `approval_needed` outcome for the first eligible metered route. `never` skips every metered route and returns blocked when no subscription route resolves; it does not inspect the metered account credential.

These are durable selector guarantees: the settings are persisted and each selection produces a resolved, approval-needed, or blocked result. SightMesh spawn and teammate launches route through the selector and carry the selected binding.

## What advances a chain

Exactly three typed provider outcomes move a task along its chain, and they are read from the effect journal, never from anything a worker printed:

| outcome | cooldown |
|---|---|
| `rate_limited` | the provider's advertised `retry_at`, else the capacity default |
| `auth` | the short window - a rotated credential is not exhausted capacity |
| `provider_down` | the short window, applied to every account in that route's pool |

Anything else - a definitive rejection, a lost launch, and every repository, test, or code failure - carries no provider outcome at all, so it cannot reroute. A worker whose failing test output happens to mention a rate limit is not a rate limit.

The condemned accounts are cooled once, at the moment the outcome is recorded, into pool `state.json` - the single source of account truth, so the cooldown survives a restart and every later selection observes it. The reroute itself is then a pure read: it excludes the failed binding and re-walks the same class chain, which lands on the next eligible *account of the same route* before it ever reaches the next route. Retrying one model on a second account before switching models therefore needs no retry counter.

The new target opens a new epoch that fences the old one. The task epoch, active lease, and attempt budget make repeated observations idempotent and bounded. An explicit profile or `--executor` override still records its class, so it stays recoverable; a profile with `automatic_failover` off blocks with a reason instead of failing over. A route that requires metered approval or a fully exhausted chain leaves the task blocked for a human decision.

Use the non-launching commands to review the policy:

`routing validate` proves a usable path per class before dispatch, and dispatch gates on it: a class whose chain is empty or whose every hop is ineligible is refused before any epoch, effect row, or native call exists. A metered hop awaiting approval still counts as usable - the work has somewhere to go, it just needs a human first.

```sh
sightmesh routing show
sightmesh routing validate
sightmesh routing validate --class deep
sightmesh routing explain --class deep --workspace workspace-demo
sightmesh routing set-metered ask
sightmesh routing set-free-fallback off
```

## Free routes and their failure

A `free` route bills nothing, so it owns no account and may name neither an `account` nor an `accountPool`.
Selection resolves it without reading pool state at all, and hands the launcher a fixed `free` binding sentinel that resolves to no credential anywhere.
That keeps "a free turn never spends" true by construction rather than by remembering to check.

Because a free route owns no binding, a terminal failure has nothing to cool and nothing to reroute.
Quota exhaustion on a subscription route cools the binding and moves on; the same failure on a free route would simply leave the worker blocked with no signal.
`escalate_free_route_failure` closes that gap: every terminal free-route failure produces exactly one escalation carrying the route id and an outcome class, delivered to a live parent or parked in the decision inbox.
There is no third outcome, and no silent block.

The outcome class is one of `model_unavailable`, `provider_rejected`, or `unknown`.
It is a report for a human, read from whatever the executor printed.
It is never persisted and no selection consults it, so a misread class costs clarity at worst - it can never move work onto an account that bills.

Degrading a failed free route onto a paid route is a separate decision, and it is off by default:

```sh
sightmesh routing set-free-fallback on    # allow it
sightmesh routing set-free-fallback off   # default
```

With the opt-in off, no other route is even considered and pool state is never read.
With it on, selection retries with the failed route excluded by id - every free route shares the binding sentinel, so excluding by account could not name one free route without naming them all.
Either way the escalation names where the work went, so a fallback onto a billed account is never something the operator discovers after the fact.

## Authentication boundary and visibility

A resolved target carries an opaque `auth_binding_id`: a reference to the selected pool account, not a credential. The reference is handed to the executor launcher; only that launcher may resolve the account's normal provider credentials, immediately before the executor starts.

Never place credential paths, headers, cookies, tokens, provider responses, or credential-shaped sample values in routing settings, selection traces, UI, logs, or examples. Route IDs and account aliases are safe operational labels, such as `codex-api-primary`; they are not authentication material.

With `exposeAccountAlias=true`, the selected target includes that safe alias for operator diagnostics. With it disabled, the selected target returns `account_alias: null`; the opaque binding remains available to the launcher. Disabling the display alias does not change route order, eligibility, or which binding the selector chooses. In this mode, selection traces use the generic label `account` and do not identify account IDs or aliases; traces must never include credentials or their locations.

## Upgrade and compatibility

This settings file is versioned independently and starts with routing enabled, no chains, and `meteredFallback: auto`. No chain for a class means dispatch of that class is refused by `validate`, not silently degraded. Add routes deliberately, validate them, and inspect the non-launching explanation before connecting later execution integration.

Existing cdesktop workspaces, profiles, and active sessions continue to be managed in cdesktop. Do not use this policy to imply that an existing session will be interrupted, recovered, or approved automatically. cdesktop is the primary UI for those activities. `sightmesh pool serve` is retained only as a loopback recovery/compatibility view of pool state; it is not a route launcher, route-approval UI, or substitute for cdesktop.
