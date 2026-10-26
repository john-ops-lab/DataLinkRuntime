"""No-I/O HTTP collection bounds, with the actual parser/Envelope builder."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from dlr.control.services import http_ingress as service
from dlr.control.services import http_ingress_group as grouping
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services import subscription_t1_unit
from dlr.control.services.event_envelope import validate_envelope


class Timer:
    def __init__(self, deadline, callback, args):
        self.deadline, self.callback, self.args = deadline, callback, args
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Loop:
    def __init__(self):
        self.now = 0.0
        self.timers = []
        self.futures = asyncio.new_event_loop()

    def time(self):
        return self.now

    def call_later(self, delay, callback, *args):
        timer = Timer(self.now + delay, callback, args)
        self.timers.append(timer)
        return timer

    def fire_due(self):
        for timer in list(self.timers):
            if not timer.cancelled and timer.deadline <= self.now:
                timer.cancel()
                timer.callback(*timer.args)

    def item(self, index, raw_bytes=2):
        # All fixtures are legal JSON bodies; collection itself must not parse.
        return grouping.HttpInput(
            None,
            str(index),
            b" " * (raw_bytes - 2) + b"{}",
            lambda: None,
            self.futures.create_future(),
        )


@pytest.fixture
def collector_clock(monkeypatch):
    clock = Loop()
    groups = []
    monkeypatch.setattr(grouping.asyncio, "get_running_loop", lambda: clock)
    collector = grouping.HttpCollector(lambda public, items: groups.append((public, items)))
    yield clock, collector, groups
    collector.close()
    clock.futures.close()


def test_sixteen_small_items_flush_without_wait_or_early_response(collector_clock):
    clock, collector, groups = collector_clock
    items = [clock.item(i) for i in range(17)]
    released = []
    for index, item in enumerate(items):
        item.release = lambda index=index: released.append(index)
        collector.add("route", item)
        if index < 15:
            assert not groups
    assert [len(items) for _, items in groups] == [16]
    assert collector.pending["route"].items == [items[-1]]
    assert not released and all(not item.future.done() for item in items)
    assert grouping.MAX_ITEMS == subscription_t1_unit.HTTP_MAX_ITEMS == 16
    collector.close()
    assert [len(items) for _, items in groups] == [16, 1]


@pytest.mark.parametrize("extra_raw_byte", [0, 1])
def test_exact_one_mib_estimate_flushes_and_overflow_starts_new_group(
    collector_clock, extra_raw_byte
):
    clock, collector, groups = collector_clock
    sizes = [9830] * 9 + [9834 + extra_raw_byte]
    assert sum(grouping.estimated_input_bytes(size) for size in sizes) == (
        1024**2 + 9 * extra_raw_byte
    )
    for index, size in enumerate(sizes):
        collector.add("route", clock.item(index, size))
    if not extra_raw_byte:
        assert [len(items) for _, items in groups] == [10]
        assert not collector.pending
    else:
        assert [len(items) for _, items in groups] == [9]
        assert len(collector.pending["route"].items) == 1
    collector.close()
    assert [item.key for _, items in groups for item in items] == list(map(str, range(10)))
    assert all(
        sum(grouping.estimated_input_bytes(len(item.body)) for item in items) <= 1024**2
        for _, items in groups
    )


def test_maximum_legal_bodies_split_before_exceeding_one_mib(collector_clock):
    clock, collector, groups = collector_clock
    for index in range(4):
        collector.add("route", clock.item(index, 64 * 1024))
    assert [len(items) for _, items in groups] == [3]
    assert collector.pending["route"].estimated_bytes == 320 * 1024
    collector.close()
    assert [len(items) for _, items in groups] == [3, 1]


def test_timer_flushes_at_twenty_five_ms_and_never_moves_first_deadline(collector_clock):
    clock, collector, groups = collector_clock
    collector.add("route", clock.item(0))
    clock.now = 0.024999
    collector.add("route", clock.item(1))
    clock.fire_due()
    assert not groups and collector.pending["route"].deadline == 0.025
    clock.now = 0.025
    clock.fire_due()
    assert [[item.key for item in items] for _, items in groups] == [["0", "1"]]
    assert not collector.pending


def test_expired_group_rejects_late_join_even_if_timer_has_not_run(collector_clock):
    clock, collector, groups = collector_clock
    collector.add("route", clock.item(0))
    clock.now = 0.025
    collector.add("route", clock.item(1))
    assert [[item.key for item in items] for _, items in groups] == [["0"]]
    assert collector.pending["route"].deadline == 0.050
    clock.fire_due()  # The cancelled old callback must not flush the new group.
    assert len(groups) == 1
    clock.now = 0.050
    clock.fire_due()
    assert [[item.key for item in items] for _, items in groups] == [["0"], ["1"]]


def test_routes_keep_independent_timers_and_close_preserves_all_items(collector_clock):
    clock, collector, groups = collector_clock
    collector.add("first", clock.item(0))
    clock.now = 0.010
    collector.add("second", clock.item(1))
    clock.now = 0.025
    clock.fire_due()
    assert [public for public, _ in groups] == ["first"]
    collector.close()
    assert [public for public, _ in groups] == ["first", "second"]


def test_collection_does_not_parse_or_change_headers_or_body(collector_clock, monkeypatch):
    clock, collector, groups = collector_clock

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("Collection must not decode JSON")

    monkeypatch.setattr(service, "parse_body", forbidden_parse)
    item = clock.item(0)
    item.body, item.authorization, item.key = b"invalid-json", "Bearer unchanged", "same-key"
    collector.add("route", item)
    collector.close()
    assert groups == [("route", [item])]
    assert (item.authorization, item.key, item.body) == (
        "Bearer unchanged",
        "same-key",
        b"invalid-json",
    )
    assert not item.future.done()


@pytest.mark.parametrize(
    "body",
    [
        b"0",
        b"null",
        b"1e9",
        b"-0e0",
        b"5e-324",
        b"-2.2250738585072014e-308",
        b"9007199254740991",
        b"[" + b",".join([b"1e9"] * 4095) + b"]",
        b"[" * 31 + b"0" + b"]" * 31,
        b'"' + b"x" * 65534 + b'"',
        b" " * 65534 + b"[]",
        json.dumps({"中文": "火😀" * 100}, ensure_ascii=False).encode(),
        json.dumps({"escaped": '"\\\b\t\n\f\r\x01中😀' * 100}).encode(),
    ],
    ids=[
        "smallest-integer",
        "null",
        "short-exponent-expansion",
        "negative-zero",
        "smallest-subnormal",
        "longest-negative-float",
        "largest-safe-integer",
        "maximum-nodes-floats",
        "maximum-depth",
        "maximum-string-body",
        "maximum-whitespace-body",
        "literal-unicode",
        "escaped-strings",
    ],
)
def test_actual_envelope_with_maximum_metadata_fits_length_only_estimate(monkeypatch, body):
    fixed = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    monkeypatch.setattr(service, "datetime", SimpleNamespace(now=lambda _: fixed))
    # Database-length maxima, including worst-case JSON metadata escaping;
    # normal generated route/epoch values are only 32 ASCII hex characters.
    key = service.require_key('"\\' * 64)
    payload = service.parse_body(body)
    envelope = service._build_envelope(2**63 - 1, 2**63 - 1, "\x01" * 64, "\x01" * 64, key, payload)
    _, actual_envelope = validate_envelope(envelope)
    assert actual_envelope == accounting.compact_bytes(envelope)
    assert actual_envelope <= service.MAX_ENVELOPE_BYTES
    assert actual_envelope + len(body) <= grouping.estimated_input_bytes(len(body))
    metadata = accounting.compact_bytes({**envelope, "data": {**envelope["data"], "payload": None}})
    assert metadata < 16 * 1024
    assert envelope["received_at"] == fixed.isoformat()
    assert envelope["data"]["payload"] == payload


def test_supported_numeric_tokens_never_exceed_eightfold_serialization():
    # Exercise every decimal exponent spanning binary64, including underflow,
    # plus shortest expanding integral floats. Rejected values stay rejected.
    bodies = [
        f"{sign}{base}e{exponent}".encode()
        for sign in ("", "-")
        for base in ("1", "5", "1.2345678901234567")
        for exponent in range(-325, 16)
    ]
    for body in bodies:
        payload = service.parse_body(body)
        assert accounting.compact_bytes(payload) <= 8 * len(body)
