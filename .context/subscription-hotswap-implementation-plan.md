# SightMesh subscription hot-swap and durable auto-resume plan

Status: implementation plan of record for the open-source release candidate  
Date: 2026-08-18  
Owners: one visible SightMesh program manager plus independently isolated cdesktop and SightMesh workers  
Release rule: all pull requests remain draft until exact-head review and explicit operator approval

## 1. Product outcome

SightMesh automatically keeps a logical agent task running across provider quota and authentication boundaries.

The default route is subscription-first:

1. Use the preferred subscription target, including its preferred model and ordered owned accounts.
2. If that subscription route is unavailable, try the next configured subscription route. Routes may cross executor families, for example Codex Luna to Claude Opus.
3. If every eligible subscription route is unavailable, use the configured metered API route automatically by default.
4. An operator setting can change metered fallback from `auto` to `ask` or `never`.

The logical command must execute exactly once. A quota or authentication handoff may create multiple attempts, processes, or executor sessions, but they remain one visible task with one durable command identity.

## 2. User-visible promise

An operator should be able to:

- add or remove owned Claude, Codex, and API authentications without changing code;
- order accounts and model routes;
- launch a visible worker without manually selecting an account;
- see the active safe account alias, executor, model, subscription or metered class, and reason it was selected;
- watch SightMesh cool an exhausted account until its provider reset;
- see the same task resume on the next route without copying prompts or reopening a worktree;
- choose whether metered API fallback is automatic, approval-gated, or disabled;
- approve or reject one metered transition from the normal cdesktop approval surface;
- recover correctly after SightMesh, cdesktop, or the local machine restarts;
- verify that no credential, header value, credential path, or reusable fingerprint appears in logs, transcripts, APIs, or UI.

## 3. Scope and non-goals

### In scope

- Claude Code subscription authentications.
- Codex subscription authentications with a dedicated `CODEX_HOME` per account.
- Metered API-key targets.
- Ordered cross-provider and cross-model routes.
- New authentication records discovered from the authoritative pool or cdesktop provider registry.
- cdesktop automatic launch and durable same-task resume.
- Quota exhaustion, authentication expiration, model unavailability, and transient provider throttling.
- Settings in CLI, local configuration, and the built-in cdesktop/SightMesh UI.
- Safe account and transition observability.

### Not in scope

- Bypassing provider limits or using accounts the operator does not own.
- Extracting credentials from an unrelated live process.
- Replaying raw browser or HTTP authorization headers from transcripts.
- Hiding metered usage when the operator selected approval-gated or disabled policy.
- Retrying ordinary code, test, tool, or user errors on another model.
- Treating a provider change as evidence that an otherwise failed task succeeded.

## 4. Core invariants

1. **One logical command, one active attempt.** A durable command has at most one running attempt. Selection and dispatch share a process-scoped dedupe fence.
2. **Subscription-first routing.** Every eligible subscription route precedes metered routes unless the operator explicitly reorders policy.
3. **Sticky execution.** A healthy task stays on its selected route. SightMesh does not churn accounts for load balancing.
4. **Classified transitions only.** Automatic fallback occurs only for a normalized quota, auth, model-availability, or bounded transient-provider outcome.
5. **No secret transport in orchestration state.** Durable commands and policy decisions carry opaque auth-binding identifiers, never credential values or paths.
6. **Reconstructable recovery.** After restart, cdesktop command metadata plus current policy and pool state determine the next valid action. No second transcript or context database is introduced.
7. **Stable order.** New authentication entries join the authoritative configured order. SightMesh does not maintain a mirrored inventory.
8. **Cross-executor continuity.** A Codex-to-Claude transition may create a linked successor session, but the workspace, logical command, checkpoint, and attempt history remain one visible run.
9. **Metered policy is explicit.** `auto` is the product default. `ask` blocks durably on approval. `never` reports exhaustion without starting a metered attempt.
10. **Fail closed on ambiguity.** Unknown errors, missing credentials, malformed settings, or uncertain duplicate state block and surface attention rather than spending or launching twice.
11. **Retirement is terminal.** Once ownership transfers, the superseded session is quarantined from automatic message wake and cannot launch, write, or spawn again. Queued delivery targets the active successor or remains pending for operator review.
12. **Workspace start is atomic.** A failed start either returns a launchable workspace with a container reference or compensates to an archived/deleted empty record. A missing `container_ref` cannot remain active or block later spawns.
13. **External launchers are durable.** When no cdesktop parent session exists, SightMesh records the Conductor launcher identity and persists status or decision requests in a local controller inbox. `sightmesh parent` reports durable delivery state instead of failing with `No recorded parent`.

