"""Physical inline parser ownership: actual children, bounded slots and recovery."""

import base64
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dlr.control.ai import attachments


def parse(source: bytes = b"original text") -> attachments.AttachmentResult:
    return attachments.process_attachment(
        "source.txt", "text/plain", base64.b64encode(source).decode(), False, 1024
    )


def test_real_worker_keeps_utf8_and_stable_failure_then_recovers() -> None:
    result = parse("原件\n0 false".encode())
    assert isinstance(result, attachments.ParsedText)
    assert result.text == "原件\n0 false" and not result.truncated
    with pytest.raises(attachments.AttachmentError, match="ai_attachment_parse_failed"):
        parse(b"binary\x00source")
    assert parse().text == "original text"


def test_worker_does_not_inherit_control_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLR_MASTER_KEY", "must-not-enter-parser")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-parser-either")
    original = attachments.subprocess.Popen
    environments = []

    def start(*args, **kwargs):
        environments.append(kwargs["env"])
        return original(*args, **kwargs)

    monkeypatch.setattr(attachments.subprocess, "Popen", start)
    assert parse().text == "original text"
    assert len(environments) == 1
    assert "DLR_MASTER_KEY" not in environments[0]
    assert "OPENAI_API_KEY" not in environments[0]
    assert environments[0]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_timeout_reaps_actual_process_before_capacity_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = []

    def start(*_args):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        processes.append(process)
        return process

    with monkeypatch.context() as patch:
        patch.setattr(attachments, "_start_parse_process", start)
        patch.setattr(attachments, "PARSE_TIMEOUT_SECONDS", 0.08)
        started = time.monotonic()
        with pytest.raises(attachments.AttachmentError, match="ai_attachment_parse_timeout"):
            parse()
        assert time.monotonic() - started < 3
    assert len(processes) == 1 and processes[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)
    # A fresh public parse proves the timeout returned its real capacity.
    assert parse().text == "original text"


def test_waiting_and_active_work_share_bounded_parser_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = []
    lock = threading.Lock()
    peak = 0

    def start(*_args):
        nonlocal peak
        with lock:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    'import time; time.sleep(.12); print(\'{"text":"ok","truncated":false}\')',
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            processes.append(process)
            peak = max(peak, sum(item.poll() is None for item in processes))
            return process

    monkeypatch.setattr(attachments, "_start_parse_process", start)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: parse(), range(4)))
    assert [item.text for item in results] == ["ok"] * 4
    assert peak == attachments.MAX_PARSE_PROCESSES
    assert all(process.poll() is not None for process in processes)
