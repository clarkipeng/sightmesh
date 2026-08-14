from __future__ import annotations

import logging
import os

LOGGER = logging.getLogger("sightmesh.stalls")

DEFAULT_THRESHOLD_MINUTES = 30
MAX_THRESHOLD_MINUTES = 24 * 60
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
    if not 1 <= minutes <= MAX_THRESHOLD_MINUTES:
        LOGGER.warning(
            "%s must be a whole number from 1 to %s; using %s minutes",
            THRESHOLD_ENV,
            MAX_THRESHOLD_MINUTES,
            DEFAULT_THRESHOLD_MINUTES,
        )
        return DEFAULT_THRESHOLD_MINUTES
    return minutes
