## Scope

Clarify that handling a peer, parent, or approval message interrupts a visible worker but does not complete its owned assignment. The worker resumes unless the message replaced or invalidated the task.

## Validation

- Skill creator `quick_validate.py` with PyYAML: valid
- `git diff --check`
