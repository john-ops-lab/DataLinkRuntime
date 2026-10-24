#!/usr/bin/env python3
"""PLAINTEXT fixture proxy and real owner/PG OffsetCommit fault probe.

The proxy only supports the isolated single-broker fixture: it rewrites broker
ports in Metadata/FindCoordinator frames and selectively drops/holds API 8.
It is not a general Kafka proxy. The CLI runs the dedicated opt-in pytest cases
against a task-owned PostgreSQL database and random Kafka topics/groups.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Literal

Fault = Literal["drop_request", "drop_response", "late_request"]


class OffsetCommitFaultProxy:
    def __init__(self, host: str, port: int, fault: Fault) -> None:
        self.upstream = (host, port)
        self.fault = fault
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.armed = threading.Event()
        self.request_seen = threading.Event()
        self.response_seen = threading.Event()
        self.allow_request = threading.Event()
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    def _accept(self) -> None:
        while not self.stopped.is_set():
            try:
                incoming, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                outgoing = socket.create_connection(self.upstream, timeout=3)
            except OSError:
                incoming.close()
                continue
            correlations: dict[int, tuple[int, bool]] = {}
            correlation_lock = threading.Lock()
            with self._lock:
                self._sockets.extend((incoming, outgoing))
            for source, target, is_response in (
                (incoming, outgoing, False),
                (outgoing, incoming, True),
            ):
                thread = threading.Thread(
                    target=self._relay,
                    args=(source, target, is_response, correlations, correlation_lock),
                    daemon=True,
                )
                with self._lock:
                    self._threads.append(thread)
                thread.start()

    def _relay(
        self,
        source: socket.socket,
        target: socket.socket,
        is_response: bool,
        correlations: dict[int, tuple[int, bool]],
        correlation_lock: threading.Lock,
    ) -> None:
        buffered = bytearray()
        source.settimeout(0.1)
        try:
            while not self.stopped.is_set():
                try:
                    chunk = source.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    return
                buffered.extend(chunk)
                while len(buffered) >= 4:
                    size = struct.unpack_from(">i", buffered)[0]
                    if size < 4 or size > 2 * 1024**2:
                        return
                    if len(buffered) < size + 4:
                        break
                    frame = bytes(buffered[4 : size + 4])
                    del buffered[: size + 4]
                    if is_response:
                        correlation = struct.unpack_from(">i", frame)[0]
                        with correlation_lock:
                            api, armed = correlations.pop(correlation, (-1, False))
                        if api in (3, 10):
                            frame = frame.replace(
                                struct.pack(">i", self.upstream[1]),
                                struct.pack(">i", self.port),
                            )
                        if api == 8 and armed:
                            self.response_seen.set()
                            if self.fault == "drop_response":
                                continue
                    else:
                        api = struct.unpack_from(">h", frame)[0]
                        correlation = struct.unpack_from(">i", frame, 4)[0]
                        armed = api == 8 and self.armed.is_set()
                        with correlation_lock:
                            correlations[correlation] = (api, armed)
                        if armed:
                            self.request_seen.set()
                            if self.fault == "drop_request":
                                continue
                            if self.fault == "late_request" and not self.allow_request.wait(10):
                                return
                    target.sendall(struct.pack(">i", len(frame)) + frame)
        except OSError:
            return

    def close(self) -> None:
        self.stopped.set()
        self.allow_request.set()
        self.listener.close()
        self._acceptor.join(timeout=1)
        with self._lock:
            connections = list(self._sockets)
            threads = list(self._threads)
        for connection in connections:
            connection.close()
        for thread in threads:
            thread.join(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Private PLAINTEXT Kafka JSON")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--database", required=True, help="Task-owned PostgreSQL test database")
    args = parser.parse_args()
    if not re.fullmatch(r"dlr_issue219_[a-z0-9_]{1,40}", args.database):
        parser.error("Database must be a task-owned dlr_issue219_* test database")
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "DLR_KAFKA_TEST_CONFIG": str(args.config.resolve()),
        "DLR_KAFKA_FAULT_EVIDENCE_DIR": str(args.evidence_dir.resolve()),
        "DLR_TEST_DATABASE": args.database,
    }
    with (args.evidence_dir / "api8-pytest.log").open("w") as log:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_issue219_runtime.py",
                "-k",
                "live_offset_commit_fault_windows",
            ],
            cwd=root / "backend",
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    print(json.dumps({"exit_code": completed.returncode}))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
