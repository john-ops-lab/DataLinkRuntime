"""Targeted fixture tests; no live intake/Worker/capacity acceptance claims."""

import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.ingress.workload import (
    BODY_BYTES,
    HTTP_FOLLOWUP_STARTS_PER_SECOND,
    KINDS,
    MANIFEST_MAX_BYTES,
    FixtureError,
    HttpReply,
    HttpSender,
    HttpTransport,
    PrivateLog,
    Publisher,
    generate,
    input_record,
    read_inputs,
    retry_after_seconds,
    verify_rows,
    workload_count,
)
from tools.ingress_b0.test_workload import (
    FakeChannel,
    FakeConnection,
    MemorySink,
    confirmation,
)


def config(root, *, kind="warmup", run_id="test-run"):
    return {
        "run_id": run_id,
        "kind": kind,
        "count": KINDS[kind],
        "input_path": str(root / "inputs.private.jsonl"),
        "output_path": str(root),
        "ingress_url": "https://intake.example/api/ingress/public",
    }


def reply(identity=1):
    return HttpReply(
        202,
        json.dumps(
            {
                "receipt_id": identity,
                "processing_status": "pending",
                "status_url": f"/api/ingress/public/receipts/{identity}",
            }
        ).encode(),
    )


class Clock:
    value = 0.0

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


