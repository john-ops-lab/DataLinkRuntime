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


def _group_exists(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def terminate(process, *, deadline=None):
    """Confirm the entire process group disappeared, including orphan descendants.

    The container's init is responsible for reaping adopted grandchildren. A
    reaped launcher alone is insufficient evidence that conversion has stopped.
    """
    if process is None:
        return
    group = process.pid
    try:
        for sig, grace in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 2.0)):
            if deadline is not None and time.monotonic() >= deadline:
                raise ConversionError("ai_attachment_cancel_failed")
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, sig)
            until = time.monotonic() + grace
            if deadline is not None:
                until = min(until, deadline)
            while True:
                process.poll()  # Reap our direct child before testing the group.
                if not _group_exists(group):
                    wait_seconds = max(0.01, until - time.monotonic())
                    if deadline is not None:
                        wait_seconds = min(wait_seconds, max(0, deadline - time.monotonic()))
                    process.wait(timeout=wait_seconds)
                    return
                if time.monotonic() >= until:
                    break
                time.sleep(min(0.01, max(0, until - time.monotonic())))
    except (OSError, subprocess.TimeoutExpired):
        raise ConversionError("ai_attachment_cancel_failed") from None
    raise ConversionError("ai_attachment_cancel_failed")


class Job:
    def __init__(self):
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.process = None
        self.run_finished = False
        self.group_reaped = False
        self.owns_slot = True
        self.office_started = False
        self.office_reaped = False
        self.recovering = False
        self.registered = False


class Supervisor:
    def __init__(self):
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(1)
        self.jobs = {}
        self.tombstones = {}
        self.stopped = {}

    def _remember_stopped_locked(self, job_id):
        now = time.monotonic()
        self.stopped = {k: v for k, v in self.stopped.items() if now - v < 120}
        if len(self.stopped) >= 512 and job_id not in self.stopped:
            self.stopped.pop(next(iter(self.stopped)))
        self.stopped[job_id] = now

    def _finalize_locked(self, job_id, job):
        # A reaped process alone does not finish an active Office call or the
        # run's temporary-directory cleanup. Retain the permit until both end.
        if not (
            job.run_finished
            and job.group_reaped
            and job.owns_slot
            and (not job.office_started or job.office_reaped)
        ):
            return
        if self.jobs.get(job_id) is job:
            self.jobs.pop(job_id)
        job.owns_slot = False
        self.slots.release()
        if job.registered:
            self._remember_stopped_locked(job_id)
        job.done.set()

    def _cancel_office(self, job_id, job, *, deadline=None):
        timeout = 3 if deadline is None else min(3, deadline - time.monotonic())
        if timeout <= 0:
            raise ConversionError("ai_attachment_cancel_failed")
        try:
            reply = http_office(f"/v1/jobs/{job_id}", None, timeout, method="DELETE")
        except (OSError, ValueError):
            raise ConversionError("ai_attachment_cancel_failed") from None
        if not isinstance(reply, dict) or reply.get("terminated") is not True:
            raise ConversionError("ai_attachment_cancel_failed")
        if job is not None:
            with self.lock:
                job.office_reaped = True

    def _recover_finished(self, deadline):
        # A failed Control operation may have discarded its job locator. New
        # admission therefore retries the sole finished quarantine, within the
        # new request's original deadline and without disturbing active runs.
        with self.lock:
            candidate = next(
                (
                    (job_id, job)
                    for job_id, job in self.jobs.items()
                    if job.run_finished and job.owns_slot and not job.recovering
                ),
                None,
            )
            if candidate is None:
                return
            job_id, job = candidate
            job.recovering = True
        try:
            if not job.group_reaped:
                terminate(job.process, deadline=deadline)
                with self.lock:
                    job.group_reaped = True
            if job.office_started and not job.office_reaped:
                self._cancel_office(job_id, job, deadline=deadline)
            with self.lock:
                self._finalize_locked(job_id, job)
        except ConversionError:
            pass  # Keep both the retryable job and its conservative permit.
        finally:
            with self.lock:
                job.recovering = False

    def cancel(self, job_id):
        with self.lock:
            now = time.monotonic()
            self.tombstones = {k: v for k, v in self.tombstones.items() if now - v < 120}
            if len(self.tombstones) >= 512:
                self.tombstones.pop(next(iter(self.tombstones)))
            self.tombstones[job_id] = now
            job = self.jobs.get(job_id)
            stopped_at = self.stopped.get(job_id)
            known_stopped = stopped_at is not None and now - stopped_at < 120
            if job:
                job.cancelled.set()
                terminate(job.process)
                job.group_reaped = True
            needs_office = ROLE == "docling" and (
                (job.office_started and not job.office_reaped) if job else not known_stopped
            )
        if needs_office:
            self._cancel_office(job_id, job)
        if job:
            with self.lock:
                self._finalize_locked(job_id, job)
            if not job.done.wait(3):
                raise ConversionError("ai_attachment_cancel_failed")
        else:
            with self.lock:
                self._remember_stopped_locked(job_id)
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
            self._recover_finished(deadline)
            if time.monotonic() >= deadline:
                raise ConversionError("ai_attachment_parse_timeout")
            if not self.slots.acquire(blocking=False):
                raise ConversionError("ai_attachment_converter_busy")
        job = Job()
        try:
            with self.lock:
                if job_id in self.jobs or job_id in self.tombstones:
                    raise ConversionError("ai_attachment_cancelled")
                self.jobs[job_id] = job
                job.registered = True
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
                    with self.lock:
                        if job.cancelled.is_set():
                            raise ConversionError("ai_attachment_cancelled")
                        job.office_started = True
                        job.office_active = True
                    try:
                        reply = http_office(
                            "/v1/prepare",
                            request,
                            max(0.05, deadline - time.monotonic()),
                        )
                        request["rendered_pdf_base64"] = reply["rendered_pdf"]
                        with self.lock:
                            # The Office supervisor returns success only after its
                            # process group and temporary directory have finished.
                            job.office_reaped = True
                    except (OSError, urllib.error.URLError, KeyError, ValueError):
                        self._cancel_office(job_id, job)
                        request["office_failed"] = True
                    finally:
                        with self.lock:
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
            reaped = False
            try:
                terminate(job.process)
                reaped = True
            finally:
                with self.lock:
                    job.run_finished = True
                    job.group_reaped = job.group_reaped or reaped
                    # Failed reaping leaves the job quarantined. A later cancel
                    # can confirm its group stopped and finalize this same permit.
                    self._finalize_locked(job_id, job)


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
