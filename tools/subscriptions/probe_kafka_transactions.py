#!/usr/bin/env python3
"""Compare real aborted/committed records with the product Kafka SDK transport.

Run with the backend Python and configured Control environment. Use a task-owned
Kafka fixture and private kafka-python connection JSON. This creates and deletes
one temporary topic. It does not connect to PostgreSQL/RabbitMQ or run the owner
loop, a Worker, or business-target delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from probe_clients import KAFKA_BUDGET, kafka_connection

# Run with the backend Python environment; no machine-specific import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "src"))


def consume_to_end(consumer: Any, partition: Any, end: int, poll: Any) -> list[dict[str, Any]]:
    consumer.assign([partition])
    consumer.seek(partition, 0)
    deadline = time.monotonic() + 15
    rows = []
    while consumer.position(partition) < end:
        if time.monotonic() >= deadline:
            raise TimeoutError("Probe did not reach the known source end")
        for batch in poll().values():
            for record in batch:
                rows.append(
                    {
                        "offset": record.offset,
                        "key_hex": record.key.hex() if record.key is not None else None,
                        "value_hex": record.value.hex() if record.value is not None else None,
                    }
                )
    return rows


def probe(config_file: Path) -> dict[str, Any]:
    from kafka import KafkaAdminClient, KafkaConsumer, KafkaProducer, TopicPartition
    from kafka.admin import NewTopic

    from dlr.control.services.subscription_kafka import KafkaPythonTransport

    started = time.monotonic()
    result: dict[str, Any] = {
        "scope": "Real Kafka transactions and the product SDK transport; owner loop, T1/T2 and "
        "business delivery are not exercised.",
        "client_version": importlib.metadata.version("kafka-python"),
        "product_transport_sha256": hashlib.sha256(
            Path(inspect.getfile(KafkaPythonTransport)).read_bytes()
        ).hexdigest(),
        "passed": False,
    }
    connection = kafka_connection(config_file)
    topic = "dlr-b2-transaction-" + uuid.uuid4().hex[:16]
    admin = producer = ordinary = transport = None
    created = False
    phase = "admin_connect"
    try:
        admin = KafkaAdminClient(**connection, request_timeout_ms=10000, bootstrap_timeout_ms=3000)
        phase = "create_topic"
        admin.create_topics(
            [NewTopic(topic, num_partitions=1, replication_factor=1)], timeout_ms=5000
        )
        created = True
        phase = "initialize_transactions"
        producer = KafkaProducer(
            **connection,
            transactional_id="dlr-b2-transaction-" + uuid.uuid4().hex,
            enable_idempotence=True,
            acks="all",
            request_timeout_ms=10000,
            max_block_ms=10000,
            delivery_timeout_ms=15000,
            transaction_timeout_ms=15000,
        )
        producer.init_transactions()
        produced: dict[str, int] = {}
        for marker, committed in (
            ("committed-a", True),
            ("aborted-b", False),
            ("committed-c", True),
        ):
            phase = "produce_" + marker
            producer.begin_transaction()
            record = producer.send(
                topic, partition=0, key=b"fixture-id", value=marker.encode()
            ).get(timeout=10)
            produced[marker] = record.offset
            if committed:
                producer.commit_transaction()
            else:
                producer.abort_transaction()
        partition = TopicPartition(topic, 0)
        phase = "read_uncommitted_oracle"
        ordinary = KafkaConsumer(
            **connection,
            **KAFKA_BUDGET,
            enable_auto_commit=False,
            auto_offset_reset="none",
            isolation_level="read_uncommitted",
            allow_auto_create_topics=False,
        )
        end = ordinary.end_offsets([partition], timeout_ms=3000)[partition]
        uncommitted = consume_to_end(
            ordinary, partition, end, lambda: ordinary.poll(timeout_ms=200)
        )
        phase = "product_transport_read_committed"
        bootstrap = connection["bootstrap_servers"]
        configuration: dict[str, Any] = {
            "source_type": "kafka",
            "cluster_id": str(admin.describe_cluster()["cluster_id"]),
            "bootstrap_servers": bootstrap.split(",") if isinstance(bootstrap, str) else bootstrap,
            "topics": [topic],
            "group_id": "dlr-b2-isolation-" + uuid.uuid4().hex,
            "initial_position": "earliest",
            "security_protocol": connection.get("security_protocol", "PLAINTEXT"),
        }
        if connection.get("ssl_cafile"):
            configuration["ca_pem"] = Path(connection["ssl_cafile"]).read_text()
        if connection.get("sasl_mechanism"):
            configuration["sasl_mechanism"] = connection["sasl_mechanism"]
        fields = {
            key: connection[source]
            for key, source in (
                ("username", "sasl_plain_username"),
                ("password", "sasl_plain_password"),
            )
            if source in connection
        }
        transport = KafkaPythonTransport(configuration, fields, "dlr-b2-isolation-probe")
        identity = transport.metadata()
        committed = consume_to_end(transport.consumer, partition, end, transport.poll)
        phase = "compare_independent_source_oracles"
        expected_all = [marker.encode().hex() for marker in produced]
        expected_committed = [b"committed-a".hex(), b"committed-c".hex()]
        checks = {
            "uncommitted_sees_all_three_source_records": [row["value_hex"] for row in uncommitted]
            == expected_all,
            "product_transport_excludes_aborted_record": [row["value_hex"] for row in committed]
            == expected_committed,
            "committed_offsets_equal_producer_metadata": [row["offset"] for row in committed]
            == [produced["committed-a"], produced["committed-c"]],
            "real_transaction_offset_hole": produced["committed-c"] > produced["committed-a"] + 1,
            "key_bytes_preserved": all(
                row["key_hex"] == b"fixture-id".hex() for row in uncommitted + committed
            ),
            "physical_topic_identity_verified": bool(identity.topic_ids.get(topic)),
            "product_transport_uses_read_committed": transport.consumer.config["isolation_level"]
            == "read_committed",
            "product_transport_auto_commit_disabled": transport.consumer.config[
                "enable_auto_commit"
            ]
            is False,
        }
        result.update(
            checks=checks,
            source_produced_offsets=produced,
            source_log_end=end,
            read_uncommitted=uncommitted,
            product_read_committed=committed,
            security_protocol=configuration["security_protocol"],
        )
        result["passed"] = all(checks.values())
    except Exception as error:  # noqa: BLE001 - record only the class, never secret-bearing SDK text
        result.update(phase=phase, error_type=type(error).__name__)
    finally:
        cleanup_errors = []
        for label, client in (
            ("transport", transport),
            ("ordinary", ordinary),
            ("producer", producer),
        ):
            if client is None:
                continue
            try:
                if label == "transport":
                    client.close()
                elif label == "ordinary":
                    client.close(autocommit=False, timeout_ms=3000)
                else:
                    client.close(timeout=3)
            except Exception as error:  # noqa: BLE001 - cleanup all resources and redact SDK text
                cleanup_errors.append({"resource": label, "error_type": type(error).__name__})
        if admin is not None:
            try:
                if created:
                    admin.delete_topics([topic], timeout_ms=5000)
                    deadline = time.monotonic() + 5
                    while topic in admin.list_topics():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Task-owned topic deletion did not converge")
                        time.sleep(0.1)
                admin.close()
            except Exception as error:  # noqa: BLE001 - cleanup all resources and redact SDK text
                cleanup_errors.append(
                    {"resource": "topic_admin", "error_type": type(error).__name__}
                )
        result["cleanup_complete"] = not cleanup_errors
        result["cleanup_scope"] = (
            "Probe clients closed and task-owned topic absent; Kafka "
            "transaction coordinator metadata follows the broker's expiration policy."
        )
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            result["passed"] = False
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Private connection JSON")
    parser.add_argument("--output", required=True, type=Path, help="Private JSON evidence path")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # Do not copy source addresses or SDK exception text.
    try:
        result = probe(args.config)
    except Exception as error:  # noqa: BLE001 - bootstrap failures must not print configuration
        result = {
            "passed": False,
            "cleanup_complete": True,
            "elapsed_seconds": 0,
            "phase": "probe_bootstrap",
            "error_type": type(error).__name__,
        }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps({key: result[key] for key in ("passed", "cleanup_complete", "elapsed_seconds")})
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
