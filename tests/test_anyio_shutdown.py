"""Direct unit tests for ``memtomem_stm.utils.anyio_shutdown``.

The two lifecycle entrypoints that rely on these helpers — ``server.main()``
and ``daemon.server.DaemonServer._teardown()`` — are exercised end-to-end in
``test_server_tools.py`` and ``tests/daemon/test_server.py`` via injected
``ExceptionGroup`` shapes. Those cover the helper transitively but not its
recursive ``BaseExceptionGroup`` walk in isolation: a regression that made a
group with one non-cancel leaf read as "clean" could slip past them depending
on how the entrypoint re-raises. These tests pin the pure-function contract
directly — every leaf must be the AnyIO cancel-scope error for the tree to
count as a clean shutdown.
"""

from __future__ import annotations

import pytest

from memtomem_stm.utils.anyio_shutdown import (
    _CANCEL_SCOPE_SHUTDOWN_MESSAGES,
    is_anyio_cancel_scope_shutdown_error,
    is_clean_cancel_scope_shutdown,
)


# The two AnyIO-version spellings of the cross-task cancel-scope message,
# pinned literally here (NOT derived from the implementation constant) so that
# dropping one from ``_CANCEL_SCOPE_SHUTDOWN_MESSAGES`` fails
# ``test_marker_constant_matches_expected`` instead of silently shrinking the
# parametrized sets below. The "tasks's" typo mirrors AnyIO's own message.
_EXPECTED_MARKERS = (
    "Attempted to exit a cancel scope that isn't the current tasks's current cancel scope",
    "Attempted to exit cancel scope in a different task than it was entered in",
)


def _cancel_error(marker: str = _EXPECTED_MARKERS[0]) -> RuntimeError:
    return RuntimeError(marker)


def test_marker_constant_matches_expected():
    # Guards the parametrize source: both spellings must stay in the
    # implementation constant — a removal flips this assertion rather than
    # quietly dropping a parametrized case.
    assert set(_CANCEL_SCOPE_SHUTDOWN_MESSAGES) == set(_EXPECTED_MARKERS)


class TestIsAnyioCancelScopeShutdownError:
    @pytest.mark.parametrize("marker", _EXPECTED_MARKERS)
    def test_each_known_marker_matches(self, marker: str):
        # Both anyio-version spellings of the cross-task cancel-scope message
        # must be recognized; the markers are the only stable signal.
        assert is_anyio_cancel_scope_shutdown_error(RuntimeError(marker)) is True

    def test_marker_matches_as_substring(self):
        # anyio prefixes/suffixes the message in practice (e.g. a traceback
        # note); the check is membership, not equality.
        wrapped = f"prefix: {_EXPECTED_MARKERS[0]} (suffix)"
        assert is_anyio_cancel_scope_shutdown_error(RuntimeError(wrapped)) is True

    def test_other_runtime_error_does_not_match(self):
        assert is_anyio_cancel_scope_shutdown_error(RuntimeError("real bug")) is False

    def test_empty_message_does_not_match(self):
        assert is_anyio_cancel_scope_shutdown_error(RuntimeError()) is False


class TestIsCleanCancelScopeShutdown:
    def test_bare_cancel_scope_error_is_clean(self):
        assert is_clean_cancel_scope_shutdown(_cancel_error()) is True

    @pytest.mark.parametrize("marker", _EXPECTED_MARKERS)
    def test_bare_cancel_scope_error_each_marker_is_clean(self, marker: str):
        assert is_clean_cancel_scope_shutdown(_cancel_error(marker)) is True

    def test_bare_other_runtime_error_is_not_clean(self):
        # The leaf check: a RuntimeError with a different message is a real
        # failure, not a shutdown.
        assert is_clean_cancel_scope_shutdown(RuntimeError("boom")) is False

    def test_non_runtime_error_leaf_is_not_clean(self):
        assert is_clean_cancel_scope_shutdown(ValueError("nope")) is False

    def test_single_level_group_all_cancel_is_clean(self):
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [_cancel_error(), _cancel_error(_EXPECTED_MARKERS[1])],
        )
        assert is_clean_cancel_scope_shutdown(group) is True

    def test_nested_group_all_cancel_is_clean(self):
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ExceptionGroup("unhandled errors in a TaskGroup", [_cancel_error()])],
        )
        assert is_clean_cancel_scope_shutdown(group) is True

    def test_mixed_group_with_non_cancel_leaf_is_not_clean(self):
        # The load-bearing case: one real failure anywhere in the tree must
        # flip the whole tree to "not clean" so it reaches the barrier.
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [_cancel_error(), ValueError("real bug")],
        )
        assert is_clean_cancel_scope_shutdown(group) is False

    def test_deeply_nested_non_cancel_leaf_is_not_clean(self):
        # A bad leaf buried under several group levels must still flip it —
        # guards the recursion, not just the top level.
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [
                _cancel_error(),
                ExceptionGroup(
                    "unhandled errors in a TaskGroup",
                    [ExceptionGroup("inner", [RuntimeError("buried real bug")])],
                ),
            ],
        )
        assert is_clean_cancel_scope_shutdown(group) is False

    def test_genuine_base_exception_group_with_base_exception_leaf_is_not_clean(self):
        # The helper's signature is ``BaseException`` and it walks
        # ``BaseExceptionGroup``, not just ``ExceptionGroup``. A group of only
        # Exception leaves auto-collapses to ``ExceptionGroup``, so the cases
        # above never construct a true ``BaseExceptionGroup``; a non-Exception
        # leaf (``KeyboardInterrupt``) keeps the type. Pin that the
        # ``BaseExceptionGroup`` branch runs on such an instance and that a
        # shutdown signal mixed with cancel-scope noise reads as a real failure
        # (the non-``RuntimeError`` ``BaseException`` leaf path).
        group = BaseExceptionGroup(
            "mixed base exceptions",
            [_cancel_error(), KeyboardInterrupt("real")],
        )
        assert isinstance(group, BaseExceptionGroup)
        assert not isinstance(group, ExceptionGroup)
        assert is_clean_cancel_scope_shutdown(group) is False
