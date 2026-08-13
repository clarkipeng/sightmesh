"""Local orchestration for visible Claude and Codex workers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sightmesh")
except PackageNotFoundError:
    __version__ = "0+unknown"
