"""Fixture unit checks only; these do not claim live RabbitMQ/Worker acceptance."""

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.ingress_b0.workload import (
    BODY_BYTES,
    PUBLISH_LOG_MAX_BYTES,
    FixtureError,
    PrivateLog,
    Publisher,
    generate,
    input_record,
    read_inputs,
    verify_rows,
)


def fixture_rows(count=20):
    inputs = [input_record("test-run", i) for i in range(1, count + 1)]
    rows = [
        {
            "run_id": x["run_id"],
            "message_id": x["message_id"],
            "seq": x["seq"],
            "raw_sha256": x["raw_sha256"],
            "normalized_value": x["expected_normalized_value"],
            "writes": 1,
            "attempts": 1,
        }
        for x in inputs
    ]
    attempts = [
        {
            "run_id": x["run_id"],
            "message_id": x["message_id"],
            "ordinal": 1,
            "outcome": "inserted",
            "raw_sha256": x["raw_sha256"],
            "normalized_value": x["expected_normalized_value"],
        }
        for x in inputs
    ]
    return inputs, rows, attempts


class MemorySink:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(row)


class FakeLoop:
    def __init__(self):
        self.stopped = False

    def call_later(self, *_args):
        return None

    def stop(self):
        self.stopped = True


class FakeConnection:
    def __init__(self):
        self.ioloop = FakeLoop()
        self.is_open = True

    def close(self):
        self.is_open = False


class FakeChannel:
    def __init__(self):
        self.messages = []

    def basic_publish(self, **kwargs):
        self.messages.append(kwargs)


def publisher(count=200):
    inputs = [input_record("test-run", i) for i in range(1, count + 1)]
    p = Publisher({"queue": "test-b0"}, inputs, MemorySink())
    p.connection, p.channel = FakeConnection(), FakeChannel()
    p.pika = SimpleNamespace(BasicProperties=lambda **kwargs: SimpleNamespace(**kwargs))
    return p


def confirmation(tag, multiple=False, ack=True):
    return SimpleNamespace(
        method=SimpleNamespace(
            delivery_tag=tag,
            multiple=multiple,
            NAME="Basic.Ack" if ack else "Basic.Nack",
        )
    )