## 5. Authoritative ownership

| Concern | Owner | Durable representation |
|---|---|---|
| Credential bytes and provider-specific launch material | Existing pool credential store or cdesktop provider store | Existing protected local files/database only |
| Account order, health, cooldown, and quota cache | SightMesh pool | Existing pool and state files |
| Fallback routes and operator policy | SightMesh settings | Versioned local settings file |
| Command identity, claim, attempt linkage, and terminal process state | cdesktop | Native durable command and execution records |
| Transcript and tool events | cdesktop | Native session/process transcript |
| Workspace and Git state | cdesktop and Git | Existing workspace records and repository |
| Approval for metered fallback | cdesktop | Native approval record linked to command transition |
| Fleet projection | SightMesh | Derived read model only, never a second source of truth |
| External launcher identity and pending escalation | SightMesh | Local launcher link and durable decision inbox; optional Conductor delivery when authenticated |

No new durable table is justified unless implementation proves a question cannot be reconstructed from these owners. Prefer extending existing command configuration and normalized process outcome fields.

## 6. Settings contract

Settings live in a versioned SightMesh configuration and are exposed in CLI and UI. Invalid values are rejected before mutation.

```yaml
executionRouting:
  enabled: true
  routes:
    - id: codex-luna-subscriptions
      executor: CODEX
      model: gpt-5.6-luna
      billingClass: subscription
      accountPool: codex
    - id: claude-opus-subscriptions
      executor: CLAUDE_CODE
      model: opus
      billingClass: subscription
      accountPool: claude
    - id: codex-metered-api
      executor: CODEX
      model: gpt-5.6-luna
      billingClass: metered
      account: codex-api
  meteredFallback: auto
  sameRouteRetries: 2
  transientBackoffSeconds: [5, 20]
  approvalTimeoutMinutes: 0
  allRoutesExhausted: block
  notifyOnSwap: true
  exposeAccountAlias: true
```

### Required settings

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `executionRouting.enabled` | boolean | `true` when a valid pool exists | Enable pool-backed visible launch and recovery |
| `routes` | ordered route list | subscription routes then metered | Model, executor, billing class, and account selector |
| `meteredFallback` | `auto`, `ask`, `never` | `auto` | Behavior when the next eligible route is metered |
| `sameRouteRetries` | 0 to 3 | `2` | Bounded retries for transient failures only |
| `transientBackoffSeconds` | bounded integer list | `[5, 20]` | Retry timing before route change |
| `approvalTimeoutMinutes` | 0 or positive integer | `0` | `0` means an `ask` approval remains pending until answered |
| `allRoutesExhausted` | `block` | `block` | No silent loop or unconfigured provider fallback |
| `notifyOnSwap` | boolean | `true` | Send a durable parent/UI notification |
| `exposeAccountAlias` | boolean | `true` | Show only the operator-defined safe alias |

### Useful secondary settings

- Optional per-workspace route override.
- Optional metered daily or per-task spend ceiling when provider usage data supports it.
- Allow or deny cross-executor fallback.
- Account cooldown override when a provider supplies no reset time.
- Route-level minimum remaining quota reserve.
- Notification verbosity.
- Approval scope: once, workspace, or global setting update. Initial implementation may ship only `once` plus the global setting.

Settings must never contain tokens, raw auth headers, expanded credential paths, or provider response bodies.

## 7. Authentication binding and new auth support

### Binding model

Every execution attempt receives an opaque `auth_binding_id`. The binding resolves at launch time to one owned account and provider-specific material:

- Claude subscription: `CLAUDE_CODE_OAUTH_TOKEN` or the provider-supported normal auth mechanism.
- Codex subscription: dedicated `CODEX_HOME` containing exactly that account's normal login.
- Metered API: provider-specific API key and supported environment/config injection.
- Custom provider headers: cdesktop-owned secret header references resolved at executor launch.

SightMesh passes the binding identifier and safe metadata only. cdesktop resolves launch material immediately before starting the executor and scrubs it from stored executor configuration, logs, errors, and transcripts.

### New authentication entries

- Pool selection derives candidates from the authoritative pool each time a new logical task starts or a fallback decision occurs.
- A newly added account requires no source change or hardcoded inventory update.
- Duplicate identity checks prevent the same underlying subscription from appearing twice under different aliases.
- Removed or credential-less entries are skipped with a safe reason.
- Changes do not rewrite an already running healthy attempt.

### Auth headers

- Header names may be configured only through the cdesktop provider/auth owner.
- Header values remain secret references, never SightMesh settings values.
- Only an executor adapter that declares support for a header may receive it.
- `Authorization`, proxy authorization, cookies, and provider-specific secret headers are always redacted.
- Normalized failures may preserve safe header-independent facts such as HTTP status, provider error code, reset time, and retry-after duration.

