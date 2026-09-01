# Lane UI-1 - spawn prompt collapse (plus picker label if free)

Base: cdesktop origin/main (post #17).

## 1. Spawn prompt collapse (the earned fix)
The backend knows which message is the spawn prompt at session creation (it is the prompt passed to spawn). Tag that message with a structured marker (e.g. kind: "spawn_prompt") in the persisted message payload. Frontend renders messages carrying the marker as a collapsed block with a short header (e.g. "Spawn instructions"), expandable on click. No content string-matching anywhere. Sessions without the marker (historical rows) render exactly as today.

Tests: one asserting the marker is written at spawn; one asserting the frontend component collapses/expands based on the marker, not content. Generated-types check if shared types change.

## 2. Executor label in the model picker (only if it rides along)
The model-selector payload is already built per executor server-side; add the executor display name as a field and render it in the picker header. Zero frontend guessing. If this turns out to touch more than a field and a label, stop and report - it is explicitly droppable.

## Guards
Bloat rules apply: smallest diff that makes the marker/label data-driven. No redesign of the picker, no new settings, no changes to message rendering beyond the marker conditional. Policy C: fmt, clippy workspace qa-mode -D warnings, targeted crate tests, frontend checks (lint/build/typecheck) if frontend touched, draft PR, self-ready on green, durable completion signal with exact head and evidence. Report BLOCKED before ending your turn.
