"""Pure usage derivation; unavailable categories stay unknown."""
from __future__ import annotations
from collections.abc import Iterable, Mapping
def derive(events: Iterable[Mapping[str, object]]) -> dict[str, int | None]:
    total = 0; seen: set[str] = set(); unknown = False
    for event in events:
        key = str(event.get("request_id", ""))
        if not key or key in seen: continue
        seen.add(key); value = event.get("input_tokens")
        if not isinstance(value, int): unknown = True
        else: total += value + (event.get("output_tokens") if isinstance(event.get("output_tokens"), int) else 0)
    return {"tokens": None if unknown else total}
