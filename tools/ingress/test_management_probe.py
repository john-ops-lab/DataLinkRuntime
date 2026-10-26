"""Finite, local probe tests; no live services or capacity claims."""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.ingress import management_probe as probe

DOMAIN = {"clock": "CLOCK_MONOTONIC", "boot_id": "00000000-0000-0000-0000-000000000000", "time_namespace": "time:[1]", "children_time_namespace": "time:[1]"}


def config(root):
    return {"schema": probe.SCHEMA, "run_id": "capacity-proof", "api_base": "https://private.example", "admin_token": "secret-not-for-evidence", "subscription_id": 7, "count": 10000, "output_path": str(root), "urls": {"adapters": "/api/adapters", "events": "/api/event-subscriptions/7/events?limit=20", "backlog": "/api/event-subscriptions/7"}, "stage": "http", "producer_log_path": str(root / "capacity-proof-http.private.jsonl"), "producer_result_path": str(root / "capacity-proof-http.private.json"), "baseline_samples": 20, "post_drain_seconds": 600, "deadline_seconds": 1800, "request_timeout_seconds": 5, "response_max_bytes": probe.MAX_RESPONSE_BYTES, "log_max_bytes": probe.MAX_LOG_BYTES}


def backlog(accepted=0, succeeded=0):
    return {"id": 7, "event_counts": {"accepted": accepted, "pending": accepted - succeeded, "succeeded": succeeded, "coalesced": 0, "replayed": 0}, "quota": {"pending_count": accepted - succeeded, "history_bytes": accepted * 2048}, "limits": {"pending_count": 10000}, "oldest_pending_at": None, "config": {"credential_id": 88, "server": "secret-host"}, "name": "secret-adapter"}


def body_for(endpoint, counts=None):
    return probe.encoded({"adapters": [{"name": "never-log-me"}], "events": {"items": [{"envelope": {"secret": "private-payload"}}], "total": 1}, "backlog": counts or backlog()}[endpoint])


class MemorySink:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(row)


class FakeClock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    async def sleep(self, seconds):
        # Flush immediate fake transports before jumping to the next 1 Hz tick.
        for _ in range(8):
            await asyncio.sleep(0)
        self.value += int(seconds * probe.NS)


def evidence_rows(conf):
    rows = [{"kind": "start", "schema": probe.SCHEMA, "run_id": conf["run_id"], "config_fingerprint": probe.config_fingerprint(conf), "clock_domain": DOMAIN, "monotonic_ns": 0}]
    for slot in range(624):
        for endpoint in probe.ENDPOINTS:
            counts = backlog(0 if slot <= 20 else 5000 if slot == 21 else conf["count"], conf["count"] if slot >= 23 else 0)
            rows.append({"kind": "sample", "endpoint": endpoint, "slot": slot, "scheduled_ns": slot * probe.NS, "request_begin_ns": slot * probe.NS, "response_end_ns": slot * probe.NS + 10_000_000, "elapsed_ms": 10.0, "status": 200, "timeout": False, "error_code": None, "snapshot": probe.safe_response(endpoint, body_for(endpoint, counts), 7)})
        if slot == 19:
            rows.append({"kind": "ready", "monotonic_ns": 19 * probe.NS + 10_000_000, "config_fingerprint": probe.config_fingerprint(conf), "clock_domain": DOMAIN, "accepted": 0})
    rows.append({"kind": "end", "monotonic_ns": 624 * probe.NS, "reason": "post_drain_complete", "slots": 624, "concurrency_peak": 3})
    return rows


