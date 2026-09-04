"""Task-fence state shared with the cdesktop HTTP boundary."""

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any


class FenceHeldError(RuntimeError):
    """A cdesktop request escaped a task fence's external-I/O boundary."""


HELD_TASK_FENCE: ContextVar[str | None] = ContextVar("held_task_fence", default=None)


def assert_external_io_allowed() -> None:
    """Reject network I/O unless the current task fence released its gate."""
    if task_id := HELD_TASK_FENCE.get():
        raise FenceHeldError(
            f"cdesktop request attempted while task fence {task_id!r} is held"
        )


def open_transport(opener: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Open any network transport only after the task fence is released."""
    assert_external_io_allowed()
    return opener(*args, **kwargs)
