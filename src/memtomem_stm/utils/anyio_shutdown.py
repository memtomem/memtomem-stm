"""Detection helpers for AnyIO's cancel-scope shutdown errors.

AnyIO cancel scopes are task-affine: exiting one from a different task than
entered it raises a ``RuntimeError`` whose message is the only stable way to
recognize it. STM-owned MCP client contexts now use dedicated lifecycle owner
tasks, so this is no longer an expected construction. The exact-match helper
remains a narrow shutdown barrier for an upstream SDK unwind or a legacy
injected adapter; mixed exception groups are never classified as clean.
"""

from __future__ import annotations

_CANCEL_SCOPE_SHUTDOWN_MESSAGES = (
    "Attempted to exit a cancel scope that isn't the current tasks's current cancel scope",
    "Attempted to exit cancel scope in a different task than it was entered in",
)


def is_anyio_cancel_scope_shutdown_error(exc: RuntimeError) -> bool:
    """Return true for the AnyIO shutdown cleanup error seen on stdio EOF."""

    message = str(exc)
    return any(marker in message for marker in _CANCEL_SCOPE_SHUTDOWN_MESSAGES)


def is_clean_cancel_scope_shutdown(exc: BaseException) -> bool:
    """True when *exc* consists only of AnyIO cancel-scope shutdown errors.

    ``mcp.run()`` executes the server inside ``anyio.create_task_group()``,
    and anyio >= 4 (strict task groups) wraps anything escaping a task group
    in an ``ExceptionGroup`` — on stdio EOF the cancel-scope RuntimeError
    reaches ``main()`` in that wrapped shape, not bare. Walk groups
    recursively and treat the tree as a clean shutdown only when EVERY leaf
    is the cancel-scope error; any other leaf is a real failure that must
    hit the exception barrier.
    """
    if isinstance(exc, BaseExceptionGroup):
        return all(is_clean_cancel_scope_shutdown(sub) for sub in exc.exceptions)
    return isinstance(exc, RuntimeError) and is_anyio_cancel_scope_shutdown_error(exc)
