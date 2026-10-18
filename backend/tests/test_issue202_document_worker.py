"""Bounded source metadata, lazy PDF provenance, and process-supervisor contracts."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook
from pypdf import PdfReader, PdfWriter

from test_xls_fixture import build_xls

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "ai-document-converter"


def _module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"issue202_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


processor = _module("processor")


def _xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.merge_cells("A1:C1")
    sheet["A1"] = "Summary"
    sheet["B2"] = 10
    sheet["B2"].number_format = "#,##0.00"
    sheet["C2"] = 0.125
    sheet["C2"].number_format = "0.0%"
    sheet["B3"] = "=SUM(B2:B2)"
    sheet.print_area = "A1:C5"
    hidden = workbook.create_sheet("Hidden")
    hidden.sheet_state = "hidden"
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_source_xlsx_cells_never_substitute_recalculated_values() -> None:
    source = _xlsx()
    sheets, data, warnings = processor.workbook(source, ".xlsx", "Sales", "B2:C3")
    assert sheets[1]["hidden"] is True
    assert sheets[0]["merged_ranges"] == ["A1:C1"]
    assert sheets[0]["print_area"] == "'Sales'!$A$1:$C$5"
    rows = {cell["coordinate"]: cell for cell in data["cells"]}
    assert rows["B2"]["raw_value"] == 10
    assert rows["C2"]["number_format"] == "0.0%"
    assert rows["B3"]["formula"] == "=SUM(B2:B2)"
    assert rows["B3"]["cached_value"] is None
    assert rows["B3"]["cache_status"] == "missing"
    assert warnings == []
    assert data["merged_ranges"] == []


@pytest.mark.parametrize("range_a1", ["A1:A201", "A0", "B2:A1", "A1:ZZZ2", "../A1"])
def test_range_is_bounded_before_iteration(range_a1: str) -> None:
    with pytest.raises(processor.ConversionError):
        processor.workbook(_xlsx(), ".xlsx", "Sales", range_a1)


def test_true_biff_uses_stored_values_and_marks_formula_text_unknown() -> None:
    source = build_xls()
    assert source.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    sheets, data, warnings = processor.workbook(source, ".xls")
    name = sheets[0]["name"]
    _, data, _ = processor.workbook(source, ".xls", name, "A1:B2")
    assert len(data["cells"]) == 4
    assert all(x["formula_status"] == "unavailable_xlrd" for x in data["cells"])
    assert "xls_formula_text_unavailable" in warnings


def test_outline_sheet_mapping_uses_real_page_destinations() -> None:
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=595, height=842)
    writer.add_outline_item("Summary", 0)
    writer.add_outline_item("Details", 1)
    stream = io.BytesIO()
    writer.write(stream)
    sheets = [{"name": "Summary"}, {"name": "Details"}]
    _, pages = processor.pdf_catalog(stream.getvalue(), sheets)
    assert [x["page"] for x in pages] == [1, 2, 3, 4]
    assert [x["source_sheet"] for x in pages] == ["Summary", "Details", "Details", "Details"]
    assert sheets[1]["rendered_page_start"] == 2
    assert sheets[1]["rendered_page_end"] == 4


def test_native_single_page_export_preserves_original_ordinal() -> None:
    writer = PdfWriter()
    for width in [100, 200, 300]:
        writer.add_blank_page(width=width, height=400)
    stream = io.BytesIO()
    writer.write(stream)
    result = processor.dispatch(
        {
            "operation": "read-page",
            "filename": "original.pdf",
            "data_base64": base64.b64encode(stream.getvalue()).decode(),
            "page": 3,
            "view": "pdf",
        }
    )
    asset = result["assets"][0]
    extracted = PdfReader(io.BytesIO(base64.b64decode(asset["data_base64"])))
    assert len(extracted.pages) == 1
    assert extracted.pages[0].mediabox.width == 300
    assert result["data"]["page"] == 3
    assert result["page_count"] == 3
    assert asset["page_count"] == 1


def test_cancel_before_registration_remains_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    # server's processor import intentionally has no document-library side effects.
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "office")
    supervisor = module.Supervisor()
    assert supervisor.cancel("cancel-first")["terminated"] is True
    with pytest.raises(module.ConversionError):
        supervisor.run(
            {
                "job_id": "cancel-first",
                "filename": "source.pdf",
                "data_base64": base64.b64encode(b"source").decode(),
            },
            "prepare",
        )
    assert supervisor.jobs == {}


@pytest.mark.parametrize("launcher_exits", [False, True])
def test_cancel_reaps_entire_group_after_launcher_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launcher_exits: bool
) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    marker = tmp_path / "descendant.pid"
    # A real descendant ignores TERM and keeps running after its launcher dies.
    script = """