class InputTests(unittest.TestCase):
    def test_warmup_ids_are_disjoint_from_formal_epoch_identities(self):
        warmup = {
            input_record("warmup", i, warmup=True)["message_id"] for i in range(1, 21)
        }
        formal = {input_record("formal", i)["message_id"] for i in range(1, 201)}
        self.assertFalse(warmup & formal)
        self.assertEqual(min(warmup), "w000001")
        self.assertEqual(min(formal), "000001")

    def test_exact_body_identity_hash_and_independent_expected_value(self):
        for sequence in range(1, 201):
            row = input_record("sample-零".replace("零", "zero"), sequence)
            raw = base64.b64decode(row["raw_base64"], validate=True)
            decoded = json.loads(raw)
            self.assertEqual(len(raw), BODY_BYTES)
            self.assertEqual(row["raw_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row["message_id"], f"{sequence:06d}")
            self.assertEqual(decoded["value"], sequence)
            self.assertEqual(row["expected_normalized_value"], sequence * 2 + 1)

    def test_generate_is_exclusive_and_manifest_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "run_id": "sample",
                "count": 20,
                "warmup": True,
                "input_path": str(Path(tmp) / "inputs.jsonl"),
            }
            self.assertEqual(generate(config)["count"], 20)
            self.assertEqual(len(read_inputs(config)), 20)
            with self.assertRaises(FileExistsError):
                generate(config)
            path = Path(config["input_path"])
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            rows[0]["raw_sha256"] = "0" * 64
            path.write_text("\n".join(json.dumps(x) for x in rows))
            with self.assertRaises(FixtureError):
                read_inputs(config)

    def test_formal_manifest_truncated_to_twenty_cannot_pass_as_formal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "run_id": "formal",
                "count": 200,
                "input_path": str(Path(tmp) / "formal-inputs.jsonl"),
            }
            generate(config)
            path = Path(config["input_path"])
            lines = path.read_text().splitlines()
            path.write_text("\n".join(lines[:20]) + "\n")
            with self.assertRaisesRegex(FixtureError, "invalid_manifest_count"):
                read_inputs(config)

    def test_warmup_and_formal_counts_cannot_be_exchanged(self):
        for warmup, count in ((False, 20), (True, 200)):
            with (
                self.subTest(warmup=warmup, count=count),
                self.assertRaisesRegex(FixtureError, "workload_count_mismatch"),
            ):
                generate({"run_id": "invalid", "warmup": warmup, "count": count})

    def test_private_log_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.jsonl"
            writer = PrivateLog(p)
            writer.append({"safe": 1})
            writer.close()
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                PrivateLog(p)

    def test_private_log_limit_preserves_first_evidence(self):
        self.assertEqual(PUBLISH_LOG_MAX_BYTES * 2, 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bounded.jsonl"
            writer = PrivateLog(path, max_bytes=16)
            try:
                writer.append({"first": 1})
                with self.assertRaisesRegex(FixtureError, "evidence_limit_reached"):
                    writer.append({"second": 2})
            finally:
                writer.close()
            self.assertEqual(path.read_text(), '{"first":1}\n')
            self.assertLessEqual(path.stat().st_size, 16)
            with self.assertRaises(FileExistsError):
                PrivateLog(path, max_bytes=16)


class OracleTests(unittest.TestCase):
    def test_valid_rows_and_visible_duplicate_attempt(self):
        inputs, rows, attempts = fixture_rows()
        self.assertTrue(verify_rows(inputs, rows, attempts)["target_oracle_passed"])
        rows[0]["attempts"] = 2
        duplicate = {**attempts[0], "ordinal": 2, "outcome": "duplicate"}
        attempts.append(duplicate)
        result = verify_rows(inputs, rows, attempts)
        self.assertTrue(result["target_oracle_passed"])
        self.assertEqual(result["recorded_attempts"], 21)
        self.assertEqual(result["effective_writes"], 20)

    def test_oracle_detects_missing_content_conflict_and_duplicate_effect(self):
        for field, value in (
            ("raw_sha256", "0" * 64),
            ("normalized_value", -1),
            ("writes", 2),
            ("attempts", 2),
        ):
            with self.subTest(field=field):
                inputs, rows, attempts = fixture_rows()
                rows[0][field] = value
                self.assertFalse(
                    verify_rows(inputs, rows, attempts)["target_oracle_passed"]
                )
        inputs, rows, attempts = fixture_rows()
        self.assertFalse(
            verify_rows(inputs, rows[1:], attempts)["target_oracle_passed"]
        )
        self.assertFalse(
            verify_rows(inputs, rows + [rows[0]], attempts)["target_oracle_passed"]
        )

    def test_oracle_rejects_duplicate_insert_and_wrong_attempt_content(self):
        inputs, rows, attempts = fixture_rows()
        rows[0]["attempts"] = 2
        attempts.append({**attempts[0], "ordinal": 2})
        self.assertFalse(verify_rows(inputs, rows, attempts)["target_oracle_passed"])
        inputs, rows, attempts = fixture_rows()
        attempts[0]["raw_sha256"] = "0" * 64
        self.assertFalse(verify_rows(inputs, rows, attempts)["target_oracle_passed"])


class PublishTests(unittest.TestCase):
    def test_confirm_window_and_accurate_multiple_ack(self):
        p = publisher()
        p.pump()
        self.assertEqual(len(p.channel.messages), 32)
        self.assertEqual(len(p.acked), 0)  # Socket submission is not confirmation.
        p.on_confirmation(confirmation(32, multiple=True))
        self.assertEqual(len(p.acked), 32)
        self.assertEqual(len(p.channel.messages), 64)
        while not p.complete:
            p.on_confirmation(confirmation(max(p.pending), multiple=True))
        self.assertEqual(len(p.acked), 200)
        self.assertEqual(p.window_peak, 32)
        self.assertTrue(p.complete)
        for message in p.channel.messages:
            self.assertTrue(message["mandatory"])
            self.assertEqual(message["properties"].delivery_mode, 2)
            self.assertEqual(len(message["body"]), 2048)

    def test_nack_stops_and_keeps_unknown_responsibility(self):
        p = publisher()
        p.pump()
        p.on_confirmation(confirmation(1, ack=False))
        self.assertEqual(p.failure, "broker_nack")
        self.assertEqual(len(p.nacked), 1)
        self.assertEqual(len(p.pending), 31)
        self.assertEqual(p.next_index, 32)
        self.assertFalse(p.complete)

    def test_mandatory_return_and_connection_failure_cannot_be_success(self):
        p = publisher()
        p.pump()
        p.on_return(
            None,
            SimpleNamespace(reply_code=312),
            SimpleNamespace(message_id="000001"),
            b"not logged",
        )
        self.assertEqual(p.failure, "mandatory_return")
        self.assertFalse(p.complete)
        self.assertNotIn("not logged", json.dumps(p.sink.rows))
        q = publisher()
        q.pump()
        q.on_closed(q.connection, RuntimeError("secret must not escape"))
        self.assertFalse(q.complete)
        self.assertEqual(len(q.pending), 32)
        self.assertNotIn("secret must not escape", json.dumps(q.sink.rows))


if __name__ == "__main__":
    unittest.main()