class ConfigurationTests(unittest.TestCase):
    def test_closed_formal_routes_and_hard_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.json"
            original = config(root)
            source.write_bytes(probe.encoded(original))
            self.assertEqual(probe.load_config(source)["count"], 10000)
            for key, value in [("count", 200), ("request_timeout_seconds", 6), ("deadline_seconds", 1801), ("post_drain_seconds", 599), ("baseline_samples", 19), ("response_max_bytes", probe.MAX_RESPONSE_BYTES + 1), ("log_max_bytes", probe.MAX_LOG_BYTES + 1)]:
                source.write_bytes(probe.encoded({**original, key: value}))
                with self.subTest(key=key), self.assertRaises(probe.ProbeError):
                    probe.load_config(source)
            source.write_bytes(probe.encoded({**original, "urls": {**original["urls"], "events": "/api/event-subscriptions/7/reset"}}))
            with self.assertRaisesRegex(probe.ProbeError, "fixed_get"):
                probe.load_config(source)

    def test_evidence_is_exclusive_private_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence"
            sink = probe.PrivateLog(path, 30)
            sink.append({"ok": True})
            with self.assertRaisesRegex(probe.ProbeError, "budget"):
                sink.append({"overflow": "x" * 30})
            sink.close()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                probe.PrivateLog(path, 30)
        with self.assertRaisesRegex(probe.ProbeError, "inside_repository"):
            probe.private_path(Path(probe.__file__))

    def test_backlog_counts_do_not_double_count_aliases_or_retain_bodies(self):
        value = backlog(3, 2)
        value["event_counts"].update(superseded=1, pending=0, coalesced=1, replayed=20)
        safe = probe.safe_response("backlog", probe.encoded(value), 7)
        self.assertEqual(safe["counts"]["accepted"], 3)
        self.assertNotIn("secret", probe.encoded(safe).decode())
        value["event_counts"]["accepted"] = 24
        with self.assertRaisesRegex(probe.ProbeError, "alias_mismatch"):
            probe.safe_response("backlog", probe.encoded(value), 7)
        value["event_counts"]["accepted"] = 3
        value["event_counts"]["new_unknown_state"] = 0
        with self.assertRaisesRegex(probe.ProbeError, "unknown_receipt_counter"):
            probe.safe_response("backlog", probe.encoded(value), 7)


class SamplerTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_three_concurrent_reads_and_ready_only_after_twenty_empty_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            conf, clock, sink = config(Path(directory)), FakeClock(), MemorySink()
            ready = []
            active = peak = 0

            async def read(endpoint):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1
                return 200, body_for(endpoint, backlog(10000, 10000) if clock.now() >= 22 * probe.NS else backlog())

            sampler = probe.Sampler(conf, sink, read, clock=clock.now, sleep=clock.sleep, domain=DOMAIN, ready_sink=ready.append)
            result = await sampler.run()
            self.assertEqual(peak, 3)
            self.assertEqual(result["reason"], "post_drain_complete")
            self.assertEqual(result["errors"], 0)
            self.assertEqual(len(ready), 1)
            self.assertTrue(all(number >= 20 for number in ready[0]["baseline_samples"].values()))
            self.assertEqual(ready[0]["accepted"], 0)
            samples = [row for row in sink.rows if row["kind"] == "sample"]
            self.assertEqual(len(samples), result["slots"] * 3)
            self.assertTrue(all(row["request_begin_ns"] == row["slot"] * probe.NS for row in samples))
            self.assertNotIn("private-payload", probe.encoded(sink.rows).decode())
            self.assertNotIn("never-log-me", probe.encoded(sink.rows).decode())

    async def test_hanging_request_is_not_queued_or_retried_and_causes_explicit_missed_slots(self):
        conf, sink = config(Path("/tmp/probe-unit")), MemorySink()
        conf["deadline_seconds"] = 3
        clock = FakeClock()
        calls = []

        async def read(endpoint):
            calls.append(endpoint)
            await asyncio.Future()

        sampler = probe.Sampler(conf, sink, read, clock=clock.now, sleep=clock.sleep, domain=DOMAIN)
        result = await sampler.run()
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["concurrency_peak"], 3)
        self.assertEqual(sum(row["kind"] == "missed" for row in sink.rows), 6)
        self.assertTrue(all(row["error_code"] == "request_cancelled_at_probe_deadline" for row in sink.rows if row["kind"] == "sample"))
        self.assertFalse(result["ready"])

    async def test_error_status_timeout_and_driver_text_are_not_hidden_or_leaked(self):
        conf, sink = config(Path("/tmp/probe-unit")), MemorySink()
        conf["request_timeout_seconds"] = 0.01

        async def read(endpoint):
            if endpoint == "adapters":
                return 503, b"secret-response-body"
            if endpoint == "events":
                raise ValueError("https://private.example secret-token")
            await asyncio.sleep(10)

        sampler = probe.Sampler(conf, sink, read, domain=DOMAIN)
        await asyncio.gather(*(sampler.one(endpoint, 0, sampler.start_ns) for endpoint in probe.ENDPOINTS))
        self.assertEqual(sampler.errors, 3)
        self.assertEqual({row["error_code"] for row in sink.rows}, {"http_status_not_200", "request_transport_failed", "request_timeout"})
        self.assertNotIn("secret", probe.encoded(sink.rows).decode())
        self.assertTrue(any(row["timeout"] for row in sink.rows))

    async def test_streamed_body_budget_is_enforced_before_json_or_retention(self):
        class Response:
            status_code, headers = 200, {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def aiter_raw(self, chunk_size):
                self.asserted_chunk_size = chunk_size
                yield b"x" * 1025

        response = Response()
        client = SimpleNamespace(stream=lambda *args, **kwargs: response)
        conf = config(Path("/tmp/probe-unit"))
        conf["response_max_bytes"] = 1024
        with self.assertRaisesRegex(probe.ProbeError, "body_budget"):
            await probe.HttpReader(client, conf)("adapters")
        self.assertEqual(response.asserted_chunk_size, 65536)
        response.headers = {"Content-Encoding": "gzip"}
        with self.assertRaisesRegex(probe.ProbeError, "encoded_response"):
            await probe.HttpReader(client, conf)("adapters")


class AnalysisTests(unittest.TestCase):
    def test_valid_complete_evidence_uses_conservative_causal_intervals(self):
        conf = config(Path("/tmp/probe-unit"))
        result = probe.evaluate(conf, evidence_rows(conf), {"sender_begin_ns": 20 * probe.NS, "clock_domain_proven": True})
        self.assertTrue(result["management_and_timing_passed"], result["failure_codes"])
        self.assertEqual(result["timing"]["actual_accept_interval_ns"], {"lower": 21 * probe.NS, "upper": 22 * probe.NS + 10_000_000})
        self.assertAlmostEqual(result["timing"]["acceptance_seconds_upper"], 2.01)
        self.assertAlmostEqual(result["timing"]["completion_after_accept_seconds_upper"], 2.01)
        self.assertEqual(result["latency"]["adapters"]["baseline"]["samples"], 20)
        self.assertEqual(result["latency"]["adapters"]["limit_ms"], 500)
        self.assertFalse(result["capacity_acceptance_declared"])

    def test_failures_missing_phase_and_unknown_domain_never_become_zero_latency(self):
        conf = config(Path("/tmp/probe-unit"))
        original = evidence_rows(conf)
        for kind in ("missing", "timeout", "status", "no_producer", "short_post", "count_alias", "regressed_terminal"):
            rows = copy.deepcopy(original)
            producer = {"sender_begin_ns": 20 * probe.NS, "clock_domain_proven": True}
            expected = None
            if kind == "missing":
                del rows[4]
                expected = "sampling_coverage_incomplete"
            elif kind == "timeout":
                rows[4].update(status=0, timeout=True, error_code="request_timeout")
                expected = "management_request_failed"
            elif kind == "status":
                rows[4].update(status=503, error_code="http_status_not_200")
                expected = "management_request_failed"
            elif kind == "no_producer":
                producer = None
                expected = "producer_clock_evidence_missing"
            elif kind == "short_post":
                rows = [row for row in rows if row.get("slot", 0) < 100]
                expected = "post_drain_600s_not_observed"
            elif kind == "count_alias":
                for row in rows:
                    if row.get("endpoint") == "backlog":
                        row["snapshot"]["counts"]["accepted"] += 1
                expected = "subscription_not_fresh_or_count_exceeded"
            else:
                last = next(row for row in reversed(rows) if row.get("endpoint") == "backlog")
                last["snapshot"]["counts"].update(succeeded=9999, failed=1)
                expected = "post_drain_business_state_regressed"
            with self.subTest(kind=kind):
                result = probe.evaluate(conf, rows, producer)
                self.assertFalse(result["management_and_timing_passed"])
                self.assertIn(expected, result["failure_codes"])

    def test_no_prior_incomplete_or_sender_before_ready_is_unknown_and_not_reset(self):
        conf = config(Path("/tmp/probe-unit"))
        rows = evidence_rows(conf)
        for row in rows:
            if row.get("endpoint") == "backlog":
                row["snapshot"] = probe.safe_response("backlog", probe.encoded(backlog(10000, 10000)), 7)
        result = probe.evaluate(conf, rows, {"sender_begin_ns": 20 * probe.NS, "clock_domain_proven": True})
        self.assertFalse(result["timing"]["known"])
        self.assertIn("accept_or_completion_interval_unknown", result["failure_codes"])
        result = probe.evaluate(conf, evidence_rows(conf), {"sender_begin_ns": 18 * probe.NS, "clock_domain_proven": True})
        self.assertIn("sender_time_order_invalid", result["failure_codes"])
        self.assertFalse(result["management_and_timing_passed"])

    def test_nearest_rank_p95_and_relative_idle_limit_are_per_phase(self):
        self.assertEqual(probe.percentile95(list(range(1, 21))), 19)
        conf = config(Path("/tmp/probe-unit"))
        rows = evidence_rows(conf)
        for row in rows:
            if row.get("kind") == "sample" and row["slot"] <= 19:
                row["elapsed_ms"] = 300
                row["response_end_ns"] = row["request_begin_ns"] + 300_000_000
            elif row.get("kind") == "sample" and 20 <= row["slot"] <= 23:
                row["elapsed_ms"] = 650
                row["response_end_ns"] = row["request_begin_ns"] + 650_000_000
            elif row.get("kind") == "ready":
                row["monotonic_ns"] = 19 * probe.NS + 300_000_000
        result = probe.evaluate(conf, rows, {"sender_begin_ns": 20 * probe.NS, "clock_domain_proven": True})
        self.assertEqual(result["latency"]["adapters"]["limit_ms"], 600)
        self.assertIn("management_p95_exceeded", result["failure_codes"])
        # Six hundred quiet post-drain points must not hide load-phase p95.
        self.assertEqual(result["latency"]["adapters"]["load_and_drain"]["p95_ms"], 650)

    def test_late_acceptance_uses_original_sender_boundary_and_cannot_reset_deadline(self):
        conf = config(Path("/tmp/probe-unit"))
        rows = evidence_rows(conf)
        for row in rows:
            if row.get("endpoint") == "backlog":
                slot = row["slot"]
                if 21 <= slot <= 80:
                    row["snapshot"] = probe.safe_response("backlog", probe.encoded(backlog(5000, 0)), 7)
                elif slot == 81:
                    row["snapshot"] = probe.safe_response("backlog", probe.encoded(backlog(10000, 0)), 7)
        result = probe.evaluate(conf, rows, {"sender_begin_ns": 20 * probe.NS, "clock_domain_proven": True})
        self.assertEqual(result["timing"]["sender_begin_ns"], 20 * probe.NS)
        self.assertAlmostEqual(result["timing"]["acceptance_seconds_upper"], 61.01)
        self.assertIn("causal_time_upper_bound_exceeded", result["failure_codes"])
        self.assertFalse(result["management_and_timing_passed"])

    def test_full_ten_thousand_producer_log_needs_same_domain_and_unchanged_files(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            names = probe.paths(conf)
            ready = {"monotonic_ns": 19 * probe.NS}
            witness = {"run_id": conf["run_id"], "stage": "http", "clock_domain": DOMAIN, "workload_sha256": probe.WORKLOAD_SHA256, "config_fingerprint": probe.config_fingerprint(conf)}
            probe.write_private(names["producer_start"], {**witness, "monotonic_ns": 20 * probe.NS})
            with Path(conf["producer_log_path"]).open("wb") as output:
                output.writelines(probe.encoded({"kind": "http_attempt", "seq": seq, "attempt": 1, "started_ns": 20 * probe.NS + seq * 1000}) + b"\n" for seq in range(1, 10001))
            probe.write_private(Path(conf["producer_result_path"]), {"run_id": conf["run_id"], "stage": "http", "count": 10000, "concurrency_peak": 125, "burst_window_passed": True, "all_responses_accepted": True, "retry_after_contract_passed": True, "local_response_deadline_passed": True})
            probe.write_private(names["producer_end"], {**witness, "monotonic_ns": 30 * probe.NS, "returncode": 0, "log_sha256": probe.file_digest(Path(conf["producer_log_path"])), "result_sha256": probe.file_digest(Path(conf["producer_result_path"]))})
            result = probe.producer_evidence(conf, ready, DOMAIN)
            self.assertEqual(result["sender_begin_ns"], 20 * probe.NS + 1000)
            self.assertEqual(result["first_request_count"], 10000)
            with self.assertRaisesRegex(probe.ProbeError, "clock_provenance"):
                probe.producer_evidence(conf, ready, {**DOMAIN, "time_namespace": "time:[2]"})
            with Path(conf["producer_log_path"]).open("ab") as output:
                output.write(b"{}\n")
            with self.assertRaisesRegex(probe.ProbeError, "unchanged"):
                probe.producer_evidence(conf, ready, DOMAIN)

    def test_producer_wrapper_refuses_128_plus_management_without_running_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root, conf = Path(directory), config(Path(directory))
            probe.write_private(probe.paths(conf)["ready"], {"clock_domain": DOMAIN, "config_fingerprint": probe.config_fingerprint(conf)})
            path = root / "producer-config.private.json"
            probe.write_private(path, {"run_id": conf["run_id"], "kind": "capacity", "count": 10000, "http_concurrency": 128})
            with patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe.subprocess, "run") as run:
                with self.assertRaisesRegex(probe.ProbeError, "exceeds_128"):
                    probe.run_producer(conf, path, "http")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
