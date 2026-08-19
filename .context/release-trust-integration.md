# Release trust integration

`Lease.to_public_dict()` is the safe public representation. The excluded
`src/sightmesh/cli.py` must use it for `status` lease rows and all diagnostic
lease output; retain `to_dict()` only for private persistence and internal
handoffs that need the bearer token.

`src/sightmesh/conductor_migrate.py` also persists `lease.to_dict()` in a run
record. Its owner should confirm that record remains private or replace it with
an internal opaque reference before exposing it in diagnostics.
