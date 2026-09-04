"""Local orchestration for visible Claude and Codex workers."""

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

_SDK_EXPORTS = frozenset(
    {
        "BatchResult",
        "Command",
        "SightMesh",
        "SightMeshError",
        "Worker",
        "WorkerSpec",
    }
)

if TYPE_CHECKING:
    from .sdk import BatchResult, Command, SightMesh, SightMeshError, Worker, WorkerSpec

try:
    __version__ = version("sightmesh")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "BatchResult",
    "Command",
    "SightMesh",
    "SightMeshError",
    "Worker",
    "WorkerSpec",
    "__version__",
]


def __getattr__(name: str) -> Any:
    """Load the SDK only for the package-root convenience exports."""
    if name not in _SDK_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import sdk

    value = getattr(sdk, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _SDK_EXPORTS)
