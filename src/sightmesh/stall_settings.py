from __future__ import annotations

import logging
import os

LOGGER = logging.getLogger("sightmesh.stalls")

DEFAULT_THRESHOLD_MINUTES = 30
THRESHOLD_ENV = "SIGHTMESH_STALL_THRESHOLD_MINUTES"


def threshold_minutes() -> int:
    """Read a safe stall threshold without allowing bad environment to stop service."""
    raw = os.environ.get(THRESHOLD_ENV)
    if raw is None:
        return DEFAULT_THRESHOLD_MINUTES
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 0
    if minutes < 1:
        LOGGER.warning(
            "%s must be a positive whole number; using %s minutes",
            THRESHOLD_ENV,
            DEFAULT_THRESHOLD_MINUTES,
        )
        return DEFAULT_THRESHOLD_MINUTES
    return minutes
