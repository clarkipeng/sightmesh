# Security

SightMesh keeps routing and orchestration state secret-free. The execution-routing settings contain policy, route IDs, model IDs, billing class, provider pool names, and safe account aliases only.

## Authoritative owned-auth inventory

The account pool is the authoritative inventory for operator-owned Claude and Codex accounts. Each account must be one the operator owns and logged into through the provider's normal interface. SightMesh may observe quota, cooldown, disabled state, and account order, then select the next eligible owned account.

SightMesh must not extract provider secrets, replay auth headers, copy cookies or tokens between accounts, share one account's session with another worker, or evade provider limits. Routing settings must not duplicate pool inventory or credential material.

## Secret-free routing state

Execution routing returns an opaque `auth_binding_id` that references a pool account. The value is not a token and is not enough to authenticate by itself. A future executor launcher may resolve that binding only at the launch boundary, using the provider's normal local credential mechanism.

Safe examples:

- route ID: `codex-metered-api`
- account alias: `codex-api-primary`
- account pool: `codex`
- model: `gpt-5.6-luna`

Unsafe examples:

- bearer, session, or authorization header values;
- credential file paths;
- OAuth, API, or refresh tokens;
- provider response bodies;
- copied cookies or account database records.

## Alias visibility

`exposeAccountAlias=true` allows the selector to show the safe selected alias for operator diagnostics. Set it to `false` when the operator wants selected targets to hide that display alias. The opaque binding still exists for the launcher; only the display field changes.

Aliases and trace lines are safe operational labels, not credentials. Keep them stable enough for operators to debug route order and quota behavior, but do not encode private account details that should not appear in logs or release notes.

## Durable approvals

`meteredFallback=ask` currently returns an `approval_needed` selector result. Before this can drive metered execution, that outcome must be connected to a durable cdesktop approval that survives ordinary visible-agent workflow boundaries and is decided by the local human or lead session. Until that integration exists, documentation and release notes must describe `ask` as a selector outcome, not as shipped metered approval execution.

## Pool UI boundary

`sightmesh pool serve` is a local loopback compatibility view for pool diagnostics and recovery. It is not a primary routing UI, not a route launcher, and not a substitute for cdesktop's visible workspace/session surface.
