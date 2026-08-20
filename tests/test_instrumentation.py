"""
Tests for src/diagnostics/instrumentation.py.

Installing the wrappers rebinds threading.Thread.start and
multiprocessing.Process.start process-wide, so every test that installs
restores the originals first. A leaked wrapper would follow the rest of the
suite into every test that starts a thread.

The multiprocessing wrapper is asserted by installation, not by spawning:
starting a real process to cover ten lines of logging is a poor trade against
the flakiness it invites in a sandboxed runner.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import threading

import pytest

from src.diagnostics import instrumentation as inst


@pytest.fixture
def restore_starters():
    """Snapshot and restore the two class attributes install() rebinds."""
    thread_start = threading.Thread.start
    process_start = multiprocessing.Process.start
    yield
    threading.Thread.start = thread_start
    multiprocessing.Process.start = process_start


# ---------------------------------------------------------------------------
# _short_repr / _stack_snippet
# ---------------------------------------------------------------------------


def test_short_repr_leaves_a_small_value_alone():
    assert inst._short_repr(42) == "42"


def test_short_repr_truncates_and_marks_the_cut():
    out = inst._short_repr("x" * 500, limit=10)
    assert out.endswith("...")
    assert len(out) == 13


def test_short_repr_falls_back_to_the_type_when_repr_raises():
    # A half-constructed object whose __repr__ explodes must not take the
    # instrumentation down with it — this runs on every task creation.
    class _Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    assert inst._short_repr(_Hostile()) == "_Hostile"


def test_stack_snippet_returns_formatted_frames():
    # The last two frames are dropped, which at the real call sites is the
    # wrapper plus this helper — so the spawner stays visible. Called
    # directly, as here, the caller is what gets dropped.
    snippet = inst._stack_snippet()
    assert isinstance(snippet, str)
    assert "File" in snippet


def test_stack_snippet_honours_the_frame_cap():
    assert isinstance(inst._stack_snippet(max_frames=1), str)


# ---------------------------------------------------------------------------
# thread wrapper
# ---------------------------------------------------------------------------


def test_thread_start_is_wrapped_and_the_thread_still_runs(restore_starters):
    before = threading.Thread.start
    inst._wrap_thread_start()
    assert threading.Thread.start is not before

    seen = []
    t = threading.Thread(target=lambda: seen.append("ran"), name="probe")
    t.start()
    t.join(timeout=5)

    assert seen == ["ran"]


def test_thread_wrapper_survives_a_thread_with_no_target(restore_starters):
    inst._wrap_thread_start()
    t = threading.Thread(name="empty")
    t.start()
    t.join(timeout=5)


# ---------------------------------------------------------------------------
# multiprocessing wrapper
# ---------------------------------------------------------------------------


def test_multiprocess_start_is_rebound(restore_starters):
    before = multiprocessing.Process.start
    inst._wrap_multiprocess_start()
    assert multiprocessing.Process.start is not before


# ---------------------------------------------------------------------------
# asyncio task factory
# ---------------------------------------------------------------------------


async def test_task_factory_still_produces_working_tasks():
    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()
    try:
        inst._install_asyncio_task_factory(loop)
        assert loop.get_task_factory() is not None

        async def _answer():
            return 42

        assert await loop.create_task(_answer()) == 42
    finally:
        loop.set_task_factory(original)


async def test_task_factory_install_failure_is_logged_not_raised():
    class _RefusingLoop:
        def get_task_factory(self):
            return None

        def set_task_factory(self, factory):
            raise RuntimeError("this loop refuses factories")

    # Fails open: instrumentation must never be able to abort startup.
    inst._install_asyncio_task_factory(_RefusingLoop())


# ---------------------------------------------------------------------------
# install_instrumentation / log_manual_event
# ---------------------------------------------------------------------------


def test_install_is_a_no_op_when_disabled_by_env(monkeypatch, restore_starters):
    monkeypatch.setattr(inst, "_ENABLED", False)
    before = threading.Thread.start
    loop = asyncio.new_event_loop()
    try:
        inst.install_instrumentation(loop=loop)
    finally:
        loop.close()

    assert threading.Thread.start is before


def test_install_wires_every_hook(monkeypatch, restore_starters):
    monkeypatch.setattr(inst, "_ENABLED", True)
    loop = asyncio.new_event_loop()
    try:
        before_thread = threading.Thread.start
        inst.install_instrumentation(loop=loop)
        assert threading.Thread.start is not before_thread
        assert loop.get_task_factory() is not None
    finally:
        loop.close()


def test_install_survives_a_failing_hook(monkeypatch, restore_starters):
    # Each hook is independent: one refusing to install must not prevent the
    # others, and must not raise into the caller's startup path.
    monkeypatch.setattr(inst, "_ENABLED", True)

    def _boom(*_a, **_k):
        raise RuntimeError("hook exploded")

    monkeypatch.setattr(inst, "_install_asyncio_task_factory", _boom)
    before_thread = threading.Thread.start

    inst.install_instrumentation()

    assert threading.Thread.start is not before_thread


def test_log_manual_event_accepts_arbitrary_context():
    inst.log_manual_event("probe", symbol="BTC/USDT", size=1)


def test_log_manual_event_swallows_a_hostile_value():
    class _Hostile:
        def __repr__(self):
            raise RuntimeError("no repr")

    inst.log_manual_event("probe", obj=_Hostile())
