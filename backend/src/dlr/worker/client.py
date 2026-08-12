"""Minimal outbound HTTP client used by the Worker agent.

Stdlib-only (urllib). The Worker always initiates connections towards the
Control Node; it never listens on any port.
"""

import json
import urllib.error
import urllib.request
from typing import Any


class ControlUnavailableError(Exception):
    """Network failure or 5xx: the Control Node is (temporarily) unusable."""


class ClientError(Exception):
    """Unexpected non-success response (e.g. 401/404): a permanent problem."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"control answered {status}: {body[:500]}")
        self.status = status
        self.body = body


class ControlClient:
    # Live-log uploads must never eat the normal API wait budget: a stuck
    # progress request would otherwise block the executor's deadline checks
    # (Important: progress is best effort and may be dropped quickly).
    PROGRESS_TIMEOUT_SECONDS = 5.0

    def __init__(self, base_url: str, token: str, timeout_seconds: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    def _request(
        self, method: str, path: str, payload: Any = None, timeout: float | None = None
    ) -> tuple[int, bytes]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self._base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=timeout if timeout is not None else self._timeout_seconds
            ) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ControlUnavailableError(str(error)) from error

    def _expect(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        expected: int = 200,
        timeout: float | None = None,
    ) -> bytes:
        status, raw = self._request(method, path, payload, timeout=timeout)
        if status >= 500:
            raise ControlUnavailableError(f"control answered {status}")
        if status != expected:
            raise ClientError(status, raw.decode(errors="replace"))
        return raw

    def register(self, name: str, capabilities: list[str]) -> dict[str, Any]:
        raw = self._expect(
            "POST", "/api/workers/register", {"name": name, "capabilities": capabilities}
        )
        body: dict[str, Any] = json.loads(raw)
        return body

    def heartbeat(self, worker_id: int) -> None:
        self._expect("POST", f"/api/workers/{worker_id}/heartbeat", expected=204)

    def mark_offline(self, worker_id: int) -> None:
        self._expect("POST", f"/api/workers/{worker_id}/offline", expected=204)

    def claim(self, worker_id: int, wait_seconds: int) -> dict[str, Any] | None:
        """Long-poll one task; None when the wait deadline expires (204)."""
        status, raw = self._request(
            "POST",
            f"/api/workers/{worker_id}/tasks/claim?wait_seconds={wait_seconds}",
        )
        if status >= 500:
            raise ControlUnavailableError(f"control answered {status}")
        if status == 204:
            return None
        if status != 200:
            raise ClientError(status, raw.decode(errors="replace"))
        task: dict[str, Any] = json.loads(raw)
        return task

    def report_result(
        self, worker_id: int, execution_id: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        raw = self._expect(
            "POST", f"/api/workers/{worker_id}/executions/{execution_id}/result", result
        )
        body: dict[str, Any] = json.loads(raw)
        return body

    def report_progress(
        self, worker_id: int, execution_id: int, stdout_chunk: str, stderr_chunk: str
    ) -> bool:
        """Best-effort live-log upload (M3). Progress is never retried here;
        callers swallow failures so an Execution can never fail because its
        live logs could not be delivered. Uses a dedicated short timeout so
        a stuck upload can never stretch the adapter execution deadline.

        Returns True when Control requested cancellation of this Execution
        (M3.2); empty uploads are legal and double as the cancel poll."""
        raw = self._expect(
            "POST",
            f"/api/workers/{worker_id}/executions/{execution_id}/progress",
            {"stdout_chunk": stdout_chunk, "stderr_chunk": stderr_chunk},
            timeout=self.PROGRESS_TIMEOUT_SECONDS,
        )
        body = json.loads(raw) if raw else {}
        return bool(body.get("cancel_requested", False))