import os, signal, sys, time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(sys.argv[1], 'w') as output:
        output.write(str(os.getpid()))
    while True:
        time.sleep(.01)
if sys.argv[2] == 'exit':
    sys.exit(0)
while True:
    time.sleep(.01)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(marker), "exit" if launcher_exits else "wait"],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        descendant = int(marker.read_text())
        os.kill(descendant, 0)
        if launcher_exits:
            assert process.wait(timeout=3) == 0
        started = time.monotonic()
        module.terminate(process)
        assert time.monotonic() - started < 3
        assert process.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
        with pytest.raises(ProcessLookupError):
            os.kill(descendant, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def test_cancel_never_acknowledges_unconfirmed_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import itertools
    import subprocess
    import sys

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "office")
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=3)
    monkeypatch.setattr(module, "_group_exists", lambda group: True)
    clock = itertools.count()
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    supervisor = module.Supervisor()
    job = module.Job()
    job.process = process
    job.done.set()
    supervisor.jobs["unconfirmed"] = job
    with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
        supervisor.cancel("unconfirmed")


def test_supervisor_quarantines_failed_reaping_and_cancel_retry_restores_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import os
    import subprocess
    import sys
    import threading

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "office")
    supervisor = module.Supervisor()
    real_popen = subprocess.Popen
    real_terminate = module.terminate
    children = []
    directories = []
    failed_reaping = threading.Event()
    failed_reaping.set()

    def spawn(command, **kwargs):
        request = json.loads(Path(command[-2]).read_text())
        script = (
            "import time; time.sleep(60)"
            if request["job_id"] == "quarantined"
            else "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{\"ok\":true}')"
        )
        process = real_popen([sys.executable, "-c", script, command[-1]], **kwargs)
        children.append(process)
        directories.append(Path(command[-2]).parent)
        return process

    def reap(process, *, deadline=None):
        if process is not None and failed_reaping.is_set():
            raise module.ConversionError("ai_attachment_cancel_failed")
        return real_terminate(process, deadline=deadline)

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module, "terminate", reap)
    request = {
        "job_id": "quarantined",
        "filename": "source.pdf",
        "data_base64": base64.b64encode(b"source").decode(),
        "timeout_seconds": 0.05,
    }
    try:
        with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
            supervisor.run(request, "prepare")
        job = supervisor.jobs["quarantined"]
        assert job.run_finished and not job.done.is_set()
        assert job.owns_slot and not job.group_reaped
        assert children[0].poll() is None
        os.killpg(children[0].pid, 0)
        assert not directories[0].exists()
        assert not supervisor.slots.acquire(blocking=False)
        with pytest.raises(module.ConversionError, match="ai_attachment_converter_busy"):
            supervisor.run({**request, "job_id": "too-early"}, "prepare")
        with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
            supervisor.cancel("quarantined")
        assert supervisor.jobs["quarantined"] is job and job.owns_slot

        failed_reaping.clear()
        # Control may have lost the old locator: a new request must recover it.
        assert supervisor.run(
            {**request, "job_id": "healthy-next", "timeout_seconds": 3}, "prepare"
        ) == {"ok": True}
        assert children[0].poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(children[0].pid, 0)
        assert supervisor.jobs == {} and job.done.is_set() and not job.owns_slot
        # Both a duplicate cancellation and duplicate finalization must be harmless.
        assert supervisor.cancel("quarantined")["terminated"] is True
        with supervisor.lock:
            supervisor._finalize_locked("quarantined", job)
        assert supervisor.slots.acquire(blocking=False)
        assert not supervisor.slots.acquire(blocking=False)
        supervisor.slots.release()
        assert supervisor.jobs == {} and all(not path.exists() for path in directories)
    finally:
        failed_reaping.clear()
        for process in children:
            real_terminate(process)


