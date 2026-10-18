"""Timed-out callers must not create unbounded detached DNS transport workers."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import request

import pytest

from dlr.control.ai import providers


def test_dns_retries_keep_at_most_the_legacy_40_worker_capacity(monkeypatch):
    release = threading.Event()
    workers = []
    lock = threading.Lock()

    def blocked_dns(*args, **kwargs):
        with lock:
            workers.append(threading.current_thread())
        release.wait(10)
        raise providers.socket.gaierror("review DNS stalled")

    opener = request.build_opener(
        request.ProxyHandler({}),
        providers._NoRedirectHandler(),
        providers._DeadlineHTTPHandler(),
        providers._DeadlineHTTPSHandler(),
    )
    monkeypatch.setattr(providers.socket, "getaddrinfo", blocked_dns)
    monkeypatch.setattr(providers, "_NO_REDIRECT_OPENER", opener)

    def retries():
        for _ in range(12):
            with pytest.raises(providers.AiProviderError) as error:
                providers._request_json(
                    "GET",
                    "http://dns.review.invalid/",
                    {},
                    not_found_code="ai_model_not_found",
                    timeout_seconds=0.03,
                )
            assert error.value.code == "ai_timeout"

    try:
        with ThreadPoolExecutor(max_workers=4) as callers:
            futures = [callers.submit(retries) for _ in range(4)]
            for future in futures:
                future.result(timeout=6)
        # All 48 caller attempts finished. The last eight could not start a
        # worker: timed-out callers did not release slots still held by DNS.
        assert len(workers) == 40
        assert all(worker.is_alive() for worker in workers)
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
    assert not any(worker.is_alive() for worker in workers)
    # Actual termination restores all permits for unrelated later requests.
    if hasattr(providers, "_TRANSPORT_PERMITS"):
        permits = providers._TRANSPORT_PERMITS
        held = 0
        try:
            while permits.acquire(blocking=False):
                held += 1
            assert held == 40
        finally:
            for _ in range(held):
                permits.release()


def test_capacity_wait_consumes_original_deadline_without_starting_worker(monkeypatch):
    permits = threading.BoundedSemaphore(1)
    permits.acquire()
    monkeypatch.setattr(providers, "_TRANSPORT_PERMITS", permits)
    monkeypatch.setattr(
        threading.Thread, "start", lambda self: pytest.fail("started without capacity")
    )
    started = time.monotonic()
    try:
        with pytest.raises(providers.AiProviderError) as error:
            providers._request_json(
                "GET",
                "http://review.invalid/",
                {},
                not_found_code="ai_model_not_found",
                timeout_seconds=0.05,
            )
        assert error.value.code == "ai_timeout"
        assert time.monotonic() - started < 0.3
    finally:
        permits.release()


def test_transport_start_failure_releases_its_permit(monkeypatch):
    permits = threading.BoundedSemaphore(1)
    monkeypatch.setattr(providers, "_TRANSPORT_PERMITS", permits)

    def fail_start(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread start failed"):
        providers._bounded_http_read(request.Request("http://review.invalid/"), 0.1)
    assert permits.acquire(blocking=False)
    permits.release()
