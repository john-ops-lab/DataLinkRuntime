#!/usr/bin/env python3
"""Bounded subprocess experiment: join a real group, then blackhole established sockets.

The tiny PLAINTEXT-only proxy rewrites broker ports in Metadata/FindCoordinator
responses for this experiment. It is not a reusable Kafka proxy or product code.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from probe_clients import KAFKA_BUDGET, kafka_connection


class BlackholeProxy:
    def __init__(self, upstream_host: str, upstream_port: int):
        self.upstream = (upstream_host, upstream_port)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.blackhole = threading.Event()
        self.stopped = threading.Event()
        self.sockets = []
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                incoming, _ = self.listener.accept()
            except TimeoutError:
                continue
            outgoing = socket.create_connection(self.upstream, timeout=2)
            self.sockets.extend((incoming, outgoing))
            correlations = {}
            for source, target, is_response in (
                (incoming, outgoing, False),
                (outgoing, incoming, True),
            ):
                threading.Thread(
                    target=self.relay,
                    args=(source, target, is_response, correlations),
                    daemon=True,
                ).start()

    def relay(self, source, target, is_response, correlations):
        buffer = bytearray()
        source.settimeout(0.2)
        try:
            while not self.stopped.is_set():
                try:
                    chunk = source.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buffer.extend(chunk)
                while len(buffer) >= 4:
                    size = struct.unpack_from(">i", buffer)[0]
                    if size < 0 or size > 1024 * 1024:
                        raise ValueError("Probe frame limit")
                    if len(buffer) < size + 4:
                        break
                    frame = bytes(buffer[4 : size + 4])
                    del buffer[: size + 4]
                    if self.blackhole.is_set():
                        continue
                    if is_response:
                        correlation = struct.unpack_from(">i", frame)[0]
                        if correlations.pop(correlation, None) in (3, 10):
                            frame = frame.replace(
                                struct.pack(">i", self.upstream[1]),
                                struct.pack(">i", self.port),
                            )
                    else:
                        api_key = struct.unpack_from(">h", frame)[0]
                        correlation = struct.unpack_from(">i", frame, 4)[0]
                        correlations[correlation] = api_key
                    target.sendall(struct.pack(">i", len(frame)) + frame)
        except OSError:
            pass

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
        self.listener.close()
        for connection in self.sockets:
            connection.close()


def worker(config_file: Path, topic: str, group: str) -> dict:
    from kafka import KafkaConsumer, TopicPartition
    from kafka.structs import OffsetAndMetadata

    connection = kafka_connection(config_file)
    host, port = connection["bootstrap_servers"].rsplit(":", 1)
    proxy = BlackholeProxy(host, int(port))
    print("proxy-ready", file=sys.stderr, flush=True)
    consumer = KafkaConsumer(
        **{
            **connection,
            **KAFKA_BUDGET,
            "bootstrap_servers": f"127.0.0.1:{proxy.port}",
        },
        group_id=group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    try:
        consumer.subscribe([topic])
        deadline = time.monotonic() + 20
        message = None
        while time.monotonic() < deadline:
            for batch in consumer.poll(timeout_ms=200).values():
                if batch:
                    message = batch[0]
            if message:
                break
        assert message is not None and message.value == b"close-probe"
        assert consumer.assignment()
        print("real-group-message-read", file=sys.stderr, flush=True)
        # All established broker sockets now stay open but drop requests/responses.
        proxy.blackhole.set()
        print("blackhole-enabled", file=sys.stderr, flush=True)
        started = time.monotonic()
        consumer.commit_async(
            {
                TopicPartition(topic, message.partition): OffsetAndMetadata(
                    message.offset + 1, ""
                )
            }
        )
        async_commit_seconds = time.monotonic() - started
        print("async-commit-enqueued", file=sys.stderr, flush=True)
        started = time.monotonic()
        consumer.close(autocommit=False, timeout_ms=3000)
        print("consumer-close-returned", file=sys.stderr, flush=True)
        elapsed = time.monotonic() - started
        consumer = None
        return {
            "real_group_joined_and_message_read": True,
            "fault": "established TCP sockets blackholed before async commit and close",
            "async_commit_enqueue_seconds": round(async_commit_seconds, 4),
            "blackholed_close_seconds": round(elapsed, 3),
            "configured_limits": KAFKA_BUDGET,
        }
    finally:
        if consumer is not None:
            consumer.close(autocommit=False, timeout_ms=3000)
        proxy.close()


CONFLUENT_REJECTED_BUDGET = {
    "queued.max.messages.kbytes": 1024,
    "fetch.message.max.bytes": 1048576,
    "fetch.max.bytes": 1048576,
    "receive.message.max.bytes": 2097152,
    "socket.timeout.ms": 3000,
    "session.timeout.ms": 6000,
    "heartbeat.interval.ms": 2000,
    "max.poll.interval.ms": 30000,
    "socket.connection.setup.timeout.ms": 3000,
    "api.version.request.timeout.ms": 3000,
}


def rejected_worker(config_file: Path, topic: str, group: str) -> dict:
    from confluent_kafka import Consumer, TopicPartition

    connection = json.loads(config_file.read_text())
    host, port = connection["bootstrap.servers"].rsplit(":", 1)
    proxy = BlackholeProxy(host, int(port))
    print("proxy-ready", file=sys.stderr, flush=True)
    consumer = Consumer(
        {
            **connection,
            **CONFLUENT_REJECTED_BUDGET,
            "bootstrap.servers": f"127.0.0.1:{proxy.port}",
            "log_level": 0,
            "group.id": group,
            "group.protocol": "classic",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
        }
    )
    try:
        consumer.subscribe([topic])
        deadline = time.monotonic() + 20
        message = None
        while time.monotonic() < deadline:
            candidate = consumer.poll(0.2)
            if candidate is not None and not candidate.error():
                message = candidate
                break
        assert message is not None and message.value() == b"close-probe"
        assert consumer.assignment()
        print("real-group-message-read", file=sys.stderr, flush=True)
        # All established broker sockets now stay open but drop requests/responses.
        proxy.blackhole.set()
        print("blackhole-enabled", file=sys.stderr, flush=True)
        started = time.monotonic()
        consumer.commit(
            offsets=[TopicPartition(topic, message.partition(), message.offset() + 1)],
            asynchronous=True,
        )
        async_commit_seconds = time.monotonic() - started
        print("async-commit-enqueued", file=sys.stderr, flush=True)
        started = time.monotonic()
        consumer.close()
        print("consumer-close-returned", file=sys.stderr, flush=True)
        elapsed = time.monotonic() - started
        consumer = None
        return {
            "real_group_joined_and_message_read": True,
            "fault": "established TCP sockets blackholed before async commit and close",
            "async_commit_enqueue_seconds": round(async_commit_seconds, 4),
            "blackholed_close_seconds": round(elapsed, 3),
            "configured_limits": CONFLUENT_REJECTED_BUDGET,
        }
    finally:
        if consumer is not None:
            consumer.close()
        proxy.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument(
        "--client", choices=("selected", "rejected"), default="selected"
    )
    parser.add_argument("--topic")
    parser.add_argument("--group")
    args = parser.parse_args()
    try:
        if args.worker:
            implementation = worker if args.client == "selected" else rejected_worker
            print(json.dumps(implementation(args.config, args.topic, args.group)))
            return
        from kafka import KafkaAdminClient, KafkaProducer
        from kafka.admin import NewTopic

        connection = kafka_connection(args.config)
        topic = "dlr-b0-close-" + uuid.uuid4().hex[:12]
        group = topic + "-group"
        admin = KafkaAdminClient(
            **connection, request_timeout_ms=10000, bootstrap_timeout_ms=3000
        )
        admin.create_topics(
            [NewTopic(topic, num_partitions=1, replication_factor=1)], timeout_ms=5000
        )
        try:
            producer = KafkaProducer(
                **connection, request_timeout_ms=5000, max_block_ms=5000
            )
            producer.send(topic, value=b"close-probe").get(timeout=6)
            producer.close(timeout=5)
            # Includes join/setup + blackhole; watchdog prevents a stuck probe blocking the run.
            result = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--worker",
                    "--client",
                    args.client,
                    "--config",
                    str(args.config),
                    "--topic",
                    topic,
                    "--group",
                    group,
                ],
                capture_output=True,
                text=True,
                timeout=35,
                check=True,
            )
            evidence = json.loads(result.stdout)
            assert evidence["blackholed_close_seconds"] < 5
            print(
                json.dumps(
                    {
                        "status": "passed",
                        **evidence,
                        "close_observation_limit_seconds": 5,
                        "worker_watchdog_seconds": 35,
                        "limits": "one controlled network fault, not universal hard cancellation",
                    },
                    indent=2,
                )
            )
        finally:
            admin.delete_topics([topic], timeout_ms=5000)
            admin.delete_groups([group])
            admin.close()

    except Exception as error:  # noqa: BLE001 - external clients may expose connection secrets
        if isinstance(error, subprocess.TimeoutExpired):
            details = error.stderr or b""
            if isinstance(details, bytes):
                details = details.decode("utf-8", errors="replace")
            (args.config.parent / "client-kafka-close-watchdog-stages.txt").write_text(
                details
            )
        print(
            json.dumps({"status": "failed", "exception_type": type(error).__name__}),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
