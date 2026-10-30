"""Finite, local probe tests; no live services or capacity claims."""

import asyncio
import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.ingress import management_probe as probe

DOMAIN = {"clock": "CLOCK_MONOTONIC", "boot_id": "00000000-0000-0000-0000-000000000000", "time_namespace": "time:[1]", "children_time_namespace": "time:[1]"}
PROCESS = {"pid": 123, "start_ticks": 7, "pid_namespace": "pid:[1]", "time_namespace": "time:[1]"}


def source_binding():
    return {"id": 7, "adapter_id": 12, "activation_id": 4, "source_type": "http", "config_version": 1, "source_epoch": "private-epoch", "ingress_path": "/api/ingress/private-route"}


def config(root):
    return {"schema": probe.SCHEMA, "run_id": "capacity-proof", "api_base": "https://private.example", "admin_token": "secret-not-for-evidence", "subscription_id": 7, "source_binding": source_binding(), "count": 10000, "output_path": str(root), "urls": {"adapters": "/api/adapters", "events": "/api/event-subscriptions/7/events?limit=20", "backlog": "/api/event-subscriptions/7"}, "stage": "http", "producer_config_path": str(root / "producer-config.private.json"), "producer_log_path": str(root / "capacity-proof-http.private.jsonl"), "producer_result_path": str(root / "capacity-proof-http.private.json"), "baseline_samples": 20, "post_drain_seconds": 600, "deadline_seconds": 1800, "request_timeout_seconds": 5, "response_max_bytes": probe.MAX_RESPONSE_BYTES, "log_max_bytes": probe.MAX_LOG_BYTES}


def sampler_fixture(conf, ready_ns=19 * probe.NS):
    owner = {"schema": probe.SCHEMA, "run_id": conf["run_id"], **probe.binding_witness(conf), "clock_domain": DOMAIN, **PROCESS, "sampler_start_ns": 0, "deadline_ns": conf["deadline_seconds"] * probe.NS}
    ready = {"schema": probe.SCHEMA, "kind": "ready", "run_id": conf["run_id"], **probe.binding_witness(conf), "clock_domain": DOMAIN, "monotonic_ns": ready_ns, "accepted": 0}
    start = {"kind": "start", "schema": probe.SCHEMA, "run_id": conf["run_id"], **probe.binding_witness(conf), "clock_domain": DOMAIN, "monotonic_ns": 0, "deadline_seconds": conf["deadline_seconds"]}
    names = probe.paths(conf)
    probe.write_private(names["pid"], owner)
    probe.write_private(names["ready"], ready)
    names["log"].write_bytes(probe.encoded(start) + b"\n" + probe.encoded(ready) + b"\n")
    return ready


def producer_fixture(conf):
    path = Path(conf["producer_config_path"])
    probe.write_private(path, {"run_id": conf["run_id"], "kind": "capacity", "count": 10000, "output_path": conf["output_path"], "http_concurrency": 125, "ingress_url": conf["api_base"] + conf["source_binding"]["ingress_path"]})
    return path


def producer_artifacts(conf, returncode=0):
    # Only the mocked child creates these evidence files; no real sender runs.
    for key in ("producer_log_path", "producer_result_path"):
        Path(conf[key]).write_bytes(b"{}\n")
    return SimpleNamespace(returncode=returncode)


def rabbit_config(root):
    value = config(root)
    value.update(stage="publish", publisher_origin="amqp://publisher-private:5672")
    value["source_binding"] = {**source_binding(), "source_type": "rabbitmq", "config": {"cluster_id": "private-cluster", "server": "amqp://consumer-private:5672", "queue": "private-queue", "vhost": "private/vhost+"}}
    value["source_binding"].pop("ingress_path")
    for key, suffix in (("producer_log_path", "jsonl"), ("producer_result_path", "json")):
        value[key] = str(root / f"capacity-proof-publish.private.{suffix}")
    return value


def synthetic_broker_witness(conf, root):
    value = {"schema": "dlr.private.rabbit-target-identity/v1", "run_id": conf["run_id"], "candidate": "1" * 40, "case_context_sha256": "2" * 64, "identity": probe._broker_identity_projection(conf), "containers": {role: {"cid": char * 64, "pid": 3, "started_at": "2026-10-27T00:00:00Z", "restarts": 0, "image": "sha256:" + "5" * 64, **dict.fromkeys(("config_sha256", "hostconfig_sha256", "mounts_sha256", "network_sha256"), "4" * 64)} for role, char in (("control", "a"), ("generator", "b"), ("broker", "c"))}, "child_identities": {role: {"pid": 5, "start_ticks": "6", "boot_id_sha256": "7" * 64, "netns_sha256": "8" * 64} for role in ("control", "generator")}, "simultaneous_connection_pids_sha256": {"a" * 64: "b" * 64, "c" * 64: "d" * 64}, "both_connections_observed_same_owned_CID": True, "both_child_closed_and_reaped": True, "owned_tags_absent_after_close": True}
    path = root / "synthetic-broker-witness.json"
    probe.write_private(path, value)
    conf["broker_identity_evidence"] = {"path": str(path), "sha256": probe.file_digest(path), "candidate": value["candidate"], "case_context_sha256": value["case_context_sha256"]}
    return value


