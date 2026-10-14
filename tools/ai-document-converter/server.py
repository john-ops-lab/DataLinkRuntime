"""Small internal HTTP supervisor: one bounded child per conversion service."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from processor import ConversionError, archive_policy, decode

MAX_WIRE = 96 * 1024 * 1024
ROLE = os.environ.get("DLR_DOCUMENT_ROLE", "docling")
OFFICE_URL = os.environ.get("DLR_OFFICE_CONVERTER_URL", "http://ai-office:5002").rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_office(path, payload, timeout, method="POST"):
    request = urllib.request.Request(
        OFFICE_URL + path,
        data=json.dumps(payload).encode() if payload else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_WIRE + 1)
    if len(body) > MAX_WIRE:
        raise ConversionError("ai_attachment_too_large")
    return json.loads(body)


def terminate(process):
    if process is None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)
    # Reap/kill descendants too; Office may exit its launcher before a child.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


class Job:
    def __init__(self):
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.process = None


class Supervisor:
    def __init__(self):
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(1)
        self.jobs = {}
        self.tombstones = {}

    def cancel(self, job_id):
        with self.lock:
            now = time.monotonic()
            self.tombstones = {k: v for k, v in self.tombstones.items() if now - v < 120}
            if len(self.tombstones) >= 512:
                self.tombstones.pop(next(iter(self.tombstones)))
            self.tombstones[job_id] = now
            job = self.jobs.get(job_id)
            if job:
                job.cancelled.set()
                terminate(job.process)
        if ROLE == "docling":
            try:
                reply = http_office(f"/v1/jobs/{job_id}", None, 3, method="DELETE")
                if reply.get("terminated") is not True:
                    raise ConversionError("ai_attachment_cancel_failed")
            except (OSError, urllib.error.URLError):
                # No Office call is needed for PDF/sheet jobs; a running Office
                # phase records itself before the network request.
                if job and getattr(job, "office_active", False):
                    raise ConversionError("ai_attachment_cancel_failed") from None
        if job and not job.done.wait(3):
            raise ConversionError("ai_attachment_cancel_failed")
        return {"terminated": True, "cancelled": True}

    def run(self, request, operation):
        job_id = request.get("job_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            raise ConversionError("ai_attachment_invalid")
        filename = request.get("filename", "")
        if (
            not isinstance(filename, str)
            or "/" in filename
            or "\\" in filename
            or len(filename) > 255
        ):
            raise ConversionError("ai_attachment_filename_invalid")
        source = decode(request.get("data_base64"))
        deadline = time.monotonic() + min(60, max(0.05, float(request.get("timeout_seconds", 30))))
        if not self.slots.acquire(blocking=False):
            raise ConversionError("ai_attachment_converter_busy")
        job = Job()
        try:
            with self.lock:
                if job_id in self.jobs or job_id in self.tombstones:
                    raise ConversionError("ai_attachment_cancelled")
                self.jobs[job_id] = job
            request = {**request, "operation": operation}
            extension = Path(filename).suffix.lower()
            if (
                ROLE == "docling"
                and operation != "read-sheet"
                and extension in {".xlsx", ".xls", ".docx"}
                and not request.get("rendered_pdf_base64")
            ):
                blocked = archive_policy(source) if extension in {".xlsx", ".docx"} else []
                request["office_warnings"] = blocked
                if not blocked:
                    job.office_active = True
                    try:
                        reply = http_office(
                            "/v1/prepare",
                            request,
                            max(0.05, deadline - time.monotonic()),
                        )
                        request["rendered_pdf_base64"] = reply["rendered_pdf"]
                    except (OSError, urllib.error.URLError, KeyError, ValueError):
                        http_office(f"/v1/jobs/{job_id}", None, 3, method="DELETE")
                        request["office_failed"] = True
                    finally:
                        job.office_active = False
            if job.cancelled.is_set():
                raise ConversionError("ai_attachment_cancelled")
            if time.monotonic() >= deadline:
                raise ConversionError("ai_attachment_parse_timeout")
            with tempfile.TemporaryDirectory(prefix="dlr-ai-document-") as directory:
                root = Path(directory)
                input_file = root / "request.json"
                output = root / "result.json"
                input_file.write_text(json.dumps(request))
                command = [
                    sys.executable,
                    str(
                        Path(__file__).with_name(
                            "office.py" if ROLE == "office" else "processor.py"
                        )
                    ),
                    str(input_file),
                    str(output),
                ]
                with self.lock:
                    if job.cancelled.is_set():
                        raise ConversionError("ai_attachment_cancelled")
                    job.process = subprocess.Popen(
                        command,
                        start_new_session=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env={**os.environ, "HOME": str(root)},
                        close_fds=True,
                    )
                while job.process.poll() is None:
                    if job.cancelled.wait(0.02):
                        terminate(job.process)
                        raise ConversionError("ai_attachment_cancelled")
                    if time.monotonic() >= deadline:
                        terminate(job.process)
                        if ROLE == "docling" and getattr(job, "office_active", False):
                            http_office(f"/v1/jobs/{job_id}", None, 3, method="DELETE")
                        raise ConversionError("ai_attachment_parse_timeout")
                if output.exists() and output.stat().st_size <= MAX_WIRE:
                    result = json.loads(output.read_text())
                    if "code" in result:
                        raise ConversionError(result["code"])
                    if job.process.returncode == 0:
                        return result
                raise ConversionError("ai_attachment_parse_failed")
        finally:
            terminate(job.process)
            with self.lock:
                if self.jobs.get(job_id) is job:
                    self.jobs.pop(job_id)
            job.done.set()
            self.slots.release()


SUPERVISOR = Supervisor()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # Do not log document filenames, paths, contents, or base64.

    def reply(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok", "role": ROLE, "max_workers": 1})
        else:
            self.reply(404, {"code": "not_found"})

    def do_DELETE(self):
        match = re.fullmatch(r"/v1/jobs/([A-Za-z0-9_-]{1,80})", self.path)
        if not match:
            self.reply(422, {"code": "ai_attachment_invalid"})
            return
        try:
            self.reply(200, SUPERVISOR.cancel(match.group(1)))
        except ConversionError as error:
            self.reply(503, {"code": error.code})

    def do_POST(self):
        operation = {
            "/v1/prepare": "prepare",
            "/v1/read-page": "read-page",
            "/v1/read-sheet": "read-sheet",
        }.get(self.path)
        if operation is None:
            self.reply(404, {"code": "not_found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > MAX_WIRE:
                raise ConversionError("ai_attachment_too_large")
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ConversionError("ai_attachment_invalid")
            self.reply(200, SUPERVISOR.run(body, operation))
        except ConversionError as error:
            self.reply(
                429 if error.code == "ai_attachment_converter_busy" else 422,
                {"code": error.code},
            )
        except Exception:
            # Third-party archive/XML failures are sanitized just like child
            # failures; never echo source names, bodies or parser diagnostics.
            self.reply(422, {"code": "ai_attachment_parse_failed"})


if __name__ == "__main__":
    os.umask(0o077)
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "5001"))), Handler).serve_forever()