## 8. Route selection algorithm

For a new logical command:

1. Load and validate settings.
2. Enumerate routes in configured order.
3. For each route, enumerate its account selector in authoritative pool order.
4. Exclude missing credentials, explicit cooldowns, known zero quota, disabled providers, incompatible executor/model pairs, and the prior failed binding when its failure is binding-specific.
5. If the first eligible route is metered, apply `meteredFallback`.
6. Atomically claim the command and write the selected safe target snapshot and attempt number.
7. Resolve the auth binding only inside cdesktop launch.
8. Start exactly one process.

For recovery:

1. Read the normalized terminal outcome and current command attempt.
2. Confirm no active or uncertain process already owns the command.
3. Apply the failure classifier.
4. Retry the same route only for bounded transient outcomes.
5. Cool or disable the failing binding when appropriate.
6. Select the next eligible target from the current policy.
7. Create approval if required.
8. Requeue the same logical command with incremented attempt and the same dedupe identity.
9. Dispatch once.

Concurrent supervisors must converge through cdesktop's native claim and dedupe semantics. SightMesh remains a repeatable reconciler, not an independent claimant.

## 9. Failure classification

| Outcome | Examples | Action |
|---|---|---|
| `quota_exhausted` | weekly cap, zero remaining, explicit insufficient quota | Cool until provider reset or safe default, then select next route |
| `auth_expired` | expired OAuth, revoked login | Mark binding unhealthy, notify reauthentication, select next owned binding |
| `auth_invalid` | invalid key/header, account mismatch | Mark binding unhealthy; never retry same secret automatically |
| `model_unavailable` | model removed, access not granted | Try next compatible configured route without cooling unrelated accounts |
| `rate_limited_transient` | retry-after with remaining quota | Retry same route using bounded backoff, then fall back if exhausted |
| `network_transient` | connection reset, provider unavailable | Bounded same-route retry; do not cool the account |
| `user_stopped` | explicit stop | Terminal, no fallback |
| `task_failed` | tests fail, tool error, invalid code | Terminal task outcome, no provider fallback |
| `unknown` | unclassified error | Block for review; do not spend or duplicate |

cdesktop emits a stable normalized outcome enum plus safe structured fields. Raw provider text is retained only where already owned and redacted, and is never used as the sole durable classifier if a stable provider code exists.

## 10. Durable resume state machine

```text
queued
  -> selecting
  -> approval_pending          when next route is metered and policy is ask
  -> claimed
  -> running
  -> succeeded                terminal
  -> failed_task              terminal, no fallback
  -> recoverable_failure
       -> retry_wait          bounded same-route retry
       -> selecting           next route
  -> routes_exhausted         blocked terminal until settings/auth changes
```

Crash recovery rules:

- `selecting` without a claimed process may be recomputed.
- `claimed` with an uncertain process is observed, never relaunched blindly.
- Accepted stop/requeue requests reuse the same process-scoped idempotency key.
- An approval survives service and machine restart.
- A settings or pool change wakes `routes_exhausted` commands, but native claiming still prevents duplicate dispatch.
- Completion of any attempt closes the logical command and prevents later stale attempts from launching.
- A retired predecessor cannot be auto-resumed by a queued message. Delivery is rebound to the active successor by logical command identity or blocked without side effects.
- A failed workspace start leaves no active record without `container_ref`; compensation is idempotent across daemon restart.
- A manager without a cdesktop parent can persist completion, blocker, or decision-required status for its recorded Conductor launcher. Lack of Conductor API authentication may delay wake delivery but cannot lose the escalation.

## 11. Cross-executor handoff

A Codex-to-Claude or Claude-to-Codex fallback preserves:

- workspace and checked-out files;
- Git branch and dirty state;
- original user command and durable dedupe identity;
- safe checkpoint containing completed work, current tool state, exact HEAD, dirty paths, remaining scope, exclusions, and validation evidence;
- links to prior session and execution attempt.

The target executor starts in a linked successor session if cdesktop cannot safely change executor configuration within one session. The UI groups those sessions under one logical command and labels the provider/model transition. Raw transcript replay is not required; cdesktop transcript plus the bounded checkpoint is the handoff source.

## 12. Metered approval behavior

### `auto` default

- Select and launch the first eligible metered route.
- Emit a durable notification that subscription routes were exhausted and metered execution started.
- Display account alias, provider, model, and any configured spend ceiling.

### `ask`

