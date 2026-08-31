"""Local orchestration for visible Claude and Codex workers."""

from importlib.metadata import PackageNotFoundError, version

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