class ManifestTests(unittest.TestCase):
    def test_counts_are_explicit_and_cannot_be_interchanged(self):
        for kind, count in KINDS.items():
            self.assertEqual(workload_count({"kind": kind, "count": count}), count)
            with self.assertRaisesRegex(FixtureError, "workload_count_mismatch"):
                workload_count({"kind": kind, "count": count + 1})
        for value in (
            {},
            {"kind": "capacity"},
            {"kind": "warmup", "count": True},
            {"kind": "baseline", "count": 200, "warmup": False},
        ):
            with self.assertRaises(FixtureError):
                workload_count(value)

    def test_ten_thousand_exact_bodies_hashes_and_manifest_remain_within_budget(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root), kind="capacity")
            result = generate(cfg)
            self.assertLess(Path(cfg["input_path"]).stat().st_size, MANIFEST_MAX_BYTES)
            self.assertEqual(
                result["manifest_sha256"],
                hashlib.sha256(Path(cfg["input_path"]).read_bytes()).hexdigest(),
            )
            inputs = read_inputs(cfg)
            self.assertEqual(len(inputs), 10000)
            for item in (inputs[0], inputs[9999]):
                raw = base64.b64decode(item["raw_base64"], validate=True)
                self.assertEqual(len(raw), BODY_BYTES)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), item["raw_sha256"])
                canonical = json.dumps(
                    json.loads(raw),
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode()
                self.assertEqual(
                    hashlib.sha256(canonical).hexdigest(), item["payload_sha256"]
                )
                self.assertNotEqual(item["payload_sha256"], item["raw_sha256"])
            self.assertNotEqual(
                inputs[0]["message_id"], input_record("other-run", 1)["message_id"]
            )
            with self.assertRaises(FileExistsError):
                generate(cfg)

    def test_formal_manifest_cannot_be_truncated_or_changed(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root), kind="baseline")
            generate(cfg)
            path = Path(cfg["input_path"])
            original = path.read_text().splitlines()
            path.write_text("\n".join(original[:20]) + "\n")
            with self.assertRaisesRegex(FixtureError, "invalid_manifest_count"):
                read_inputs(cfg)
            row = json.loads(original[0])
            row["payload_sha256"] = "0" * 64
            path.write_text(json.dumps(row) + "\n" + "\n".join(original[1:]) + "\n")
            with self.assertRaisesRegex(FixtureError, "manifest_content_mismatch"):
                read_inputs(cfg)

    def test_evidence_preserves_first_bytes_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "evidence"
            sink = PrivateLog(path, max_bytes=16)
            sink.append({"first": 1})
            with self.assertRaisesRegex(FixtureError, "evidence_limit_reached"):
                sink.append({"second": 2})
            sink.close()
            self.assertEqual(path.read_text(), '{"first":1}\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                PrivateLog(path)


class HttpTests(unittest.TestCase):
    def make(self, send, *, count=1, clock=None, concurrency=128):
        cfg = config(Path("/tmp/not-used"))
        cfg["http_concurrency"] = concurrency
        kind = "warmup" if count <= 20 else "capacity"
        inputs = [input_record("test-run", i, kind=kind) for i in range(1, count + 1)]
        sink = MemorySink()
        kwargs = {"clock": clock.now, "sleep": clock.sleep} if clock else {}
        return HttpSender(cfg, inputs, sink, send, **kwargs), sink

    def test_transient_rejection_keeps_original_identity_and_bytes(self):
        clock = Clock()
        seen = []

        async def send(method, row, receipt_id):
            seen.append((method, row["message_id"], row["raw_base64"]))
            return HttpReply(429, retry_after="2") if len(seen) == 1 else reply()

        sender, sink = self.make(send, clock=clock)
        result = asyncio.run(sender.run())
        self.assertTrue(result["all_responses_accepted"])
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(result["request_amplification"], 2)
        self.assertEqual(result["counts"]["first_rejected"], 1)
        self.assertEqual(clock.value, 2)
        self.assertEqual(len(sink.rows), 2)

    def test_unknown_without_receipt_retries_same_id_without_guessing_a_query(self):
        clock = Clock()
        methods = []

        async def send(method, row, receipt_id):
            methods.append(method)
            if len(methods) == 1:
                raise TimeoutError("secret endpoint detail")
            return reply()

        sender, sink = self.make(send, clock=clock)
        result = asyncio.run(sender.run())
        self.assertEqual(methods, ["POST", "POST"])
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertNotIn("secret endpoint", json.dumps(sink.rows))

    def test_unknown_with_receipt_queries_only_the_known_id(self):
        methods = []

        async def send(method, row, receipt_id):
            methods.append((method, receipt_id))
            return (
                HttpReply(202, b'{"receipt_id":9}')
                if method == "POST"
                else HttpReply(200, reply(9).body)
            )

        sender, sink = self.make(send)
        result = asyncio.run(sender.run())
        self.assertEqual(methods, [("POST", None), ("GET", 9)])
        self.assertTrue(result["all_responses_accepted"])
        self.assertTrue(sink.rows[0]["queried"])

    def test_permanent_rejection_has_no_hidden_retry(self):
        async def send(*args):
            return HttpReply(401)

        sender, _ = self.make(send)
        result = asyncio.run(sender.run())
        self.assertEqual(result["counts"]["post_requests"], 1)
        self.assertFalse(result["all_responses_accepted"])

    def test_twenty_attempt_limit_and_sixty_second_deadline_are_separate(self):
        async def short(*args):
            return HttpReply(503, retry_after="1")

        sender, _ = self.make(short, clock=Clock())
        result = asyncio.run(sender.run())
        self.assertEqual(result["counts"]["post_requests"], 20)
        self.assertFalse(result["all_responses_accepted"])

        async def long(*args):
            return HttpReply(503, retry_after="30")

        sender, _ = self.make(long, clock=Clock())
        result = asyncio.run(sender.run())
        self.assertEqual(result["counts"]["post_requests"], 2)
        self.assertFalse(result["all_responses_accepted"])

    def test_first_window_cannot_be_extended_to_hide_unsent_ids(self):
        async def send(*args):
            raise AssertionError("late request must not start")

        clock = Clock()
        sender, _ = self.make(send, clock=clock)
        clock.value = 6
        result = asyncio.run(sender.run())
        self.assertFalse(result["burst_window_passed"])
        self.assertEqual(result["counts"]["first_not_sent"], 1)
        self.assertEqual(result["id_outcomes"][0]["attempts"], 0)

    def test_concurrency_is_real_and_never_exceeds_the_frozen_window(self):
        async def send(method, row, receipt_id):
            await asyncio.sleep(0.002)
            return reply(row["seq"])

        sender, _ = self.make(send, count=20, concurrency=3)
        result = asyncio.run(sender.run())
        self.assertEqual(result["concurrency_peak"], 3)
        self.assertTrue(result["all_responses_accepted"])
        with self.assertRaisesRegex(FixtureError, "invalid_http_concurrency"):
            self.make(send, concurrency=129)

    def test_retry_after_is_not_wall_clock_math_or_silently_accepted(self):
        for value in (None, "0", "31", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT"):
            self.assertEqual(retry_after_seconds(value), (1, False))
        self.assertEqual(retry_after_seconds("30"), (30, True))

    def test_known_receipt_query_cannot_overtake_any_first_request(self):
        first = set()
        violations = []

        async def send(method, row, receipt_id):
            if method == "POST":
                first.add(row["seq"])
                return (
                    HttpReply(202, b'{"receipt_id":1}')
                    if row["seq"] == 1
                    else reply(row["seq"])
                )
            if len(first) != 20:
                violations.append(len(first))
            return HttpReply(200, reply(receipt_id).body)

        sender, _ = self.make(send, count=20, concurrency=1)
        result = asyncio.run(sender.run())
        self.assertEqual(violations, [])
        self.assertEqual(first, set(range(1, 21)))
        self.assertTrue(result["all_responses_accepted"])
        self.assertEqual(result["counts"]["first_unknown"], 1)
        self.assertEqual(result["concurrency_peak"], 1)

    def test_backoff_owns_no_request_slot_and_first_round_precedes_retry(self):
        async def run():
            all_first = asyncio.Event()
            release_backoff = asyncio.Event()
            first = set()
            attempts = {}
            violations = []

            async def send(method, row, receipt_id):
                identity = row["seq"]
                attempts[identity] = attempts.get(identity, 0) + 1
                if attempts[identity] == 1:
                    first.add(identity)
                    if len(first) == 20:
                        all_first.set()
                    if identity == 1:
                        return HttpReply(503, retry_after="1")
                elif len(first) != 20:
                    violations.append(len(first))
                return reply(identity)

            sender, _ = self.make(send, count=20, concurrency=1)

            async def backoff(_seconds):
                await release_backoff.wait()

            sender.sleep = backoff
            pending = asyncio.create_task(sender.run())
            await asyncio.wait_for(all_first.wait(), 1)
            # All other IDs used the sole request slot while ID 1 was asleep.
            self.assertEqual(len(first), 20)
            self.assertEqual(attempts[1], 1)
            release_backoff.set()
            result = await pending
            self.assertEqual(violations, [])
            self.assertTrue(result["all_responses_accepted"])
            self.assertEqual(result["counts"]["post_requests"], 21)

        asyncio.run(run())

    def test_missed_first_deadline_is_accounted_before_queries_without_resetting_t0(
        self,
    ):
        clock = Clock()
        observed = []
        sender = None

        async def send(method, row, receipt_id):
            if method == "POST":
                clock.value = 6
                return HttpReply(202, b'{"receipt_id":1}')
            observed.append(len(sender.first_accounted))
            return HttpReply(200, reply(receipt_id).body)

        sender, _ = self.make(send, count=20, clock=clock, concurrency=1)
        result = asyncio.run(sender.run())
        self.assertEqual(observed, [20])
        self.assertEqual(result["counts"]["first_not_sent"], 19)
        self.assertFalse(result["burst_window_passed"])
        self.assertFalse(result["all_responses_accepted"])
        self.assertEqual(sender.start, 0)


class HttpCoordinatorTests(unittest.TestCase):
    make = HttpTests.make

    def test_ten_thousand_ids_have_only_the_frozen_number_of_request_tasks(self):
        seen_tasks = []

        async def send(method, row, receipt_id):
            if not seen_tasks:
                seen_tasks.append(len(asyncio.all_tasks()))
            await asyncio.sleep(0)
            return reply(row["seq"])

        sender, sink = self.make(send, count=10000, concurrency=125, clock=Clock())
        result = asyncio.run(sender.run())
        self.assertTrue(result["all_responses_accepted"])
        self.assertLessEqual(seen_tasks[0], 127)
        self.assertEqual(result["scheduler"]["request_tasks_peak"], 125)
        self.assertEqual(result["scheduler"]["deferred_items_peak"], 0)
        self.assertEqual(result["concurrency_peak"], 125)
        self.assertEqual(len(sink.rows), 10000)

    def test_all_rejected_firsts_do_not_park_workers_or_overtake_first_round(self):
        first, attempts = set(), {}

        async def send(method, row, receipt_id):
            seq = row["seq"]
            attempts[seq] = attempts.get(seq, 0) + 1
            if attempts[seq] == 1:
                first.add(seq)
                return HttpReply(429, retry_after="1")
            self.assertEqual(len(first), 500)
            return reply(seq)

        sender, _ = self.make(send, count=500, concurrency=3, clock=Clock())
        result = asyncio.run(sender.run())
        self.assertTrue(result["all_responses_accepted"])
        self.assertEqual(result["counts"]["first_rejected"], 500)
        self.assertEqual(result["counts"]["post_requests"], 1000)
        self.assertEqual(result["scheduler"]["request_tasks_peak"], 3)
        self.assertLessEqual(result["scheduler"]["deferred_items_peak"], 500)

    def test_many_known_receipts_wait_as_state_until_all_first_requests_started(self):
        first = set()

        async def send(method, row, receipt_id):
            if method == "POST":
                first.add(row["seq"])
                return HttpReply(202, b'{"receipt_id":9}')
            self.assertEqual(len(first), 250)
            self.assertEqual(receipt_id, 9)
            return HttpReply(200, reply(9).body)

        sender, _ = self.make(send, count=250, concurrency=3)
        result = asyncio.run(sender.run())
        self.assertTrue(result["all_responses_accepted"])
        self.assertEqual(result["counts"]["query_requests"], 250)
        self.assertEqual(result["counts"]["first_unknown"], 250)
        self.assertEqual(result["scheduler"]["request_tasks_peak"], 3)

    def test_cancel_drains_only_bounded_active_requests_and_releases_their_slots(self):
        async def run():
            started = asyncio.Event()

            async def send(*args):
                started.set()
                await asyncio.Event().wait()

            sender, _ = self.make(send, count=1000, concurrency=3)
            task = asyncio.create_task(sender.run())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(sender.active, 0)
            self.assertEqual(sender.slots._value, 3)
            self.assertLessEqual(sender.request_tasks_peak, 3)
            self.assertEqual(sender.counts["post_requests"], 3)

        asyncio.run(run())

    def test_evidence_budget_failure_stops_and_cleans_up_other_requests(self):
        async def send(*args):
            await asyncio.sleep(0)
            return reply()

        class FullSink:
            def append(self, value):
                raise FixtureError("evidence_limit_reached")

        sender, _ = self.make(send, count=500, concurrency=3)
        sender.sink = FullSink()
        with self.assertRaisesRegex(FixtureError, "evidence_limit_reached"):
            asyncio.run(sender.run())
        self.assertEqual(sender.active, 0)
        self.assertEqual(sender.slots._value, 3)
        self.assertEqual(sender.counts["post_requests"], 3)


class HttpFollowupPacingTests(unittest.TestCase):
    make = HttpTests.make

    def test_first_posts_bypass_followup_pacing_and_keep_the_original_cutoff(self):
        clock = Clock()
        starts = []

        async def send(method, row, receipt_id):
            starts.append(clock.value)
            return reply(row["seq"])

        sender, _ = self.make(send, count=20, clock=clock, concurrency=3)
        sender.followup_next_start = 1000
        result = asyncio.run(sender.run())
        self.assertEqual(starts, [0] * 20)
        self.assertTrue(result["burst_window_passed"])
        self.assertEqual(sender.first_deadline, 5)
        self.assertEqual(sender.deadline, 60)

    def test_actual_followup_starts_are_spaced_and_never_catch_up_after_a_stall(self):
        clock = Clock()
        starts = []

        async def send(method, row, receipt_id):
            starts.append(clock.value)
            if len(starts) == 1:
                clock.value = 10  # No tokens may accumulate during this stall.
            return reply(row["seq"])

        sender, _ = self.make(send, count=625, clock=clock, concurrency=125)
        sender.first_round_done.set()

        async def run():
            await asyncio.gather(
                *(sender._request("POST", row) for row in sender.inputs)
            )

        asyncio.run(run())
        self.assertEqual(starts[:2], [0, 10])
        for before, after in pairwise(starts):
            self.assertGreaterEqual(
                after - before, 1 / HTTP_FOLLOWUP_STARTS_PER_SECOND - 1e-10
            )
        # In every half-open one-second sliding window, at most 500 starts.
        for index, start in enumerate(starts):
            self.assertLessEqual(
                sum(start <= other < start + 1 - 1e-10 for other in starts[index:]),
                HTTP_FOLLOWUP_STARTS_PER_SECOND,
            )
        self.assertEqual(sender.slots._value, 125)

    def test_pacing_wait_owns_no_http_slot_and_cancel_releases_the_start_lock(self):
        async def run():
            sleeping = asyncio.Event()

            async def pause(seconds):
                sleeping.set()
                await asyncio.Event().wait()

            async def send(*args):
                raise AssertionError("waiting request cannot reach transport")

            sender, _ = self.make(send, clock=Clock(), concurrency=3)
            sender.first_round_done.set()
            sender.followup_next_start = 1
            sender.sleep = pause
            task = asyncio.create_task(sender._request("POST", sender.inputs[0]))
            await sleeping.wait()
            self.assertEqual(sender.slots._value, 3)
            self.assertEqual(sender.active, 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(sender.followup_start_lock.locked())
            self.assertEqual(sender.slots._value, 3)

        asyncio.run(run())

    def test_retry_after_is_the_earliest_start_and_eligible_ids_remain_fair(self):
        clock = Clock()
        starts, counts = {}, {}

        async def send(method, row, receipt_id):
            seq = row["seq"]
            counts[seq] = counts.get(seq, 0) + 1
            starts.setdefault(seq, []).append(clock.value)
            if counts[seq] == 1:
                return HttpReply(429, retry_after="30" if seq == 1 else "1")
            return reply(seq)

        sender, _ = self.make(send, count=20, clock=clock, concurrency=3)
        result = asyncio.run(sender.run())
        self.assertTrue(result["all_responses_accepted"])
        self.assertGreaterEqual(starts[1][1], 30)
        self.assertTrue(all(1 <= starts[seq][1] < 2 for seq in range(2, 21)))
        self.assertEqual(counts, dict.fromkeys(range(1, 21), 2))
        self.assertEqual(sender.start, 0)

    def test_legal_deadline_tail_starts_but_no_followup_starts_at_or_after_sixty(self):
        clock = Clock()
        starts = []

        async def send(*args):
            starts.append(clock.value)
            return reply()

        sender, _ = self.make(send, clock=clock, concurrency=1)
        sender.first_round_done.set()
        clock.value = 59.997
        sender.followup_next_start = 59.998

        async def run():
            await sender._request("POST", sender.inputs[0])
            with self.assertRaises(TimeoutError):
                await sender._request("POST", sender.inputs[0])

        asyncio.run(run())
        self.assertEqual(starts, [59.998])
        self.assertEqual(sender.counts["post_requests"], 1)
        self.assertEqual(sender.deadline, 60)
        self.assertEqual(sender.slots._value, 1)

    def test_request_timeout_uses_remaining_window_and_cleans_up_in_this_task(self):
        async def run():
            clock = Clock()

            async def send(*args):
                await asyncio.Event().wait()

            sender, _ = self.make(send, clock=clock, concurrency=1)
            sender.first_round_done.set()
            clock.value = 59.99
            with self.assertRaises(TimeoutError):
                await sender._request("POST", sender.inputs[0])
            self.assertEqual(sender.counts["post_requests"], 1)
            self.assertEqual(sender.active, 0)
            self.assertEqual(sender.slots._value, 1)
            self.assertFalse(sender.followup_start_lock.locked())
            self.assertEqual(sender.deadline, 60)

        asyncio.run(run())

    def test_many_unknowns_retain_original_id_body_and_do_not_invent_queries(self):
        clock = Clock()
        seen = {}

        async def send(method, row, receipt_id):
            self.assertEqual(method, "POST")
            self.assertIsNone(receipt_id)
            identity = (row["message_id"], row["raw_base64"])
            previous = seen.setdefault(row["seq"], [])
            previous.append(identity)
            if len(previous) == 1:
                raise TimeoutError("never serialize this endpoint or error")
            return reply(row["seq"])

        sender, sink = self.make(send, count=20, clock=clock, concurrency=3)
        result = asyncio.run(sender.run())
        self.assertTrue(result["all_responses_accepted"])
        self.assertTrue(all(rows[0] == rows[1] for rows in seen.values()))
        self.assertEqual(result["counts"]["query_requests"], 0)
        self.assertEqual(result["counts"]["post_requests"], 40)
        self.assertNotIn("never serialize", json.dumps(sink.rows))

    def test_known_receipt_queries_share_the_same_followup_start_limit(self):
        clock = Clock()
        queried = []

        async def send(method, row, receipt_id):
            if method == "POST":
                return HttpReply(202, b'{"receipt_id":9}')
            self.assertEqual(receipt_id, 9)
            queried.append(clock.value)
            return HttpReply(200, reply(9).body)

        sender, _ = self.make(send, count=20, clock=clock, concurrency=3)
        result = asyncio.run(sender.run())
        self.assertTrue(result["all_responses_accepted"])
        self.assertEqual(len(queried), 20)
        self.assertTrue(all(b - a >= 0.002 - 1e-10 for a, b in pairwise(queried)))


class HttpTransportTests(unittest.TestCase):
    def client(self, chunks=(b"{}",), *, length=None):
        calls = []

        class Response:
            status = 202
            content_length = length

            def __init__(self):
                self.content = self
                self.headers = {"Retry-After": "2"}

            def iter_chunked(self, size):
                assert size == 16384

                async def iterate():
                    for chunk in chunks:
                        yield chunk

                return iterate()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Client:
            closed = False

            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return Response()

            async def close(self):
                self.closed = True

        return Client(), calls

    def transport(self, client):
        cfg = config(Path("/tmp/not-used"))
        cfg.update(token="synthetic-token", http_concurrency=125)
        transport = HttpTransport(cfg)
        transport.client = client
        return transport

    def test_bytes_identity_pooling_and_redirect_policy_use_the_public_client(self):
        client, calls = self.client()
        transport = self.transport(client)
        row = input_record("test-run", 1, kind="warmup")
        result = asyncio.run(transport.send("POST", row, None))
        self.assertEqual(result, HttpReply(202, b"{}", "2"))
        method, url, options = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, transport.url)
        self.assertEqual(options["data"], base64.b64decode(row["raw_base64"]))
        self.assertEqual(options["headers"]["Idempotency-Key"], row["message_id"])
        self.assertEqual(options["headers"]["Accept-Encoding"], "identity")
        self.assertIs(options["allow_redirects"], False)
        self.assertEqual(options["middlewares"], ())

    def test_known_length_and_streamed_responses_remain_within_16k(self):
        row = input_record("test-run", 1, kind="warmup")
        for chunks, length in (((b"unused",), 16385), ((b"x" * 16384, b"y"), None)):
            client, _ = self.client(chunks, length=length)
            with self.assertRaisesRegex(FixtureError, "http_response_too_large"):
                asyncio.run(self.transport(client).send("POST", row, None))
        client, _ = self.client((b"x" * 16384,), length=16384)
        self.assertEqual(
            len(asyncio.run(self.transport(client).send("POST", row, None)).body), 16384
        )

    def test_get_middleware_blocks_a_library_retry_before_a_second_dispatch(self):
        async def run():
            client, calls = self.client()
            transport = self.transport(client)
            await transport.send("GET", {}, 9)
            method, url, options = calls[0]
            self.assertEqual((method, url), ("GET", transport.url + "/receipts/9"))
            (middleware,) = options["middlewares"]
            dispatched = []

            async def handler(request):
                dispatched.append(request)
                return "one response"

            self.assertEqual(await middleware("one", handler), "one response")
            with self.assertRaisesRegex(FixtureError, "http_query_connection_lost"):
                await middleware("two", handler)
            self.assertEqual(dispatched, ["one"])

        asyncio.run(run())

    def test_pinned_session_config_is_bounded_and_closed(self):
        client, _ = self.client()
        recorded = {}

        class Connector:
            def __init__(self, **kwargs):
                recorded["connector"] = kwargs

        def session(**kwargs):
            recorded["session"] = kwargs
            return client

        fake = SimpleNamespace(
            __version__="3.14.5",
            TCPConnector=Connector,
            ClientTimeout=lambda **kwargs: kwargs,
            DummyCookieJar=lambda: object(),
            ClientSession=session,
        )

        async def run():
            async with self.transport(None) as send:
                self.assertTrue(callable(send))
            self.assertTrue(client.closed)

        with patch.dict(sys.modules, {"aiohttp": fake}):
            asyncio.run(run())
        self.assertEqual(recorded["connector"]["limit"], 125)
        self.assertEqual(recorded["connector"]["limit_per_host"], 125)
        self.assertFalse(recorded["session"]["auto_decompress"])
        self.assertFalse(recorded["session"]["trust_env"])
        self.assertEqual(recorded["session"]["timeout"]["total"], 5)
        self.assertEqual(recorded["session"]["timeout"]["ceil_threshold"], 60)
        self.assertEqual(recorded["session"]["read_bufsize"], 16384)


class OracleTests(unittest.TestCase):
    def test_inserted_outcome_must_belong_to_first_attempt(self):
        inputs = [input_record("test-run", 1)]
        item = inputs[0]
        row = {
            "run_id": item["run_id"],
            "message_id": item["message_id"],
            "seq": 1,
            "payload_sha256": item["payload_sha256"],
            "normalized_value": 3,
            "writes": 1,
            "attempts": 2,
        }
        attempts = [
            {**row, "ordinal": 1, "outcome": "duplicate"},
            {**row, "ordinal": 2, "outcome": "inserted"},
        ]
        self.assertFalse(verify_rows(inputs, [row], attempts)["target_oracle_passed"])
        attempts[0]["outcome"], attempts[1]["outcome"] = "inserted", "duplicate"
        self.assertTrue(verify_rows(inputs, [row], attempts)["target_oracle_passed"])

    def test_canonical_target_hash_and_attempt_chain_are_independent(self):
        inputs = [input_record("test-run", 1)]
        item = inputs[0]
        row = {
            "run_id": item["run_id"],
            "message_id": item["message_id"],
            "seq": 1,
            "payload_sha256": item["payload_sha256"],
            "normalized_value": 3,
            "writes": 1,
            "attempts": 1,
        }
        attempt = {**row, "ordinal": 1, "outcome": "inserted"}
        self.assertTrue(
            verify_rows(inputs, iter([row]), iter([attempt]))["target_oracle_passed"]
        )
        for field, value in [
            ("payload_sha256", item["raw_sha256"]),
            ("writes", 2),
            ("normalized_value", 4),
            ("attempts", 2),
        ]:
            self.assertFalse(
                verify_rows(inputs, [{**row, field: value}], [attempt])[
                    "target_oracle_passed"
                ]
            )
        self.assertFalse(verify_rows(inputs, [], [attempt])["target_oracle_passed"])
        self.assertFalse(
            verify_rows(inputs, [row, row], [attempt])["target_oracle_passed"]
        )
        self.assertFalse(
            verify_rows(inputs, [row], [{**attempt, "ordinal": 2}])[
                "target_oracle_passed"
            ]
        )


class PublisherTests(unittest.TestCase):
    def make(self):
        inputs = [input_record("test-run", i) for i in range(1, 201)]
        instance = Publisher({"queue": "fixture"}, inputs, MemorySink())
        instance.connection, instance.channel = FakeConnection(), FakeChannel()
        instance.pika = SimpleNamespace(
            BasicProperties=lambda **kwargs: SimpleNamespace(**kwargs)
        )
        return instance

    def test_confirm_window_does_not_confuse_submission_with_confirmation(self):
        publisher = self.make()
        publisher.pump()
        self.assertEqual(len(publisher.pending), 32)
        self.assertFalse(publisher.acked)
        while not publisher.complete:
            publisher.on_confirmation(
                confirmation(max(publisher.pending), multiple=True)
            )
        self.assertEqual(len(publisher.acked), 200)
        self.assertEqual(publisher.window_peak, 32)
        self.assertTrue(
            all(len(row["body"]) == 2048 for row in publisher.channel.messages)
        )

    def test_nack_retains_unconfirmed_responsibility(self):
        publisher = self.make()
        publisher.pump()
        publisher.on_confirmation(confirmation(1, ack=False))
        self.assertEqual(publisher.failure, "broker_nack")
        self.assertEqual(len(publisher.pending), 31)
        self.assertFalse(publisher.complete)


if __name__ == "__main__":
    unittest.main()