- Create one native cdesktop approval linked to logical command and proposed target.
- Show `Allow once`, `Deny`, and a shortcut to change the global setting.
- Do not resolve or expose the secret while approval is pending.
- Approval resumes exactly once through the normal command dispatcher.
- Denial leaves the command blocked with its checkpoint intact.

### `never`

- Enter `routes_exhausted` with a clear explanation.
- Offer settings and authentication actions, but never start metered work.

## 13. UI and CLI surfaces

### One website

The primary product surface is the existing cdesktop local web application. Operators must not need a separate SightMesh website to manage routing or inspect agents.

- Add an `Agents` destination to the cdesktop application navigation.
- The Agents view shows the visible fleet, active logical commands, executor/model, safe account alias, billing class, quota/cooldown state, approvals, swaps, and attention states.
- Selecting an agent opens its existing cdesktop session/workspace view and a unified attempt timeline, rather than creating a parallel transcript UI.
- Add `Settings > Execution Routing` to the same application for auth pool health, route order, model targets, retry/cooldown policy, metered fallback, and optional spend controls.
- Reuse cdesktop's existing responsive layout, settings primitives, and approval surface.
- The current `sightmesh pool serve` page remains a narrow compatibility and recovery surface. It is not the main user journey and must not become a second product dashboard.
- cdesktop may call a loopback SightMesh service for derived pool/routing state, but browser code must use the existing cdesktop origin or a narrowly authenticated same-origin proxy. Do not expose credential-bearing pool files or unauthenticated cross-origin mutation endpoints.

### Built-in settings

- Toggle automatic routing.
- Reorder routes.
- Select executor, model, billing class, and account pool or fixed safe alias.
- Set metered fallback to auto, ask, or never.
- Configure retry/cooldown and optional spend limits.
- Validate that each route resolves to at least one supported target.

### Fleet and session visibility

- `active: codex-sub1 / CODEX / Luna / subscription`
- `swapping: quota exhausted, next Claude Opus via max-a`
- `approval required: subscriptions exhausted, metered Codex API proposed`
- `blocked: all configured routes unavailable`
- reset or retry timing when known.

### CLI

- `sightmesh routing show`
- `sightmesh routing validate`
- `sightmesh routing set-metered auto|ask|never`
- `sightmesh routing routes list|add|remove|order`
- `sightmesh routing explain --workspace <id>` for a safe selection trace.
- Existing `pool` commands remain the account owner and are not duplicated.

## 14. API contracts

### cdesktop normalized execution outcome

```json
{
  "class": "quota_exhausted",
  "provider_code": "insufficient_quota",
  "retry_after_seconds": null,
  "resets_at": "2026-08-25T04:00:00Z",
  "binding_scope": "account",
  "safe_message": "Subscription quota exhausted"
}
```

### Durable command attempt metadata

```json
{
  "logical_command_id": "...",
  "attempt": 2,
  "route_id": "claude-opus-subscriptions",
  "auth_binding_id": "opaque-local-id",
  "account_alias": "max-a",
  "executor": "CLAUDE_CODE",
  "model": "opus",
  "billing_class": "subscription",
  "policy_digest": "...",
  "predecessor_execution_process_id": "..."
}
```

Public projections omit `auth_binding_id` unless the local caller needs the opaque identifier. They never include resolved environment or headers.

## 15. Implementation lanes

### Lane A: cdesktop outcome and attempt contract

Owner: one cdesktop worker.

- Add normalized terminal outcome mapping for Claude Code and Codex.
- Preserve retry-after/reset facts.
- Extend existing durable command/execution metadata for logical command and attempt linkage.
- Add auth-binding reference to launch configuration without persisting resolved secrets.
- Add exact-once claim and stale-attempt guards.
- Add focused Rust tests and API contract fixtures.

### Lane B: cdesktop auth adapters and approvals

Owner: one cdesktop worker after Lane A contract stabilizes, or parallel only on non-overlapping adapter paths.

- Resolve Claude OAuth, Codex home, API key, and supported custom header bindings at launch.
- Redact every secret-bearing surface.
- Add native metered-fallback approval payload and resume path.
- Add process restart and approval persistence tests.

### Lane C: SightMesh routing policy and settings

Owner: one SightMesh worker.

- Add versioned settings model and migration from current profile/pool defaults.
- Implement ordered route validation and selector using the existing pool.
- Add metered auto/ask/never policy.
- Add safe selection explanations and CLI.
- Derive auth inventory from authoritative owners.

### Lane D: SightMesh auto-launch and reconciler integration

Owner: one SightMesh worker after Lane A contract is reviewable.

