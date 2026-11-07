"""Record GC passes and loop stalls inside the timing test (probe only)."""

import gc
import time

import pytest

TARGET = "test_overall_deadline_includes_backoff_sleep"
_events: list[tuple[int, float, int]] = []
_active = [False]
_start = [0.0]
_lines: list[str] = []


def _callback(phase: str, info: dict) -> None:
    if not _active[0]:
        return
    if phase == "start":
        _start[0] = time.perf_counter()
    else:
        _events.append(
            (info["generation"], round(time.perf_counter() - _start[0], 4), info["collected"])
        )


gc.callbacks.append(_callback)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if TARGET not in item.nodeid:
        yield
        return
    _events.clear()
    counts = gc.get_count()
    _active[0] = True
    began = time.perf_counter()
    try:
        yield
    finally:
        _active[0] = False
        _lines.append(
            f"GCPROBE wall={time.perf_counter() - began:.4f}s counts_before={counts} "
            f"passes={_events}"
        )


def pytest_terminal_summary(terminalreporter):
    for line in _lines:
        terminalreporter.write_line(line)
