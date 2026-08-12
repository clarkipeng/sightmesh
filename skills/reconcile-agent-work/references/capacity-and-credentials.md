# Capacity and credential policy

## Supported behavior

- Detect provider rate limits, context exhaustion, authentication expiry, and unavailable models.
- Pause new requests and preserve exact work state.
- Resume through another provider profile or API key that the user explicitly configured through the provider's supported interface.
- Map supported cdesktop providers to secret-free SightMesh profile names and mark automatic failover only on API or enterprise profiles.
- Start the replacement as a visible cdesktop successor with `sightmesh failover`; keep the prior session and transcript until reconciliation is complete.
- Keep each provider profile isolated and label the transition in the handoff.
- Respect each provider's rate limits, subscription terms, organization policy, and billing controls.

## Prohibited behavior

- Extracting or replaying auth headers, cookies, refresh tokens, bearer tokens, session databases, or keychain contents.
- Automatically cycling consumer Claude Max, ChatGPT, Codex, or similar subscription accounts to avoid usage limits.
- Sharing one person's session credential with another process or worker.
- Concealing the effective account, provider, model, or billing identity.
- Retrying indefinitely after a provider communicates a hard usage limit.

## Failover record

Record:

- exhausted provider and model;
- observed error and time;
- last completed action;
- commands with uncertain outcome;
- destination provider profile and model;
- approving user or preconfigured policy;
- resumed cdesktop workspace and session IDs;
- validation that the resumed worker read the correct repository, branch, HEAD, and handoff before writing.