- Connect `spawn`, teammate spawn, queued dispatch, and durable recovery to route selection.
- Cool exhausted bindings and wake blocked work after reset or configuration change.
- Implement cross-executor checkpoint and successor linkage.
- Reuse cdesktop native claim/dedupe, existing durable reconciler, and stall service loop.

### Lane E: UI and observability

Owner: one cdesktop UI worker after settings and API contracts stabilize.

- Add routing settings.
- Show safe active route and swap timeline.
- Add metered approval actions.
- Hide secret identifiers and paths.

### Lane F: adversarial review and release evidence

Owner: independent visible reviewer.

- Attack duplicate dispatch, lost commands, stale completion, secret leakage, wrong-account reuse, restart races, approval bypass, and silent metered spend.
- Verify exact-head tests and diff scope.
- Run deterministic simulated quota/auth transitions for every route policy.
- Maintain an evidence matrix without creating a second runtime store.

## 16. Dependency and merge order

1. Stabilize and review the cdesktop outcome, attempt, auth-binding, and approval contracts.
2. Merge the cdesktop backend CI baseline repair separately so release changes have a trustworthy gate.
3. Implement SightMesh settings/selector against the reviewed contract.
4. Implement cdesktop adapters and SightMesh reconciler integration in parallel where ownership does not overlap.
5. Add UI after contract review.
6. Run independent adversarial review and exact-head CI.
7. Publish an explicit cdesktop prerelease only after operator approval.
8. Verify the artifact and update SightMesh's runtime lock in one reviewed change.
9. Run end-to-end release-candidate proof.
10. Keep all pull requests draft until the operator approves readiness.

## 17. Test matrix

### Selection

- First healthy subscription wins.
- Cooling, missing, disabled, duplicate, and zero-quota accounts are skipped in stable order.
- New auth entry is discovered without code change.
- Preferred model unavailable advances to the next route.
- Cross-provider fallback preserves the logical task.

### Metered policy

- `auto` launches one metered attempt after subscriptions exhaust.
- `ask` creates one durable approval and launches only after approval.
- `never` blocks without resolving API credentials.
- Policy change from blocked state wakes exactly once.

### Recovery

- SightMesh restart during selection.
- cdesktop restart after claim but before process start.
- machine restart during approval.
- quota failure after partial tool output.
- stale predecessor completes after successor starts.
- two supervisors reconcile the same command.
- unknown outcome blocks instead of retrying.

### Security

- Tokens, API keys, auth header values, credential paths, and reusable fingerprints absent from API, logs, CLI, UI, exceptions, snapshots, fixtures, and transcripts.
- Malformed or injected header names rejected.
- Only loopback and authorized local UI origins can mutate settings.
- File permissions remain owner-only.

### Compatibility

- Existing direct `pool exec` behavior remains.
- Existing spawn, teammate, message, steer, failover, stall, and durable command tests remain.
- Older cdesktop versions feature-disable routing with one bounded diagnostic.
- A single configured account still launches normally.

## 18. Release acceptance criteria

The feature is implementation-complete when:

- A visible Codex subscription task deterministically exhausts, cools its binding, and resumes exactly once on Claude Opus in the same workspace.
- Exhausting all subscription routes follows each metered policy correctly.
- A newly added auth entry participates in order without a code or mirrored-config update.
- Restart and concurrent-reconciler tests show zero lost and zero duplicate logical commands.
- Cross-executor handoff preserves Git state, checkpoint, and remaining scope.
- Ownership-transfer tests prove that messages to a retired predecessor create no process and cannot race the successor worktree.
- Failed-start tests prove that a workspace created before setup failure is safely compensated even when no container reference was assigned.
- Conductor-launched manager tests prove that `sightmesh parent --message` writes one durable escalation when no cdesktop parent exists and exposes it in the dashboard/controller inbox.
- Safe UI/CLI visibility explains every selection and transition.
- Secret scans and adversarial review find no credential exposure.
- cdesktop and SightMesh exact-head CI pass.
- README, architecture, security, compatibility, and release docs describe the same honest boundary.

The release proof is repeated real workload operation across these transitions. It is not one literal job left running for multiple weeks.

## 19. Manager operating rules

- Keep one writer for each contract or shared composition hotspot.
- Review stable checkpoints while non-overlapping work continues.
- Use visible SightMesh workers only.
- Queue messages while a worker remains on a valid course; steer only to prevent invalid or conflicting work.
- Require exact SHA, branch, dirty-state, checks, and remaining scope at every handoff.
- Keep PRs draft. Do not merge, publish, mark ready, change secrets, or dispatch releases without explicit operator approval.
- Ask the operator only for product policy choices that materially change behavior. The current metered default is decided: `auto`, with `ask` and `never` settings available.