def backlog(accepted=0, succeeded=0):
    return {**source_binding(), "event_counts": {"accepted": accepted, "pending": accepted - succeeded, "succeeded": succeeded, "coalesced": 0, "replayed": 0}, "quota": {"pending_count": accepted - succeeded, "history_bytes": accepted * 2048}, "limits": {"pending_count": 10000}, "oldest_pending_at": None, "config": {"credential_id": 88, "server": "secret-host"}, "name": "secret-adapter"}


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
    rows = [{"kind": "start", "schema": probe.SCHEMA, "run_id": conf["run_id"], **probe.binding_witness(conf), "clock_domain": DOMAIN, "monotonic_ns": 0}]
    for slot in range(624):
        for endpoint in probe.ENDPOINTS:
            counts = backlog(0 if slot <= 20 else 5000 if slot == 21 else conf["count"], conf["count"] if slot >= 23 else 0)
            rows.append({"kind": "sample", "endpoint": endpoint, "slot": slot, "scheduled_ns": slot * probe.NS, "request_begin_ns": slot * probe.NS, "response_end_ns": slot * probe.NS + 10_000_000, "elapsed_ms": 10.0, "status": 200, "timeout": False, "error_code": None, "snapshot": probe.safe_response(endpoint, body_for(endpoint, counts), 7, conf["source_binding"])})
        if slot == 19:
            rows.append({"kind": "ready", "monotonic_ns": 19 * probe.NS + 10_000_000, **probe.binding_witness(conf), "clock_domain": DOMAIN, "accepted": 0})
    rows.append({"kind": "end", "monotonic_ns": 624 * probe.NS, "reason": "post_drain_complete", "slots": 624, "concurrency_peak": 3})
    return rows


