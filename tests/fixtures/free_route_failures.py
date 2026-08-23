"""Verbatim executor output captured from the free tier on 2026-08-23.

Recorded by running the real `opencode` CLI against the routed free model
(`opencode/x-preview-f-free`) and against names it does not serve, so the
classifier is exercised against the text an operator would actually see
rather than against text invented to match it.
"""

from __future__ import annotations

# opencode run --model opencode/x-preview-f-free-nope "hi"
MODEL_NOT_FOUND = (
    "Error: Model not found: opencode/x-preview-f-free-nope. "
    "Did you mean: x-preview-f-free, hy3-free, mimo-v2.5-free?\n"
    'Error: {\n'
    '  "name": "UnknownError",\n'
    '  "data": {\n'
    '    "message": "Unexpected server error. Check server logs for details.",\n'
    '    "ref": "err_34583aaa"\n'
    "  }\n"
    "}\n"
)

# opencode run --model nosuchprovider/nosuchmodel "hi"
UNKNOWN_PROVIDER = (
    "Error: Model not found: nosuchprovider/nosuchmodel.\n"
    'Error: {"name": "UnknownError", "data": {"message": "Unexpected server '
    'error. Check server logs for details.", "ref": "err_a4774ea6"}}\n'
)

# The provider answered, but not with a turn: the bare server-error body the
# free tier returns once the model name itself has resolved.
SERVER_ERROR = (
    'Error: {"name": "UnknownError", "data": {"message": "Unexpected server '
    'error. Check server logs for details.", "ref": "err_18c7c2be"}}\n'
)

# opencode run --model opencode/x-preview-f-free "say hi"  (succeeds)
HEALTHY = "> build · x-preview-f-free\n\nHi\n"
