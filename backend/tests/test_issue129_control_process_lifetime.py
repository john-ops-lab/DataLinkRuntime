"""Ambiguous thread-start failures must not duplicate Control lifecycle work."""

import threading

import pytest

from dlr.common.config import Settings
from dlr.control.services import control_process_v4


@pytest.mark.parametrize("launch_before_error", [False, True])
def test_lifecycle_start_failure_cannot_start_a_second_thread(monkeypatch, launch_before_error):
    process = control_process_v4.ControlProcess(
        Settings(DLR_WORKER_REMOTE_V4_ENABLED=False), None, None
    )
    entered, release = threading.Event(), threading.Event()
    launched = []
    original = threading.Thread.start

    def run():
        entered.set()
        release.wait(2)

    def fail_start(thread):
        if launch_before_error:
            original(thread)
            launched.append(thread)
            assert entered.wait(1)
        raise RuntimeError("ambiguous start")

    monkeypatch.setattr(process, "_run", run)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        with pytest.raises(ValueError):
            process.start()
        assert process._thread is not None
        assert process._stop.is_set()
        monkeypatch.setattr(threading.Thread, "start", original)
        with pytest.raises(ValueError):
            process.start()
        release.set()
        process.stop(timeout=1)
    finally:
        release.set()
        for thread in launched:
            thread.join(2)
            assert not thread.is_alive()


def test_probe_start_failure_keeps_one_unknown_inflight_probe(monkeypatch):
    process = control_process_v4.ControlProcess(
        Settings(DLR_WORKER_REMOTE_V4_ENABLED=False), None, None
    )
    process._state = "registered"
    process._confirmed_until = 60.0
    monkeypatch.setattr(control_process_v4, "_cache_clock", lambda: 1.0)
    monkeypatch.setattr(control_process_v4, "_PROBE_WAIT_SECONDS", 0.05)
    entered, release = threading.Event(), threading.Event()
    launched = []
    original = threading.Thread.start

    def probe(event):
        entered.set()
        release.wait(2)
        with process._lock:
            process._probe_result = "registered"
            process._probe_active = None
            event.set()

    def fail_first_start(thread):
        original(thread)
        launched.append(thread)
        if len(launched) == 1:
            assert entered.wait(1)
            raise RuntimeError("ambiguous start")

    monkeypatch.setattr(process, "_probe", probe)
    monkeypatch.setattr(threading.Thread, "start", fail_first_start)
    try:
        assert process.status().state == "unknown"
        assert process.status().state == "unknown"
        assert len(launched) == 1
    finally:
        release.set()
        for thread in launched:
            thread.join(2)
            assert not thread.is_alive()


def test_probe_start_failure_before_launch_stays_unknown_without_retries(monkeypatch):
    process = control_process_v4.ControlProcess(
        Settings(DLR_WORKER_REMOTE_V4_ENABLED=False), None, None
    )
    process._state = "registered"
    process._confirmed_until = 60.0
    monkeypatch.setattr(control_process_v4, "_cache_clock", lambda: 1.0)
    monkeypatch.setattr(control_process_v4, "_PROBE_WAIT_SECONDS", 0.01)
    attempts = []

    def fail_start(thread):
        attempts.append(thread)
        raise OSError("ambiguous start")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    assert process.status().state == "unknown"
    assert process.status().state == "unknown"
    assert len(attempts) == 1