@pytest.mark.parametrize("phase", ["office_call", "temporary_cleanup"])
def test_supervisor_cancel_waits_for_run_cleanup_before_releasing_capacity(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import subprocess
    import sys
    import tempfile
    import threading

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "docling")
    supervisor = module.Supervisor()
    cleanup_started = threading.Event()
    cleanup_continue = threading.Event()
    cancel_returned = threading.Event()
    real_directory = tempfile.TemporaryDirectory
    real_popen = subprocess.Popen
    children = []
    outcomes = []

    class DelayedCleanup(real_directory):
        def __exit__(self, *args):
            cleanup_started.set()
            assert cleanup_continue.wait(3)
            return super().__exit__(*args)

    def office(path, payload, timeout, method="POST"):
        assert phase == "office_call", "A PDF job must never depend on Office availability"
        if method == "DELETE":
            return {"terminated": True}
        cleanup_started.set()
        assert cleanup_continue.wait(3)
        return {"rendered_pdf": base64.b64encode(b"%PDF-source").decode()}

    def spawn(command, **kwargs):
        script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{\"ok\":true}')"
        process = real_popen([sys.executable, "-c", script, command[-1]], **kwargs)
        children.append(process)
        return process

    if phase == "temporary_cleanup":
        monkeypatch.setattr(module.tempfile, "TemporaryDirectory", DelayedCleanup)
    monkeypatch.setattr(module, "http_office", office)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    request = {
        "job_id": "active-run",
        "filename": "source.xls" if phase == "office_call" else "source.pdf",
        "data_base64": base64.b64encode(b"source").decode(),
    }

    def run():
        try:
            outcomes.append(supervisor.run(request, "prepare"))
        except module.ConversionError as error:
            outcomes.append(error.code)

    def cancel():
        try:
            outcomes.append(supervisor.cancel("active-run"))
        finally:
            cancel_returned.set()

    run_thread = threading.Thread(target=run)
    cancel_thread = threading.Thread(target=cancel)
    run_thread.start()
    try:
        assert cleanup_started.wait(3)
        job = supervisor.jobs["active-run"]
        cancel_thread.start()
        assert job.cancelled.wait(1)
        assert not job.run_finished and not job.done.is_set() and job.owns_slot
        assert not supervisor.slots.acquire(blocking=False)
        assert not cancel_returned.wait(0.05)
        cleanup_continue.set()
        run_thread.join(timeout=3)
        cancel_thread.join(timeout=3)
        assert not run_thread.is_alive() and not cancel_thread.is_alive()
        assert {"terminated": True, "cancelled": True} in outcomes
        assert supervisor.jobs == {} and job.done.is_set() and not job.owns_slot
        assert supervisor.slots.acquire(blocking=False)
        assert not supervisor.slots.acquire(blocking=False)
        supervisor.slots.release()
    finally:
        cleanup_continue.set()
        run_thread.join(timeout=3)
        if cancel_thread.ident is not None:
            cancel_thread.join(timeout=3)
        for process in children:
            module.terminate(process)


@pytest.mark.parametrize("failed_delete", ["unreachable", "unconfirmed"])
def test_office_unknown_termination_is_quarantined_and_next_request_recovers(
    monkeypatch: pytest.MonkeyPatch, failed_delete: str
) -> None:
    import os
    import subprocess
    import sys
    import threading
    import urllib.error

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "docling")
    supervisor = module.Supervisor()
    real_popen = subprocess.Popen
    office_children = []
    local_children = []
    fault = threading.Event()
    fault.set()
    recovery_started = threading.Event()
    recovery_continue = threading.Event()
    next_results = []
    delete_calls = []
    next_thread = None

    def office(path, payload, timeout, method="POST"):
        if method == "DELETE":
            delete_calls.append(path)
            if fault.is_set():
                if failed_delete == "unconfirmed":
                    return {"terminated": False}
                raise urllib.error.URLError("Office cancellation disconnected")
            recovery_started.set()
            assert recovery_continue.wait(3)
            for child in office_children:
                module.terminate(child)
            return {"terminated": True}
        child = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)" if fault.is_set() else "pass"],
            start_new_session=True,
        )
        office_children.append(child)
        if fault.is_set():
            raise urllib.error.URLError("Office accepted POST but response disconnected")
        child.wait(timeout=3)
        return {"rendered_pdf": base64.b64encode(b"%PDF-source").decode()}

    def spawn(command, **kwargs):
        script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{\"ok\":true}')"
        child = real_popen([sys.executable, "-c", script, command[-1]], **kwargs)
        local_children.append(child)
        return child

    monkeypatch.setattr(module, "http_office", office)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    request = {
        "job_id": "office-unknown",
        "filename": "source.xls",
        "data_base64": base64.b64encode(b"source").decode(),
        "timeout_seconds": 3,
    }
    try:
        with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
            supervisor.run(request, "prepare")
        job = supervisor.jobs["office-unknown"]
        assert job.run_finished and job.group_reaped and job.owns_slot
        assert job.office_started and not job.office_reaped and not job.done.is_set()
        assert office_children[0].poll() is None
        os.killpg(office_children[0].pid, 0)
        with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
            supervisor.cancel("office-unknown")
        # No completed receipt exists: a missing job cannot excuse an Office failure.
        with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
            supervisor.cancel("unknown-job")
        with pytest.raises(module.ConversionError, match="ai_attachment_converter_busy"):
            supervisor.run({**request, "job_id": "still-too-early"}, "prepare")
        assert supervisor.jobs["office-unknown"] is job and job.owns_slot

        fault.clear()
        # No manual DELETE of the old internal locator is needed for recovery.
        deletes_before_recovery = len(delete_calls)
        next_thread = threading.Thread(
            target=lambda: next_results.append(
                supervisor.run({**request, "job_id": "healthy-office"}, "prepare")
            )
        )
        next_thread.start()
        assert recovery_started.wait(1)
        with pytest.raises(module.ConversionError, match="ai_attachment_converter_busy"):
            supervisor.run({**request, "job_id": "contender"}, "prepare")
        assert len(delete_calls) == deletes_before_recovery + 1
        recovery_continue.set()
        next_thread.join(timeout=3)
        assert not next_thread.is_alive() and next_results == [{"ok": True}]
        assert office_children[0].poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(office_children[0].pid, 0)
        assert supervisor.jobs == {} and job.office_reaped and job.done.is_set()
        assert not job.owns_slot and not job.recovering
        assert supervisor.cancel("office-unknown")["terminated"] is True
        with supervisor.lock:
            supervisor._finalize_locked("office-unknown", job)
        assert supervisor.slots.acquire(blocking=False)
        assert not supervisor.slots.acquire(blocking=False)
        supervisor.slots.release()
    finally:
        fault.clear()
        recovery_continue.set()
        if next_thread is not None:
            next_thread.join(timeout=3)
        for child in office_children + local_children:
            module.terminate(child)


@pytest.mark.parametrize("operation", ["prepare", "read-sheet"])
def test_docling_jobs_without_office_remain_available_when_office_is_down(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import subprocess
    import sys
    import urllib.error

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "docling")
    supervisor = module.Supervisor()
    real_popen = subprocess.Popen
    office_calls = []
    children = []

    def office(*args, **kwargs):
        office_calls.append(args)
        raise urllib.error.URLError("Office is down")

    def spawn(command, **kwargs):
        script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{\"ok\":true}')"
        child = real_popen([sys.executable, "-c", script, command[-1]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module, "http_office", office)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    request = {
        "job_id": "no-office",
        "filename": "source.pdf" if operation == "prepare" else "source.xls",
        "data_base64": base64.b64encode(b"source").decode(),
    }
    try:
        assert supervisor.run(request, operation) == {"ok": True}
        assert supervisor.cancel("no-office")["terminated"] is True
        assert supervisor.cancel("no-office")["terminated"] is True
        assert office_calls == [] and supervisor.jobs == {}
    finally:
        for child in children:
            module.terminate(child)


def test_quarantine_recovery_consumes_only_new_request_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "docling")
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    supervisor = module.Supervisor()
    assert supervisor.slots.acquire(blocking=False)
    job = module.Job()
    job.run_finished = job.group_reaped = job.office_started = True
    supervisor.jobs["quarantined"] = job
    timeouts = []

    def office(path, payload, timeout, method="POST"):
        assert method == "DELETE"
        timeouts.append(timeout)
        now[0] += timeout
        raise urllib.error.URLError("Office timed out")

    monkeypatch.setattr(module, "http_office", office)
    with pytest.raises(module.ConversionError, match="ai_attachment_parse_timeout"):
        supervisor.run(
            {
                "job_id": "new-request",
                "filename": "source.pdf",
                "data_base64": base64.b64encode(b"source").decode(),
                "timeout_seconds": 0.05,
            },
            "prepare",
        )
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 0.05
    assert supervisor.jobs == {"quarantined": job}
    assert job.owns_slot and not job.office_reaped and not job.recovering
    assert not supervisor.slots.acquire(blocking=False)


def test_stopped_receipts_are_bounded_and_expired_receipts_require_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "docling")
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    supervisor = module.Supervisor()
    with supervisor.lock:
        for index in range(600):
            supervisor._remember_stopped_locked(f"completed-{index}")
    assert len(supervisor.stopped) == 512
    assert "completed-0" not in supervisor.stopped
    assert supervisor.cancel("completed-599")["terminated"] is True
    now[0] += 121

    def office(*args, **kwargs):
        raise urllib.error.URLError("Office is unavailable after receipt expired")

    monkeypatch.setattr(module, "http_office", office)
    with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
        supervisor.cancel("completed-599")


def test_rejected_registration_does_not_turn_unknown_office_work_into_stopped_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    monkeypatch.syspath_prepend(str(TOOLS))
    module = _module("server")
    monkeypatch.setattr(module, "ROLE", "docling")
    supervisor = module.Supervisor()

    def office(*args, **kwargs):
        raise urllib.error.URLError("Office termination is unknown")

    monkeypatch.setattr(module, "http_office", office)
    with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
        supervisor.cancel("unknown-office")
    with pytest.raises(module.ConversionError, match="ai_attachment_cancelled"):
        supervisor.run(
            {
                "job_id": "unknown-office",
                "filename": "source.pdf",
                "data_base64": base64.b64encode(b"source").decode(),
            },
            "prepare",
        )
    assert "unknown-office" not in supervisor.stopped
    with pytest.raises(module.ConversionError, match="ai_attachment_cancel_failed"):
        supervisor.cancel("unknown-office")
    assert supervisor.slots.acquire(blocking=False)
    assert not supervisor.slots.acquire(blocking=False)
    supervisor.slots.release()


@pytest.mark.parametrize(
    "pixels,size,error",
    [
        (16_000_000, 10 * 1024 * 1024, None),
        (16_000_001, 1, "ai_attachment_image_too_large"),
        (1, 10 * 1024 * 1024 + 1, "ai_attachment_too_large"),
    ],
)
def test_generated_media_obeys_reader_pixel_and_byte_limits(pixels, size, error) -> None:
    class EncodedImage:
        width = pixels
        height = 1

        def save(self, stream, *, format):
            assert format == "PNG"
            stream.write(b"x" * size)

    assert processor.MAX_PIXELS == 16_000_000
    assert processor.MAX_MEDIA == 10 * 1024 * 1024
    if error:
        with pytest.raises(processor.ConversionError, match=error):
            processor.png_asset(EncodedImage())
    else:
        asset = processor.png_asset(EncodedImage())
        assert asset["width"] * asset["height"] == processor.MAX_PIXELS
        assert asset["size_bytes"] == processor.MAX_MEDIA


def test_text_contract_matches_all_existing_text_and_code_mime_extensions() -> None:
    from dlr.control.ai import attachments

    text_types = {
        content_type: extensions
        for content_type, extensions in attachments.MIME_EXTENSIONS.items()
        if content_type
        not in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "application/pdf",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    }
    assert text_types == processor.TEXT_MIME_EXTENSIONS
    for content_type, extensions in text_types.items():
        for extension in extensions:
            source = '原件\r\nprint("do not execute")'.encode()
            result = processor.dispatch(
                dict(
                    operation="prepare",
                    filename=f"source.{extension}",
                    content_type=content_type,
                    data_base64=base64.b64encode(source).decode(),
                )
            )
            assert result["page_count"] == 1
            assert result["data"]["text"] == source.decode()
            assert result["data"]["source_origin"] == "original_text"
            assert result["assets"] == []


def test_text_directory_can_read_exact_last_chunk_and_preserves_source_hash() -> None:
    import hashlib

    text = "中文\r\n" * 40000 + "FINAL_SOURCE_SENTINEL"
    source = text.encode()
    request = dict(
        filename="requirements.md",
        content_type="text/markdown",
        data_base64=base64.b64encode(source).decode(),
    )
    prepared = processor.dispatch(dict(request, operation="prepare"))
    count = prepared["page_count"]
    assert count > 2
    assert [row["page"] for row in prepared["pages"]] == list(range(1, count + 1))
    assert prepared["data"]["text"] == text[: 64 * 1024]
    recovered = []
    for page in range(1, count + 1):
        result = processor.dispatch(dict(request, operation="read-page", page=page, view="both"))
        assert result["source_sha256"] == hashlib.sha256(source).hexdigest()
        assert len(result["data"]["text"]) <= 64 * 1024
        assert result["data"]["char_start"] == (page - 1) * 64 * 1024
        assert result["assets"] == []
        recovered.append(result["data"]["text"])
    assert "".join(recovered) == text
    assert recovered[-1].endswith("FINAL_SOURCE_SENTINEL")


@pytest.mark.parametrize(
    "source,view,error",
    [
        (b"", "text", "ai_attachment_no_text"),
        (b" \r\n", "text", "ai_attachment_no_text"),
        (b"\xff", "text", "ai_attachment_parse_failed"),
        (b"a\x00b", "text", "ai_attachment_parse_failed"),
        (b"x", "image", "ai_attachment_type_unsupported"),
        (b"x", "pdf", "ai_attachment_type_unsupported"),
        (b"x" * (6 * 1024 * 1024 + 1), "text", "ai_attachment_too_large"),
    ],
)
def test_text_errors_are_explicit_and_bounded(source, view, error) -> None:
    with pytest.raises(processor.ConversionError, match=error):
        processor.dispatch(
            dict(
                operation="read-page",
                filename="source.py",
                content_type="text/plain",
                data_base64=base64.b64encode(source).decode(),
                page=1,
                view=view,
            )
        )


def test_text_rejects_mime_mismatch_and_invalid_page() -> None:
    request = dict(
        operation="read-page",
        filename="source.py",
        content_type="application/json",
        data_base64=base64.b64encode(b"print('source')").decode(),
        page=1,
    )
    with pytest.raises(processor.ConversionError, match="type_unsupported"):
        processor.dispatch(request)
    request["content_type"] = "text/plain"
    for page in (0, 2, True):
        with pytest.raises(processor.ConversionError, match="page_not_found"):
            processor.dispatch(dict(request, page=page))
