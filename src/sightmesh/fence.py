"""Task-fence state shared with the cdesktop HTTP boundary."""

from contextvars import ContextVar


class FenceHeldError(RuntimeError):
    """A cdesktop request escaped a task fence's external-I/O boundary."""


HELD_TASK_FENCE: ContextVar[str | None] = ContextVar("held_task_fence", default=None)


def assert_external_io_allowed() -> None:
    """Reject network I/O unless the current task fence released its gate."""
    if task_id := HELD_TASK_FENCE.get():
        raise FenceHeldError(
            f"cdesktop request attempted while task fence {task_id!r} is held"
        )
