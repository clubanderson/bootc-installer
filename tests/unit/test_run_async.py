"""Unit tests for RunAsync — core async execution wrapper.

RunAsync uses gi.repository.GLib.idle_add to schedule callbacks back on the
main thread. We stub gi before importing so these tests run without a display
or GLib main loop.
"""

import sys
import types
import threading
import time

import pytest

# ── Stub gi.repository.GLib before importing run_async ─────────────────────

def _build_gi_stubs():
    """Inject a minimal GLib stub so run_async.py can be imported headlessly."""
    idle_add_calls = []  # shared list to record calls during each test

    gi_mod = types.ModuleType("gi")
    repo_mod = types.ModuleType("gi.repository")

    glib_mod = types.ModuleType("gi.repository.GLib")

    def _idle_add(cb, *args):
        """Synchronously invoke the callback so tests can inspect results."""
        idle_add_calls.append((cb, args))
        cb(*args)   # run inline — no real GLib loop needed
        return len(idle_add_calls)

    glib_mod.idle_add = _idle_add
    glib_mod.SOURCE_REMOVE = False

    repo_mod.GLib = glib_mod
    gi_mod.repository = repo_mod
    gi_mod.require_version = lambda *a, **kw: None

    sys.modules["gi"] = gi_mod
    sys.modules["gi.repository"] = repo_mod
    sys.modules["gi.repository.GLib"] = glib_mod

    # Remove any cached import of run_async so it reloads with stubs.
    for mod_name in list(sys.modules):
        if "run_async" in mod_name:
            del sys.modules[mod_name]

    return idle_add_calls


_idle_add_calls = _build_gi_stubs()

from bootc_installer.utils.run_async import RunAsync  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────

def _run(task_func, callback=None, join_timeout=5):
    """Create a RunAsync, wait for it to finish, return captured callback args."""
    captured = []
    _idle_add_calls.clear()

    def _capture_callback(result, error):
        captured.append((result, error))

    cb = _capture_callback if callback is None else callback
    ra = RunAsync(task_func, cb)
    ra.join(timeout=join_timeout)
    # idle_add was called synchronously in our stub, so captured is filled now
    return captured, ra


# ── tests ────────────────────────────────────────────────────────────────────

class TestRunAsyncHappyPath:
    def test_result_propagated_to_callback(self):
        """Task return value is the first arg to callback; error is None."""
        captured, _ = _run(lambda: 42)
        assert len(captured) == 1
        result, error = captured[0]
        assert result == 42
        assert error is None

    def test_none_result(self):
        """Tasks returning None propagate None, not an error."""
        captured, _ = _run(lambda: None)
        result, error = captured[0]
        assert result is None
        assert error is None

    def test_complex_return_value(self):
        """Dicts, lists, and other objects propagate correctly."""
        payload = {"status": "ok", "disks": ["/dev/sda"]}
        captured, _ = _run(lambda: payload)
        result, error = captured[0]
        assert result == payload


class TestRunAsyncErrorHandling:
    def test_exception_caught_and_passed_to_callback(self):
        """Exceptions in the task are caught; callback receives (None, exc)."""
        boom = ValueError("disk not found")

        def raises():
            raise boom

        captured, _ = _run(raises)
        result, error = captured[0]
        assert result is None
        assert isinstance(error, ValueError)
        assert str(error) == "disk not found"

    def test_exception_does_not_propagate_to_thread(self):
        """Thread exits cleanly even when the task raises."""
        def raises():
            raise RuntimeError("boom")

        _, ra = _run(raises)
        assert not ra.is_alive(), "Thread should have exited after exception"

    def test_generic_exception_type(self):
        """Any BaseException subclass is captured."""
        class CustomError(Exception):
            pass

        captured, _ = _run(lambda: (_ for _ in ()).throw(CustomError("custom")))
        _, error = captured[0]
        assert isinstance(error, CustomError)


class TestRunAsyncCallback:
    def test_default_callback_none_does_not_crash(self):
        """Passing callback=None uses a no-op lambda — no AttributeError."""
        # Should not raise
        ra = RunAsync(lambda: "ok", callback=None)
        ra.join(timeout=5)

    def test_source_id_set_after_completion(self):
        """source_id is set to the return value of idle_add."""
        _, ra = _run(lambda: None)
        # Our stub returns len(calls), which is >= 1
        assert ra.source_id is not None
        assert isinstance(ra.source_id, int)


class TestRunAsyncThreadProperties:
    def test_thread_is_daemon_by_default(self):
        """Daemon flag is True so interpreter shutdown is not blocked."""
        ra = RunAsync(lambda: time.sleep(0.01), callback=None)
        assert ra.daemon is True
        ra.join(timeout=5)

    def test_thread_exits_after_task_completes(self):
        """Thread is not alive after task + callback finish."""
        _, ra = _run(lambda: 1)
        assert not ra.is_alive()