class ConfigurationTests(unittest.TestCase):
    def test_private_log_constructor_closes_owned_fd_and_preserves_foreign_paths(self):
        for mode in ("owned", "replacement", "preexisting"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "log.private.jsonl"
                first, fds = OSError("private-fdopen-error"), []
                if mode == "preexisting":
                    path.write_bytes(b"preexisting")

                def fail_fdopen(fd, *_args, **_kwargs):
                    fds.append(fd)
                    if mode == "replacement":
                        path.unlink()
                        path.write_bytes(b"foreign-replacement")
                    raise first

                with patch.object(probe.os, "fdopen", side_effect=fail_fdopen) as fdopen:
                    with self.assertRaises(FileExistsError if mode == "preexisting" else OSError) as failure:
                        probe.PrivateLog(path, 1024)
                if mode == "preexisting":
                    fdopen.assert_not_called()
                    self.assertEqual(path.read_bytes(), b"preexisting")
                else:
                    self.assertIs(failure.exception, first)
                    with self.assertRaises(OSError):
                        probe.os.fstat(fds[0])
                    if mode == "replacement":
                        self.assertEqual(path.read_bytes(), b"foreign-replacement")
                    else:
                        self.assertFalse(path.exists())

    def test_private_log_constructor_cleanup_keeps_primary_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.private.jsonl"
            primary, calls = OSError("private-fdopen-error"), []
            close = probe.os.close

            def fail_fdopen(*_args, **_kwargs):
                calls.append("fdopen")
                raise primary

            def close_then_fail(fd):
                calls.append("close")
                close(fd)
                raise OSError("private-close-error")

            def unlink_fails(*_args, **_kwargs):
                calls.append("unlink")
                raise OSError("private-unlink-error")

            with patch.object(probe.os, "fdopen", side_effect=fail_fdopen), patch.object(probe.os, "close", side_effect=close_then_fail), patch.object(Path, "unlink", new=unlink_fails):
                with self.assertRaises(OSError) as failure:
                    probe.PrivateLog(path, 1024)
            self.assertIs(failure.exception, primary)
            self.assertEqual(calls, ["fdopen", "close", "unlink"])
            self.assertEqual(primary.__notes__, ["private log fd close failed: OSError", "private log unlink failed: OSError"])

    def test_sampler_close_failure_preserves_original_or_fails_closed(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        for original in (False, True):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                conf = config(Path(directory))
                primary, close_error, closed = OSError("private-sampler-error"), OSError("private-close-error"), []

                async def run():
                    if original:
                        raise primary
                    return {}

                def close():
                    closed.append(True)
                    raise close_error

                sampler = SimpleNamespace(start_ns=0, binding=probe.binding_witness(conf), run=run)
                httpx = SimpleNamespace(AsyncClient=lambda **_kwargs: Client(), AsyncHTTPTransport=lambda **_kwargs: None, Limits=lambda **_kwargs: None)
                with patch.dict(sys.modules, {"httpx": httpx}), patch.object(probe, "PrivateLog", return_value=SimpleNamespace(close=close)), patch.object(probe, "Sampler", return_value=sampler), patch.object(probe, "write_private"), patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe, "process_identity", return_value=PROCESS), patch.object(probe, "analyze") as analyze:
                    with self.assertRaises(OSError) as failure:
                        asyncio.run(probe.start_probe(conf))
                    analyze.assert_not_called()
                self.assertIs(failure.exception, primary if original else close_error)
                self.assertEqual(closed, [True])
                if original:
                    self.assertEqual(primary.__notes__, ["sampler log close failed: OSError"])

    def test_write_private_preserves_first_error_and_attempts_both_cleanups(self):
        for stage in ("append", "fsync", "link", "interrupt"):
            with self.subTest(stage=stage):
                calls = []
                primary = KeyboardInterrupt() if stage == "interrupt" else OSError("private-first-error")

                def action(name, error=None):
                    def run(*_args, **_kwargs):
                        calls.append(name)
                        if error is not None:
                            raise error
                    return run

                log = SimpleNamespace(handle=SimpleNamespace(fileno=lambda: 11), append=action("append", primary if stage in ("append", "interrupt") else None), close=action("close", OSError("private-close-error")))
                with patch.object(probe, "PrivateLog", return_value=log), patch.object(probe.os, "fsync", side_effect=action("fsync", primary if stage == "fsync" else None)), patch.object(probe.os, "link", side_effect=action("link", primary if stage == "link" else None)), patch.object(Path, "unlink", new=action("unlink", OSError("private-unlink-error"))):
                    with self.assertRaises(type(primary)) as failure:
                        probe.write_private(Path("/tmp/receipt-unit.private.json"), {"safe": True})
                self.assertIs(failure.exception, primary)
                expected = ["append"] + (["fsync"] if stage in ("fsync", "link") else []) + (["link"] if stage == "link" else []) + ["close", "unlink"]
                self.assertEqual(calls, expected)
                self.assertEqual(primary.__notes__, ["private receipt close failed: OSError", "private receipt unlink failed: OSError"])
                self.assertNotIn("private-close-error", str(primary.__notes__))

    def test_write_private_cleanup_failure_never_claims_success(self):
        for close_fails, unlink_fails in ((True, False), (False, True), (True, True)):
            with self.subTest(close_fails=close_fails, unlink_fails=unlink_fails):
                calls = []
                close_error, unlink_error = OSError("private-close-error"), OSError("private-unlink-error")

                def action(name, error=None):
                    def run(*_args, **_kwargs):
                        calls.append(name)
                        if error is not None:
                            raise error
                    return run

                log = SimpleNamespace(handle=SimpleNamespace(fileno=lambda: 11), append=action("append"), close=action("close", close_error if close_fails else None))
                with patch.object(probe, "PrivateLog", return_value=log), patch.object(probe.os, "fsync", side_effect=action("fsync")), patch.object(probe.os, "link", side_effect=action("link")), patch.object(Path, "unlink", new=action("unlink", unlink_error if unlink_fails else None)):
                    with self.assertRaises(OSError) as failure:
                        probe.write_private(Path("/tmp/receipt-unit.private.json"), {"safe": True})
                self.assertIs(failure.exception, close_error if close_fails else unlink_error)
                self.assertEqual(calls, ["append", "fsync", "link", "close", "unlink"])
                if close_fails and unlink_fails:
                    self.assertEqual(failure.exception.__notes__, ["private receipt unlink failed: OSError"])

    def test_api_origin_and_source_are_bound_without_logging_plaintext(self):
        conf = config(Path("/tmp/probe-unit"))
        original = probe.config_fingerprint(conf)
        self.assertEqual(original, probe.config_fingerprint({**conf, "api_base": "https://PRIVATE.example:443/"}))
        self.assertNotEqual(original, probe.config_fingerprint({**conf, "api_base": "https://other-private.example"}))
        self.assertNotEqual(original, probe.config_fingerprint({**conf, "source_binding": {**conf["source_binding"], "config_version": 2}}))
        self.assertNotIn("private", probe.encoded(probe.binding_witness(conf)).decode())

    def test_backlog_requires_the_actual_source_projection(self):
        for key, value in (("id", 8), ("adapter_id", 13), ("activation_id", 5), ("source_type", "rabbitmq"), ("config_version", 2), ("source_epoch", "other"), ("ingress_path", "/api/ingress/other")):
            with self.subTest(key=key), self.assertRaises(probe.ProbeError):
                probe.safe_response("backlog", probe.encoded({**backlog(), key: value}), 7, source_binding())
        missing = backlog()
        missing.pop("activation_id")
        with self.assertRaisesRegex(probe.ProbeError, "source_binding"):
            probe.safe_response("backlog", probe.encoded(missing), 7, source_binding())
        inactive = {**source_binding(), "activation_id": None}
        self.assertIn("source_binding_sha256", probe.safe_response("backlog", probe.encoded({**backlog(), **inactive}), 7, inactive))
        with tempfile.TemporaryDirectory() as directory:
            conf = rabbit_config(Path(directory))
            value = {**backlog(), **conf["source_binding"]}
            safe = probe.safe_response("backlog", probe.encoded(value), 7, conf["source_binding"])
            self.assertNotIn("private", probe.encoded(safe).decode())
            for key, item in (("queue", "other"), ("vhost", "other"), ("server", "amqp://other:5672"), ("cluster_id", "other")):
                changed = {**value, "config": {**value["config"], key: item}}
                with self.subTest(key=key), self.assertRaisesRegex(probe.ProbeError, "source_binding"):
                    probe.safe_response("backlog", probe.encoded(changed), 7, conf["source_binding"])

    def test_rabbit_mapping_needs_independent_same_broker_witness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = rabbit_config(root)
            with self.assertRaisesRegex(probe.ProbeError, "evidence_missing"):
                probe.binding_witness(conf)
            witness = synthetic_broker_witness(conf, root)
            self.assertEqual(probe.broker_identity_evidence(conf), conf["broker_identity_evidence"]["sha256"])
            self.assertTrue(probe.evaluate(conf, [], None)["independent_broker_identity_required"])
            path = Path(conf["broker_identity_evidence"]["path"])
            mutations = [("candidate", "0" * 40), ("case_context_sha256", "0" * 64), ("identity", {**witness["identity"], "queue_sha256": "0" * 64}), ("identity", {**witness["identity"], "cluster_id_sha256": "0" * 64}), ("containers", {}), ("child_identities", {}), ("simultaneous_connection_pids_sha256", {}), ("both_child_closed_and_reaped", False)]
            for key, item in mutations:
                path.write_bytes(probe.encoded({**witness, key: item}))
                conf["broker_identity_evidence"]["sha256"] = probe.file_digest(path)
                with self.subTest(key=key), self.assertRaisesRegex(probe.ProbeError, "evidence_"):
                    probe.binding_witness(conf)

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
        safe = probe.safe_response("backlog", probe.encoded(value), 7, source_binding())
        self.assertEqual(safe["counts"]["accepted"], 3)
        self.assertNotIn("secret", probe.encoded(safe).decode())
        value["event_counts"]["accepted"] = 24
        with self.assertRaisesRegex(probe.ProbeError, "alias_mismatch"):
            probe.safe_response("backlog", probe.encoded(value), 7, source_binding())
        value["event_counts"]["accepted"] = 3
        value["event_counts"]["new_unknown_state"] = 0
        with self.assertRaisesRegex(probe.ProbeError, "unknown_receipt_counter"):
            probe.safe_response("backlog", probe.encoded(value), 7, source_binding())


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


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(probe, "clock_domain", return_value=DOMAIN))
        self.real_identity = getattr(probe, "process_identity", None)
        self.identity = self.enterContext(patch.object(probe, "process_identity", return_value=PROCESS, create=True))
        readlink = probe.os.readlink
        self.enterContext(patch.object(probe.os, "readlink", side_effect=lambda path, *args, **kwargs: PROCESS["pid_namespace"] if str(path) == "/proc/self/ns/pid" else readlink(path, *args, **kwargs)))
        self.clock = self.enterContext(patch.object(probe.time, "monotonic_ns", return_value=20 * probe.NS))

    def test_historical_ready_after_stop_cannot_start_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            sampler_fixture(conf)
            path = producer_fixture(conf)
            probe.paths(conf)["stop"].touch()
            with patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                with self.assertRaisesRegex(probe.ProbeError, "probe_stopped"):
                    probe.run_producer(conf, path, "http")
                run.assert_not_called()
            self.assertFalse(probe.paths(conf)["producer_start"].exists())

    def test_expired_reused_dead_ended_or_mismatched_owner_cannot_launch(self):
        for kind in ("expired", "insufficient", "end", "analysis", "dead", "reused", "pidns", "boot", "time_ns", "missing_ticks", "wrong_run", "wrong_start", "future_ready"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                conf = config(Path(directory))
                sampler_fixture(conf)
                path, names = producer_fixture(conf), probe.paths(conf)
                owner = probe.bounded_json(names["pid"])
                self.clock.return_value = 20 * probe.NS
                self.identity.side_effect, self.identity.return_value = None, PROCESS
                if kind == "expired":
                    self.clock.return_value = 1800 * probe.NS
                elif kind == "insufficient":
                    self.clock.return_value = (1800 - 606) * probe.NS + 1
                elif kind == "end":
                    with names["log"].open("ab") as output:
                        output.write(probe.encoded({"kind": "end"}) + b"\n")
                elif kind == "analysis":
                    names["analysis"].write_bytes(b"{}")
                elif kind == "dead":
                    self.identity.side_effect = probe.ProbeError("probe_not_running")
                elif kind in ("reused", "pidns", "time_ns"):
                    key = {"reused": "start_ticks", "pidns": "pid_namespace", "time_ns": "time_namespace"}[kind]
                    self.identity.return_value = {**PROCESS, key: 99 if kind == "reused" else "namespace:[2]"}
                elif kind == "boot":
                    owner["clock_domain"] = {**DOMAIN, "boot_id": "11111111-1111-1111-1111-111111111111"}
                elif kind == "missing_ticks":
                    owner.pop("start_ticks")
                elif kind == "wrong_run":
                    owner["run_id"] = "other-run"
                elif kind == "wrong_start":
                    owner["sampler_start_ns"] = 1
                    owner["deadline_ns"] += 1
                else:
                    self.clock.return_value = 18 * probe.NS
                names["pid"].write_bytes(probe.encoded(owner))
                with patch.object(probe.subprocess, "run") as run:
                    with self.assertRaises(probe.ProbeError):
                        probe.run_producer(conf, path, "http")
                    run.assert_not_called()
                self.assertFalse(names["producer_start"].exists())

    def test_fresh_check_after_snapshot_stops_before_producer_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            sampler_fixture(conf)
            path, names = producer_fixture(conf), probe.paths(conf)
            with patch.object(probe.os, "fsync", side_effect=lambda _fd: names["stop"].touch()), patch.object(probe.subprocess, "run") as run:
                with self.assertRaisesRegex(probe.ProbeError, "probe_stopped"):
                    probe.run_producer(conf, path, "http")
                run.assert_not_called()
            self.assertFalse(names["producer_start"].exists())
            self.assertEqual(list(Path(directory).glob(".*producer-config-*.json")), [])

    def test_post_child_check_retains_actual_exit_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            sampler_fixture(conf)
            path, names = producer_fixture(conf), probe.paths(conf)

            def finish_child(*_args, **_kwargs):
                names["stop"].touch()
                return producer_artifacts(conf)

            with patch.object(probe.subprocess, "run", side_effect=finish_child) as run:
                with self.assertRaisesRegex(probe.ProbeError, "probe_stopped"):
                    probe.run_producer(conf, path, "http")
                run.assert_called_once()
            result = probe.bounded_json(names["producer_end"])
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["sampler_error"], "probe_stopped")
            self.assertEqual(list(Path(directory).glob(".*producer-config-*.json")), [])

    def test_short_valid_window_and_live_normal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            conf["deadline_seconds"] = 622
            conf["request_timeout_seconds"] = 1
            ready = sampler_fixture(conf)
            path, names = producer_fixture(conf), probe.paths(conf)
            self.clock.return_value = 20 * probe.NS  # Baseline at 19; 600 + 1 + 1 remain.
            self.assertTrue(probe.wait_ready(conf, 1)["ready"])
            digest = probe.assert_sampler_active(conf, ready, DOMAIN, before_producer=True)
            self.clock.return_value += 1
            with self.assertRaisesRegex(probe.ProbeError, "observation_window_insufficient"):
                probe.assert_sampler_active(conf, ready, DOMAIN, before_producer=True)
            self.clock.return_value = 20 * probe.NS
            with patch.object(probe.subprocess, "run", side_effect=lambda *_args, **_kwargs: producer_artifacts(conf)) as run:
                self.assertEqual(probe.run_producer(conf, path, "http")["returncode"], 0)
                run.assert_called_once()
            for key in ("producer_start", "producer_end"):
                self.assertEqual(probe.bounded_json(names[key])["sampler_identity_sha256"], digest)

    def test_process_identity_rejects_zombies_and_handles_comm_parentheses(self):
        # Exercise the real /proc parser using bounded in-memory stat data.
        with patch.object(probe, "process_identity", new=self.real_identity):
            for state in ("S", "R", "Z", "X", "T"):
                raw = b"123 (name ) with spaces) " + b" ".join([state.encode()] + [b"0"] * 18 + [b"7"])
                with self.subTest(state=state), patch.object(Path, "open", return_value=io.BytesIO(raw)), patch.object(probe.os, "readlink", side_effect=["pid:[1]", "time:[1]"]):
                    if state in ("S", "R"):
                        self.assertEqual(probe.process_identity(123), PROCESS)
                    else:
                        with self.assertRaisesRegex(probe.ProbeError, "probe_not_running"):
                            probe.process_identity(123)

    def test_active_snapshot_ignores_only_unterminated_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            ready = sampler_fixture(conf)
            path = probe.paths(conf)["log"]
            with path.open("ab") as output:
                output.write(b'{"kind":"sample","endpoint":')
            self.assertTrue(probe.assert_sampler_active(conf, ready, DOMAIN, before_producer=True))
            # Offline final evidence must never silently discard the same tail.
            with self.assertRaises(ValueError):
                probe.analyze(conf)

    def test_active_snapshot_rejects_complete_bad_rows_and_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            sampler_fixture(conf)
            path = probe.paths(conf)["log"]
            original = path.read_bytes()
            for suffix, limit, count, code in ((b"{bad}\n", conf["log_max_bytes"], 12000, "invalid_evidence_row"), (b"[]\n", conf["log_max_bytes"], 12000, "invalid_evidence_row"), (b"{bad}\n{", conf["log_max_bytes"], 12000, "invalid_evidence_row"), (b"{", conf["log_max_bytes"], 2, "row_budget"), (b"x" * 16384, conf["log_max_bytes"], 12000, "row_budget"), (b"", len(original) - 1, 12000, "file_too_large")):
                path.write_bytes(original + suffix)
                with self.subTest(suffix=suffix[:12], count=count, limit=limit), self.assertRaisesRegex(probe.ProbeError, code):
                    probe._read_active_rows(path, limit, count)

    def test_completion_keeps_config_failure_and_independent_sampler_facts(self):
        for mode in ("corrupt", "missing", "changed", "corrupt_stopped"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                conf = config(Path(directory))
                sampler_fixture(conf)
                path, names = producer_fixture(conf), probe.paths(conf)

                def child(argv, **_kwargs):
                    result = producer_artifacts(conf, 23)
                    frozen = Path(argv[-1])
                    if mode == "missing":
                        frozen.unlink()
                    else:
                        frozen.write_bytes(b'{"changed":true}' if mode == "changed" else b"invalid-private-contents")
                    if mode == "corrupt_stopped":
                        names["stop"].touch()
                    return result

                expected = FileNotFoundError if mode == "missing" else probe.ProbeError
                with patch.object(probe.subprocess, "run", side_effect=child) as run:
                    with self.assertRaises(expected) as failure:
                        probe.run_producer(conf, path, "http")
                    run.assert_called_once()
                end = probe.bounded_json(names["producer_end"])
                self.assertEqual(end["returncode"], 23)
                self.assertTrue(end["returncode_known"])
                self.assertEqual(end["sampler_error"], "probe_stopped" if mode == "corrupt_stopped" else None)
                code = "producer_configuration_changed" if mode == "changed" else "producer_configuration_unavailable"
                self.assertEqual(end["evidence_error_codes"][0], code)
                self.assertEqual(end["frozen_config_sha256"] is None, mode != "changed")
                self.assertTrue(end["log_sha256"] and end["result_sha256"])
                self.assertNotIn("invalid-private-contents", probe.encoded(end).decode())
                if mode == "corrupt_stopped":
                    self.assertEqual(str(failure.exception), "invalid_json_file")
                    self.assertEqual(end["evidence_error_codes"], [code, "producer_sampler_unavailable"])

    def test_completion_hash_failures_keep_actual_exit_and_other_evidence(self):
        for kind in ("log", "result"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                conf = config(Path(directory))
                sampler_fixture(conf)
                path, names = producer_fixture(conf), probe.paths(conf)
                error, digest = OSError("private-address-must-not-leak"), probe.file_digest

                def read_digest(value):
                    if Path(value).resolve() == Path(conf["producer_" + kind + "_path"]).resolve():
                        raise error
                    return digest(value)

                with patch.object(probe.subprocess, "run", side_effect=lambda *_args, **_kwargs: producer_artifacts(conf, 17)), patch.object(probe, "file_digest", side_effect=read_digest):
                    with self.assertRaises(OSError) as failure:
                        probe.run_producer(conf, path, "http")
                self.assertIs(failure.exception, error)
                end = probe.bounded_json(names["producer_end"])
                self.assertEqual(end["returncode"], 17)
                self.assertIsNone(end["sampler_error"])
                self.assertEqual(end["evidence_error_codes"], ["producer_" + kind + "_unavailable"])
                self.assertIsNone(end[kind + "_sha256"])
                self.assertTrue(end[("result" if kind == "log" else "log") + "_sha256"])
                self.assertNotIn("private-address", probe.encoded(end).decode())

    def test_post_sampler_interrupt_records_end_then_propagates_same_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            sampler_fixture(conf)
            path, names = producer_fixture(conf), probe.paths(conf)
            active, error = probe.assert_sampler_active, KeyboardInterrupt()

            def check(*args, **kwargs):
                if not kwargs["before_producer"]:
                    raise error
                return active(*args, **kwargs)

            with patch.object(probe, "assert_sampler_active", side_effect=check), patch.object(probe.subprocess, "run", side_effect=lambda *_args, **_kwargs: producer_artifacts(conf)):
                with self.assertRaises(KeyboardInterrupt) as failure:
                    probe.run_producer(conf, path, "http")
            self.assertIs(failure.exception, error)
            end = probe.bounded_json(names["producer_end"])
            self.assertEqual(end["returncode"], 0)
            self.assertEqual(end["sampler_error"], "probe_liveness_unproven")
            self.assertEqual(end["evidence_error_codes"], ["producer_sampler_unavailable"])
            self.assertTrue(end["frozen_config_sha256"] and end["log_sha256"] and end["result_sha256"])

    def test_end_write_and_cleanup_errors_do_not_replace_primary_error(self):
        for primary in (False, True):
            with self.subTest(primary=primary), tempfile.TemporaryDirectory() as directory:
                conf = config(Path(directory))
                sampler_fixture(conf)
                path, names = producer_fixture(conf), probe.paths(conf)
                read_error, end_error = OSError("private-read-error"), OSError("private-end-error")
                digest, write, unlink = probe.file_digest, probe.write_private, Path.unlink

                def read_digest(value):
                    if primary and Path(value).resolve() == Path(conf["producer_log_path"]).resolve():
                        raise read_error
                    return digest(value)

                def write_receipt(value, receipt):
                    if value == names["producer_end"]:
                        raise end_error
                    return write(value, receipt)

                def cleanup(value, *args, **kwargs):
                    if "-producer-config-" in value.name:
                        raise OSError("private-cleanup-error")
                    return unlink(value, *args, **kwargs)

                with patch.object(probe.subprocess, "run", side_effect=lambda *_args, **_kwargs: producer_artifacts(conf)), patch.object(probe, "file_digest", side_effect=read_digest), patch.object(probe, "write_private", side_effect=write_receipt), patch.object(Path, "unlink", new=cleanup):
                    with self.assertRaises(OSError) as failure:
                        probe.run_producer(conf, path, "http")
                self.assertIs(failure.exception, read_error if primary else end_error)
                self.assertFalse(names["producer_end"].exists())
                self.assertIn("owned-config cleanup failed: OSError", failure.exception.__notes__)
                if primary:
                    self.assertIn("producer_end write failed: OSError", failure.exception.__notes__)

    def test_spawn_failure_is_not_a_known_exit_and_timeout_remains_a_sentinel(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory:
                conf = config(Path(directory))
                sampler_fixture(conf)
                path, names = producer_fixture(conf), probe.paths(conf)
                error = probe.subprocess.TimeoutExpired("private-command", 90) if timeout else OSError("private-spawn-error")

                def child(*_args, **_kwargs):
                    if timeout:
                        producer_artifacts(conf)
                    raise error

                with patch.object(probe.subprocess, "run", side_effect=child) as run:
                    if timeout:
                        self.assertEqual(probe.run_producer(conf, path, "http")["returncode"], -1)
                    else:
                        with self.assertRaises(OSError) as failure:
                            probe.run_producer(conf, path, "http")
                        self.assertIs(failure.exception, error)
                    run.assert_called_once()
                self.assertTrue(names["producer_start"].exists())
                if timeout:
                    end = probe.bounded_json(names["producer_end"])
                    self.assertEqual(end["returncode"], -1)
                    self.assertFalse(end["returncode_known"])
                else:
                    self.assertFalse(names["producer_end"].exists())


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        # Synthetic Linux owner; these tests do not inspect any live process.
        self.enterContext(patch.object(probe, "process_identity", return_value=PROCESS))
        readlink = probe.os.readlink
        self.enterContext(patch.object(probe.os, "readlink", side_effect=lambda path, *args, **kwargs: PROCESS["pid_namespace"] if str(path) == "/proc/self/ns/pid" else readlink(path, *args, **kwargs)))
        self.enterContext(patch.object(probe.time, "monotonic_ns", return_value=20 * probe.NS))

    def test_replaced_or_transiently_overwritten_config_cannot_redirect_child(self):
        for mode in ("same_inode", "rename"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                conf = config(root)
                sampler_fixture(conf)
                path = Path(conf["producer_config_path"])
                valid = {"run_id": conf["run_id"], "kind": "capacity", "count": 10000, "output_path": str(root), "http_concurrency": 125, "ingress_url": "https://private.example/api/ingress/private-route"}
                changed = {**valid, "ingress_url": "https://wrong.example/api/ingress/other"}
                original = probe.encoded(valid)
                path.write_bytes(original)
                path.chmod(0o600)
                original_inode = path.stat().st_ino

                def replace_while_child_reads(argv, **_kwargs):
                    frozen = Path(argv[-1])
                    frozen_inode = frozen.stat().st_ino
                    self.assertEqual(frozen.stat().st_mode & 0o777, 0o600)
                    if mode == "rename":
                        replacement = root / "replacement.json"
                        replacement.write_bytes(probe.encoded(changed))
                        replacement.replace(path)
                    else:
                        path.write_bytes(probe.encoded(changed))
                        self.assertEqual(path.stat().st_ino, original_inode)
                    self.assertEqual(probe.bounded_json(frozen), valid)
                    self.assertNotEqual(frozen_inode, original_inode)
                    if mode == "same_inode":
                        path.write_bytes(original)
                    return producer_artifacts(conf)

                with patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe.subprocess, "run", side_effect=replace_while_child_reads):
                    self.assertEqual(probe.run_producer(conf, path, "http")["returncode"], 0)
                self.assertEqual(list(root.glob(".*producer-config-*.json")), [])
                if mode == "rename":
                    with self.assertRaisesRegex(probe.ProbeError, "target_mismatch"):
                        probe.producer_evidence(conf, {"monotonic_ns": 0}, DOMAIN)

    def test_config_snapshot_failure_cleans_only_owned_file_and_keeps_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = config(root)
            sampler_fixture(conf)
            path = Path(conf["producer_config_path"])
            path.write_bytes(probe.encoded({"run_id": conf["run_id"], "kind": "capacity", "count": 10000, "output_path": str(root), "http_concurrency": 125, "ingress_url": "https://private.example/api/ingress/private-route"}))
            with patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe.os, "fsync", side_effect=OSError("snapshot_sync_failed")), patch.object(probe.subprocess, "run") as run:
                with self.assertRaisesRegex(OSError, "snapshot_sync_failed"):
                    probe.run_producer(conf, path, "http")
                run.assert_not_called()
            self.assertEqual(list(root.glob(".*producer-config-*.json")), [])
            with patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe.os, "fsync", side_effect=OSError("snapshot_sync_failed")), patch.object(Path, "unlink", side_effect=OSError("cleanup_failed")), patch.object(probe.subprocess, "run") as run:
                with self.assertRaisesRegex(OSError, "snapshot_sync_failed") as failure:
                    probe.run_producer(conf, path, "http")
                run.assert_not_called()
            self.assertIn("owned-config cleanup failed: OSError", failure.exception.__notes__)
            real_close = probe.os.close

            def close_then_fail(fd):
                real_close(fd)
                raise OSError("close_failed")

            with patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe.os, "fsync", side_effect=OSError("snapshot_sync_failed")), patch.object(probe.os, "close", side_effect=close_then_fail), patch.object(probe.subprocess, "run") as run:
                with self.assertRaisesRegex(OSError, "snapshot_sync_failed") as failure:
                    probe.run_producer(conf, path, "http")
                run.assert_not_called()
            self.assertIn("owned-config fd close failed: OSError", failure.exception.__notes__)

    def test_analyze_cannot_rebind_api_or_omit_source_target_witness(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            path = probe.paths(conf)["log"]
            original = evidence_rows(conf)

            def save(rows):
                path.write_bytes(b"\n".join(probe.encoded(row) for row in rows) + b"\n")

            save(original)
            producer = {"sender_begin_ns": 20 * probe.NS, "clock_domain_proven": True}
            with patch.object(probe, "producer_evidence", return_value=producer):
                self.assertTrue(probe.analyze(conf)["management_and_timing_passed"])
                rebound = probe.analyze({**conf, "api_base": "https://other-private.example"})
                self.assertFalse(rebound["management_and_timing_passed"])
                for kind in ("start", "ready"):
                    for key in ("source_binding_sha256", "target_binding_sha256"):
                        rows = copy.deepcopy(original)
                        next(row for row in rows if row["kind"] == kind).pop(key)
                        save(rows)
                        with self.subTest(kind=kind, key=key):
                            self.assertFalse(probe.analyze(conf)["management_and_timing_passed"])
                rows = copy.deepcopy(original)
                next(row for row in rows if row.get("endpoint") == "backlog")["snapshot"].pop("source_binding_sha256")
                save(rows)
                self.assertIn("backlog_source_binding_mismatch", probe.analyze(conf)["failure_codes"])

    def test_producer_target_mismatch_stops_before_receipt_or_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage in ("http", "publish"):
                case = root / stage
                case.mkdir()
                conf = config(case) if stage == "http" else rabbit_config(case)
                if stage == "publish":
                    synthetic_broker_witness(conf, case)
                sampler_fixture(conf)
                valid = {"run_id": conf["run_id"], "kind": "capacity", "count": 10000, "output_path": str(case), "http_concurrency": 125, "ingress_url": "https://private.example/api/ingress/private-route", "queue": "private-queue", "amqp_url": "amqp://private-user:private-password@publisher-private:5672/private%2Fvhost+"}
                mutations = (("ingress_url", "https://other.example/api/ingress/private-route"), ("ingress_url", "https://private.example/api/ingress/other"), ("ingress_url", None)) if stage == "http" else (("queue", "other"), ("amqp_url", "amqp://publisher-private:5672/other"), ("amqp_url", "amqp://other:5672/private%2Fvhost+"))
                path = Path(conf["producer_config_path"])
                with patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe.subprocess, "run", side_effect=lambda *_args, **_kwargs: producer_artifacts(conf)) as run:
                    for key, value in mutations:
                        path.write_bytes(probe.encoded({**valid, key: value}))
                        with self.subTest(stage=stage, key=key), self.assertRaises(probe.ProbeError):
                            probe.run_producer(conf, path, stage)
                        run.assert_not_called()
                        self.assertFalse(probe.paths(conf)["producer_start"].exists())
                    path.write_bytes(probe.encoded(valid))
                    self.assertEqual(probe.run_producer(conf, path, stage)["returncode"], 0)
                    run.assert_called_once()
                for name in ("producer_start", "producer_end"):
                    receipt = probe.bounded_json(probe.paths(conf)[name])
                    self.assertEqual(receipt["target_binding_sha256"], probe.target_fingerprint(conf))
                    self.assertEqual(receipt["source_binding_sha256"], probe.source_fingerprint(conf))
                    self.assertEqual(receipt["producer_config_sha256"], probe.file_digest(path))
                    self.assertNotIn("private", probe.encoded(receipt).decode())

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
                row["snapshot"] = probe.safe_response("backlog", probe.encoded(backlog(10000, 10000)), 7, source_binding())
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
                    row["snapshot"] = probe.safe_response("backlog", probe.encoded(backlog(5000, 0)), 7, source_binding())
                elif slot == 81:
                    row["snapshot"] = probe.safe_response("backlog", probe.encoded(backlog(10000, 0)), 7, source_binding())
        result = probe.evaluate(conf, rows, {"sender_begin_ns": 20 * probe.NS, "clock_domain_proven": True})
        self.assertEqual(result["timing"]["sender_begin_ns"], 20 * probe.NS)
        self.assertAlmostEqual(result["timing"]["acceptance_seconds_upper"], 61.01)
        self.assertIn("causal_time_upper_bound_exceeded", result["failure_codes"])
        self.assertFalse(result["management_and_timing_passed"])

    def test_full_ten_thousand_producer_log_needs_same_domain_and_unchanged_files(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = config(Path(directory))
            names = probe.paths(conf)
            ready = sampler_fixture(conf)
            probe.write_private(Path(conf["producer_config_path"]), {"ingress_url": conf["api_base"] + conf["source_binding"]["ingress_path"]})
            witness = {"run_id": conf["run_id"], "stage": "http", "clock_domain": DOMAIN, "workload_sha256": probe.WORKLOAD_SHA256, **probe.binding_witness(conf), "producer_config_sha256": probe.file_digest(Path(conf["producer_config_path"])), "sampler_identity_sha256": probe.hashlib.sha256(probe.encoded(probe.bounded_json(names["pid"]))).hexdigest()}
            probe.write_private(names["producer_start"], {**witness, "monotonic_ns": 20 * probe.NS})
            with Path(conf["producer_log_path"]).open("wb") as output:
                output.writelines(probe.encoded({"kind": "http_attempt", "seq": seq, "attempt": 1, "started_ns": 20 * probe.NS + seq * 1000}) + b"\n" for seq in range(1, 10001))
            probe.write_private(Path(conf["producer_result_path"]), {"run_id": conf["run_id"], "stage": "http", "count": 10000, "concurrency_peak": 125, "burst_window_passed": True, "all_responses_accepted": True, "retry_after_contract_passed": True, "local_response_deadline_passed": True})
            probe.write_private(names["producer_end"], {**witness, "monotonic_ns": 30 * probe.NS, "returncode": 0, "returncode_known": True, "evidence_error_codes": [], "frozen_config_sha256": witness["producer_config_sha256"], "sampler_error": None, "log_sha256": probe.file_digest(Path(conf["producer_log_path"])), "result_sha256": probe.file_digest(Path(conf["producer_result_path"]))})
            result = probe.producer_evidence(conf, ready, DOMAIN)
            self.assertEqual(result["sender_begin_ns"], 20 * probe.NS + 1000)
            self.assertEqual(result["first_request_count"], 10000)
            for name in ("producer_start", "producer_end"):
                path = names[name]
                receipt = probe.bounded_json(path)
                for key in ("source_binding_sha256", "target_binding_sha256", "producer_config_sha256", "sampler_identity_sha256"):
                    path.write_bytes(probe.encoded({k: v for k, v in receipt.items() if k != key}))
                    with self.subTest(name=name, key=key), self.assertRaisesRegex(probe.ProbeError, "provenance"):
                        probe.producer_evidence(conf, ready, DOMAIN)
                path.write_bytes(probe.encoded(receipt))
            end = probe.bounded_json(names["producer_end"])
            invalid_ends = [{key: item for key, item in end.items() if key != missing} for missing in ("sampler_error", "returncode_known", "evidence_error_codes", "frozen_config_sha256")]
            invalid_ends += [{**end, "sampler_error": "probe_stopped"}, {**end, "monotonic_ns": 1800 * probe.NS}, {**end, "returncode_known": False}, {**end, "evidence_error_codes": ["producer_log_unavailable"]}, {**end, "frozen_config_sha256": "0" * 64}]
            for value in invalid_ends:
                names["producer_end"].write_bytes(probe.encoded(value))
                with self.subTest(end=value), self.assertRaises(probe.ProbeError):
                    probe.producer_evidence(conf, ready, DOMAIN)
            names["producer_end"].write_bytes(probe.encoded(end))
            producer_path = Path(conf["producer_config_path"])
            original_config = producer_path.read_bytes()
            producer_path.write_bytes(probe.encoded({"ingress_url": conf["api_base"] + conf["source_binding"]["ingress_path"], "changed_private_option": True}))
            with self.assertRaisesRegex(probe.ProbeError, "provenance"):
                probe.producer_evidence(conf, ready, DOMAIN)
            producer_path.write_bytes(original_config)
            with self.assertRaisesRegex(probe.ProbeError, "owner_mismatch|clock_provenance"):
                probe.producer_evidence(conf, ready, {**DOMAIN, "time_namespace": "time:[2]"})
            with Path(conf["producer_log_path"]).open("ab") as output:
                output.write(b"{}\n")
            with self.assertRaisesRegex(probe.ProbeError, "unchanged"):
                probe.producer_evidence(conf, ready, DOMAIN)

    def test_producer_wrapper_refuses_128_plus_management_without_running_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root, conf = Path(directory), config(Path(directory))
            sampler_fixture(conf)
            path = root / "producer-config.private.json"
            probe.write_private(path, {"run_id": conf["run_id"], "kind": "capacity", "count": 10000, "http_concurrency": 128})
            with patch.object(probe, "clock_domain", return_value=DOMAIN), patch.object(probe.subprocess, "run") as run:
                with self.assertRaisesRegex(probe.ProbeError, "exceeds_128"):
                    probe.run_producer(conf, path, "http")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
