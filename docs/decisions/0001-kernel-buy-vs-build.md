# Decision 0001: hand-rolled SQLite kernel over a durable-execution framework

Status: proposed, founder review required.
Date: 2026-09-01.

## Question

Should the task kernel in `kernel-contract.md` be built on an existing durable-execution engine or hand-rolled on SQLite?

## Candidates

Temporal, Restate, and similar engines require an external orchestration server and a workflow-worker rearchitecture; both are disqualifying for a single-Mac, local-first tool.
DBOS Transact is the one credible option: an in-process Python library that checkpoints workflow steps into Postgres or SQLite with no server.

## Decision

Hand-rolled SQLite kernel.

DBOS durably replays workflow *steps*; our risk is not step replay but *invariant enforcement*: one holder per epoch, unique effects per `(task, epoch, request_hash)`, monotone terminal transitions, fenced transfers, typed mailbox lanes.
Those are schema constraints and guarded transitions that must be written either way, so the framework does not shrink the 1-2k line kernel; it adds a dependency and a second semantics on top of it.
The fork-loop class is prevented by unique indexes and check constraints, not by replay.
SightMesh is already Python plus SQLite on one machine, and the kernel is the product's core competence.

## Exit ramp

If the simulator shows bugs clustering in the wake-delivery plumbing (atomic claim, crash between state change and notify), adopt DBOS for that plumbing only, keeping the schema and invariants ours.
That is the single revisit trigger; no other component reopens this decision.
