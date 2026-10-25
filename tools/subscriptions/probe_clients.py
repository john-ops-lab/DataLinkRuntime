#!/usr/bin/env python3
"""B0 client API probes. Live modes mutate only their uniquely named test resources.

Connection/authentication settings are read from private files, never echoed.
These probes are not DLR delivery, fencing, failure recovery or business acceptance.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

KAFKA_BUDGET = {
    "fetch_max_bytes": 1048576,
    "max_partition_fetch_bytes": 1048576,
    "receive_message_max_bytes": 2097152,
    "max_poll_records": 16,
    "request_timeout_ms": 10000,
    "bootstrap_timeout_ms": 3000,
    "session_timeout_ms": 6000,
    "heartbeat_interval_ms": 2000,
    "max_poll_interval_ms": 30000,
    "fetch_max_wait_ms": 250,
}


def kafka_connection(config_file: Path) -> dict[str, Any]:
    connection = json.loads(config_file.read_text())
    if "bootstrap.servers" in connection:
        connection["bootstrap_servers"] = connection.pop("bootstrap.servers")
    connection.pop("log_level", None)
    return connection


def process_sample() -> dict[str, int]:
    """RSS, native thread count and open fds, without recording connection addresses."""
    if sys.platform == "darwin":
        pid = str(os.getpid())
        rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", pid], text=True))
        threads = (
            len(
                subprocess.check_output(["ps", "-M", "-p", pid], text=True).splitlines()
            )
            - 1
        )
        fds = len(os.listdir("/dev/fd"))
    elif sys.platform.startswith("linux"):
        status = dict(
            line.split(":", 1)
            for line in Path("/proc/self/status").read_text().splitlines()
        )
        rss = int(status["VmRSS"].split()[0])
        threads = int(status["Threads"])
        fds = len(os.listdir("/proc/self/fd"))
    else:
        raise RuntimeError("Resource probe requires macOS or Linux")
    return {"rss_kib": rss, "native_threads": threads, "open_fds": fds}


def kafka_resources(config_file: Path) -> dict[str, Any]:
    """Measure sixteen idle real clients; this is not a load or hard-RSS-bound proof."""
    from kafka import KafkaAdminClient, KafkaConsumer
    from kafka.admin import NewTopic

    connection = kafka_connection(config_file)
    admin = KafkaAdminClient(
        **connection, request_timeout_ms=10000, bootstrap_timeout_ms=3000
    )
    topic = "dlr-b0-budget-" + uuid.uuid4().hex[:12]
    admin.create_topics(
        [NewTopic(topic, num_partitions=1, replication_factor=1)], timeout_ms=5000
    )
    try:
        cold_baseline = process_sample()
        warmup = KafkaConsumer(**connection, **KAFKA_BUDGET, enable_auto_commit=False)
        assert warmup.partitions_for_topic(topic)
        warmup.close(autocommit=False, timeout_ms=3000)
        time.sleep(0.2)
        baseline = process_sample()
        consumers = []
        started = time.monotonic()
        try:
            for _ in range(16):
                consumer = KafkaConsumer(
                    **connection, **KAFKA_BUDGET, enable_auto_commit=False
                )
                consumers.append(consumer)
                assert consumer.partitions_for_topic(topic)
            peak = process_sample()
        finally:
            for consumer in consumers:
                consumer.close(autocommit=False, timeout_ms=3000)
        time.sleep(0.2)
        after = process_sample()
        assert after["native_threads"] == baseline["native_threads"]
        assert after["open_fds"] == baseline["open_fds"]
        return {
            "mode": "kafka-resources",
            "clients": len(consumers),
            "budget_per_client": KAFKA_BUDGET,
            "cold_baseline": cold_baseline,
            "baseline": baseline,
            "peak": peak,
            "after_close": after,
            "rss_delta_kib": peak["rss_kib"] - baseline["rss_kib"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "limits": "idle metadata connections only; no sustained-load RSS bound",
        }
    finally:
        try:
            admin.delete_topics([topic], timeout_ms=5000)
        finally:
            admin.close()


def bounded_raw_k8s(
    api_client: Any,
    path: str,
    query: dict[str, Any],
    *,
    cap: int,
    lines: bool = False,
    max_lines: int = 1,
    total_seconds: float = 5,
) -> list[Any]:
    """Use configured TLS/auth but bound successful AND error bodies before decoding.

    Watch.stream and generated REST ApiException handling can buffer an entire line/body.
    This B0 transport probe deliberately bypasses those decoders.
    """
    headers = {
        **api_client.default_headers,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    query_items = list(query.items())
    api_client.update_params_for_auth(headers, query_items, ["BearerToken"])
    response = api_client.rest_client.pool_manager.request(
        "GET",
        api_client.configuration.host.rstrip("/") + path,
        fields=query_items,
        headers=headers,
        preload_content=False,
        timeout=__import__("urllib3").Timeout(connect=3, read=3),
        retries=False,
    )
    buffer = bytearray()
    result = []
    deadline = time.monotonic() + total_seconds
    try:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("Compressed source response rejected before decoding")
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Total source read deadline reached")
            chunk = response.read1(amt=4096, decode_content=False)
            if not chunk:
                break
            buffer.extend(chunk)
            if lines:
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    if len(line) > cap:
                        raise ValueError(
                            "Source line limit reached before JSON decoding"
                        )
                    buffer = bytearray(remainder)
                    if line:
                        result.append(json.loads(line))
                        # Probe needs just one event; production processes/reclaims each line.
                        if len(result) >= max_lines:
                            return result
            if len(buffer) > cap:
                raise ValueError("Source body limit reached before JSON decoding")
        if response.status != 200:
            raise RuntimeError("Source HTTP error with bounded body")
        if buffer:
            result.append(json.loads(buffer))
        return result
    finally:
        response.close()
        response.release_conn()


def installed() -> dict[str, Any]:
    from kafka import KafkaAdminClient, KafkaConsumer
    from kubernetes import client, watch

    expected = {"kafka-python": "3.0.11", "kubernetes": "35.0.0", "urllib3": "2.8.0"}
    versions = {name: importlib.metadata.version(name) for name in expected}
    assert versions == expected, versions
    assert sys.version_info[:2] == (3, 13), "Use the declared Python 3.13 baseline"
    for name in ("assign", "pause", "resume", "commit", "close", "subscribe"):
        assert callable(getattr(KafkaConsumer, name))
    for name in ("describe_topics", "describe_cluster", "describe_groups"):
        assert callable(getattr(KafkaAdminClient, name))
    assert "timeout_ms" in inspect.signature(KafkaConsumer.close).parameters
    assert "resp.close()" in inspect.getsource(watch.Watch.stream)
    assert "resp.release_conn()" in inspect.getsource(watch.Watch.stream)
    for method in (
        client.CoreV1Api.list_namespaced_pod,
        client.AppsV1Api.list_namespaced_deployment,
    ):
        for parameter in (
            "limit",
            "_continue",
            "resource_version",
            "timeout_seconds",
            "_request_timeout",
            "allow_watch_bookmarks",
        ):
            assert parameter in method.__doc__, (method.__name__, parameter)
    return {
        "mode": "installed",
        "python": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "clients": versions,
        "api_surface": "passed",
        "live_source_contacted": False,
    }


def kafka(config_file: Path) -> dict[str, Any]:
    from confluent_kafka import Producer
    from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition
    from kafka.admin import NewTopic
    from kafka.consumer.subscription_state import ConsumerRebalanceListener
    from kafka.structs import OffsetAndMetadata

    connection = kafka_connection(config_file)
    name = "dlr-b0-" + uuid.uuid4().hex[:16]
    group = name + "-group"
    admin = KafkaAdminClient(
        **connection, request_timeout_ms=10000, bootstrap_timeout_ms=3000
    )
    consumer = None
    producer = None
    created = False
    try:
        admin.create_topics(
            [NewTopic(name, num_partitions=2, replication_factor=1)], timeout_ms=5000
        )
        created = True
        cluster = admin.describe_cluster()
        description = admin.describe_topics([name])[0]
        assert (
            cluster["cluster_id"] and uuid.UUID(str(description["topic_id"])).int != 0
        )
        producer = Producer(
            {
                "bootstrap.servers": connection["bootstrap_servers"],
                "message.timeout.ms": 5000,
                "log_level": 0,
            }
        )
        payloads = [b"\x00\xffbinary", None, b""]
        headers = [("same", b"a"), ("same", None), ("same", b"")]
        for value in payloads:
            producer.produce(name, partition=0, key=b"", value=value, headers=headers)
        assert producer.flush(6) == 0
        consumer = KafkaConsumer(
            **connection,
            **KAFKA_BUDGET,
            group_id=group,
            enable_auto_commit=False,
            auto_offset_reset="none",
            isolation_level="read_committed",
        )
        assignments = []

        class Listener(ConsumerRebalanceListener):
            def on_partitions_revoked(self, partitions):
                pass

            def on_partitions_assigned(self, partitions):
                assignments.append(list(partitions))
                for partition in partitions:
                    consumer.seek(partition, 0)

        consumer.subscribe([name], listener=Listener())
        messages = []
        deadline = time.monotonic() + 20
        while len(messages) < 3 and time.monotonic() < deadline:
            for batch in consumer.poll(timeout_ms=200, max_records=3).values():
                messages.extend(batch)
        assert len(messages) == 3 and assignments
        assert [m.value for m in messages] == payloads
        assert all(m.key == b"" and m.headers == headers for m in messages)
        partition = TopicPartition(name, 0)
        safe_next = messages[-1].offset + 1
        consumer.commit({partition: OffsetAndMetadata(safe_next, "")}, timeout_ms=3000)
        assert consumer.committed(partition, timeout_ms=3000) == safe_next
        consumer.pause(*consumer.assignment())
        producer.produce(name, partition=0, value=b"after-pause")
        assert producer.flush(6) == 0
        assert not consumer.poll(timeout_ms=1000)
        consumer.resume(*consumer.assignment())
        deadline = time.monotonic() + 10
        resumed = []
        while not resumed and time.monotonic() < deadline:
            for batch in consumer.poll(timeout_ms=200).values():
                resumed.extend(batch)
        assert resumed and resumed[0].value == b"after-pause"
        started = time.monotonic()
        consumer.close(autocommit=False, timeout_ms=3000)
        close_seconds = time.monotonic() - started
        consumer = None
        observer = KafkaConsumer(
            **connection, **KAFKA_BUDGET, group_id=group, enable_auto_commit=False
        )
        try:
            assert observer.committed(partition, timeout_ms=3000) == safe_next
            observer.assign([partition])
            observer.seek(partition, safe_next)
            assert observer.assignment() == {partition}
        finally:
            observer.close(autocommit=False, timeout_ms=3000)
        return {
            "mode": "kafka",
            "client": importlib.metadata.version("kafka-python"),
            "cluster_id_present": True,
            "topic_id_present": True,
            "partition_count": len(description["partitions"]),
            "assignment_callbacks": len(assignments),
            "explicit_assign": True,
            "binary_null_headers": "passed",
            "manual_safe_next_commit": "passed",
            "pause_resume": "passed",
            "close_preserves_uncommitted_offset": "passed",
            "close_seconds": round(close_seconds, 3),
            "budget_per_client": KAFKA_BUDGET,
            "limits": "single live broker API probe; not DLR or fault acceptance",
        }
    finally:
        if consumer is not None:
            consumer.close(autocommit=False, timeout_ms=3000)
        if producer is not None:
            producer.flush(5)
        if created:
            admin.delete_topics([name], timeout_ms=5000)
            admin.delete_groups([group])
        admin.close()


def kubernetes(kubeconfig: Path, context: str | None) -> dict[str, Any]:
    from kubernetes import client, config, watch

    configuration = client.Configuration()
    config.load_kube_config(
        config_file=str(kubeconfig), context=context, client_configuration=configuration
    )
    configuration.retries = 0
    configuration.connection_pool_maxsize = 2
    api_client = client.ApiClient(configuration)
    core = client.CoreV1Api(api_client)
    apps = client.AppsV1Api(api_client)
    namespace = "dlr-b0-" + uuid.uuid4().hex[:12]
    timeout = {"_request_timeout": (3, 5)}
    created = False
    try:
        server = client.VersionApi(api_client).get_code(**timeout).git_version
        core.create_namespace({"metadata": {"name": namespace}}, **timeout)
        created = True
        labels = {"dlr-probe": namespace}
        for index in range(3):
            metadata = {"name": f"item-{index}", "labels": labels}
            core.create_namespaced_pod(
                namespace,
                {
                    "metadata": metadata,
                    "spec": {
                        "containers": [
                            {
                                "name": "placeholder",
                                "image": "registry.k8s.io/pause:3.10",
                            }
                        ]
                    },
                },
                **timeout,
            )
            apps.create_namespaced_deployment(
                namespace,
                {
                    "metadata": metadata,
                    "spec": {
                        "replicas": 0,
                        "selector": {"matchLabels": {"probe": str(index)}},
                        "template": {
                            "metadata": {"labels": {"probe": str(index)}},
                            "spec": {
                                "containers": [
                                    {
                                        "name": "placeholder",
                                        "image": "registry.k8s.io/pause:3.10",
                                    }
                                ]
                            },
                        },
                    },
                },
                **timeout,
            )

        result = {}
        for kind, list_method, patch_method in (
            ("Pod", core.list_namespaced_pod, core.patch_namespaced_pod),
            (
                "Deployment",
                apps.list_namespaced_deployment,
                apps.patch_namespaced_deployment,
            ),
        ):
            cursor = None
            rv = None
            uids = set()
            pages = 0
            while True:
                page = list_method(
                    namespace,
                    label_selector=f"dlr-probe={namespace}",
                    limit=1,
                    _continue=cursor,
                    **timeout,
                )
                assert len(page.items) <= 1
                pages += 1
                assert rv is None or rv == page.metadata.resource_version
                rv = page.metadata.resource_version
                uids.update(obj.metadata.uid for obj in page.items)
                cursor = page.metadata._continue
                if not cursor:
                    break
            assert pages == 3 and len(uids) == 3 and isinstance(rv, str)
            # Force a MODIFIED after LIST, then watch from the opaque collection RV.
            patch_method(
                "item-0",
                namespace,
                {"metadata": {"annotations": {"probe": uuid.uuid4().hex}}},
                **timeout,
            )
            watcher = watch.Watch()
            stream = watcher.stream(
                list_method,
                namespace,
                label_selector=f"dlr-probe={namespace}",
                resource_version=rv,
                allow_watch_bookmarks=True,
                timeout_seconds=2,
                _request_timeout=(3, 4),
            )
            found = False
            try:
                for event in stream:
                    if (
                        event["type"] == "MODIFIED"
                        and event["object"].metadata.name == "item-0"
                    ):
                        found = True
                        watcher.stop()
                        break
            finally:
                # Generator finally closes/releases the actual urllib3 response.
                stream.close()
            assert found

            # stop() is cooperative, not an interrupt of blocked socket reads.
            watcher = watch.Watch()
            idle_errors = []

            def idle_watch(current_watcher, current_list, current_errors):
                try:
                    for _ in current_watcher.stream(
                        current_list,
                        namespace,
                        label_selector=f"never-matches={namespace}",
                        timeout_seconds=2,
                        _request_timeout=(3, 4),
                    ):
                        pass
                except Exception as error:  # noqa: BLE001 - redact external client failures
                    current_errors.append(type(error).__name__)

            thread = threading.Thread(
                target=idle_watch,
                args=(watcher, list_method, idle_errors),
                name="dlr-b0-idle-watch",
            )
            thread.start()
            time.sleep(0.2)
            started = time.monotonic()
            watcher.stop()
            thread.join(timeout=5)
            elapsed = time.monotonic() - started
            assert not thread.is_alive() and not idle_errors, idle_errors
            path = (
                f"/api/v1/namespaces/{namespace}/pods"
                if kind == "Pod"
                else f"/apis/apps/v1/namespaces/{namespace}/deployments"
            )
            query = {"labelSelector": f"dlr-probe={namespace}", "limit": 1}
            raw_page = bounded_raw_k8s(api_client, path, query, cap=1024 * 1024)[0]
            assert len(raw_page["items"]) == 1
            raw_rv = raw_page["metadata"]["resourceVersion"]
            patch_method(
                "item-0",
                namespace,
                {"metadata": {"annotations": {"bounded": uuid.uuid4().hex}}},
                **timeout,
            )
            raw_events = bounded_raw_k8s(
                api_client,
                path,
                {
                    "labelSelector": f"dlr-probe={namespace}",
                    "watch": "true",
                    "resourceVersion": raw_rv,
                    "timeoutSeconds": 2,
                },
                cap=1024 * 1024,
                lines=True,
            )
            assert raw_events and raw_events[0]["type"] == "MODIFIED"
            before_large_rv = bounded_raw_k8s(api_client, path, query, cap=1024 * 1024)[
                0
            ]["metadata"]["resourceVersion"]
            patch_method(
                "item-0",
                namespace,
                {"metadata": {"annotations": {"bounded": "x" * 16384}}},
                **timeout,
            )
            oversized_rejected = False
            try:
                bounded_raw_k8s(
                    api_client,
                    path,
                    {"fieldSelector": "metadata.name=item-0", "limit": 1},
                    cap=8192,
                )
            except ValueError:
                oversized_rejected = True
            assert oversized_rejected
            oversized_watch_rejected = False
            try:
                bounded_raw_k8s(
                    api_client,
                    path,
                    {
                        "watch": "true",
                        "resourceVersion": before_large_rv,
                        "timeoutSeconds": 2,
                        "fieldSelector": "metadata.name=item-0",
                    },
                    cap=8192,
                    lines=True,
                    max_lines=16,
                )
            except ValueError:
                oversized_watch_rejected = True
            assert oversized_watch_rejected
            result[kind] = {
                "pages": pages,
                "objects": len(uids),
                "collection_rv_stable": True,
                "watch_modified": True,
                "bounded_idle_stop_seconds": round(elapsed, 3),
                "bounded_raw_list_watch": True,
                "raw_chunk_bytes": 4096,
                "raw_test_limit_bytes": 8192,
                "oversized_source_rejected_before_json": oversized_rejected,
                "oversized_watch_line_rejected_before_json": oversized_watch_rejected,
            }
        return {
            "mode": "kubernetes",
            "client": importlib.metadata.version("kubernetes"),
            "server": server,
            "resources": result,
            "close": "stream.close + ApiClient.close + RESTClient.pool_manager.clear",
            "limits": "Pod/Deployment API only; workload execution not tested",
        }
    finally:
        try:
            if created:
                core.delete_namespace(namespace, **timeout)
        finally:
            api_client.close()
            api_client.rest_client.pool_manager.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("installed", "kafka", "kafka-resources", "kubernetes")
    )
    parser.add_argument("--config", type=Path, help="private Kafka connection JSON")
    parser.add_argument("--kubeconfig", type=Path, help="private kubeconfig")
    parser.add_argument("--context")
    args = parser.parse_args()
    try:
        if args.mode == "installed":
            result = installed()
        elif args.mode in ("kafka", "kafka-resources"):
            if args.config is None:
                parser.error("kafka requires --config")
            result = (
                kafka(args.config)
                if args.mode == "kafka"
                else kafka_resources(args.config)
            )
        else:
            if args.kubeconfig is None:
                parser.error("kubernetes requires --kubeconfig")
            result = kubernetes(args.kubeconfig, args.context)
        print(json.dumps({"status": "passed", **result}, ensure_ascii=False, indent=2))
    except Exception as error:  # noqa: BLE001 - redact external client failures
        # Exception messages may include broker URLs, TLS identities or credential paths.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "mode": args.mode,
                    "exception_type": type(error).__name__,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
