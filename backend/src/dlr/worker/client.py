"""Minimal outbound HTTP client used by the Worker agent.

Stdlib-only (urllib). The Worker always initiates connections towards the
Control Node; it never listens on any port.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
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
    DOWNLOAD_CHUNK_BYTES = 64 * 1024

    def __init__(self, base_url: str, token: str, timeout_seconds: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        timeout: float | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self._base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
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
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        status, raw = self._request(method, path, payload, timeout=timeout, headers=headers)
        if status >= 500:
            raise ControlUnavailableError(f"control answered {status}")
        if status != expected:
            raise ClientError(status, raw.decode(errors="replace"))
        return raw

    def register(
        self,
        name: str,
        capabilities: list[str],
        *,
        protocol_version: int = 3,
        isolation_capabilities: Mapping[str, bool] | None = None,
    ) -> dict[str, Any]:
        raw = self._expect(
            "POST",
            "/api/workers/register",
            {
                "name": name,
                "capabilities": capabilities,
                "protocol_version": protocol_version,
                "isolation_capabilities": dict(isolation_capabilities or {}),
            },
        )
        body: dict[str, Any] = json.loads(raw)
        return body

    def heartbeat(
        self,
        worker_id: int,
        *,
        isolation_capabilities: Mapping[str, bool] | None = None,
    ) -> None:
        payload = (
            {"isolation_capabilities": dict(isolation_capabilities)}
            if isolation_capabilities is not None
            else None
        )
        self._expect("POST", f"/api/workers/{worker_id}/heartbeat", payload, expected=204)

    def mark_offline(self, worker_id: int) -> None:
        self._expect("POST", f"/api/workers/{worker_id}/offline", expected=204)

    def download_input_artifact(
        self,
        worker_id: int,
        execution_id: int,
        artifact_id: int,
        *,
        claim_token: str,
        destination: Any,
    ) -> int:
        """Stream one leased input Artifact into ``destination``.

        The caller owns the temporary destination and performs the payload
        size/SHA-256 check.  This method never places the Artifact bytes in a
        JSON response or a URL and only buffers a bounded response chunk.
        """
        path = (
            f"/api/workers/{worker_id}/executions/{execution_id}"
            f"/input-artifacts/{artifact_id}/content"
        )
        request = urllib.request.Request(self._base_url + path, method="GET")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("X-DLR-Claim-Token", claim_token)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                status = int(response.status)
                if status >= 500:
                    raise ControlUnavailableError(f"control answered {status}")
                if status != 200:
                    raw = response.read(4096)
                    raise ClientError(status, raw.decode(errors="replace"))
                total = 0
                while True:
                    chunk = response.read(self.DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        return total
                    destination.write(chunk)
                    total += len(chunk)
        except urllib.error.HTTPError as error:
            raw = error.read(4096)
            if error.code >= 500:
                raise ControlUnavailableError(f"control answered {error.code}") from error
            raise ClientError(error.code, raw.decode(errors="replace")) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ControlUnavailableError(str(error)) from error

    def report_cleanup_receipt(
        self, worker_id: int, execution_id: int, *, cleanup_token: str
    ) -> dict[str, Any]:
        """Confirm local Workspace cleanup with the independent credential."""
        raw = self._expect(
            "POST",
            f"/api/workers/executions/{execution_id}/workspace-cleanup",
            {"status": "completed"},
            headers={"X-DLR-Cleanup-Token": cleanup_token},
        )
        body: dict[str, Any] = json.loads(raw) if raw else {}
        return body

    def report_cleanup(self, worker_id: int, cleanup_id: int, *, success: bool) -> None:
        """Report only a cleanup outcome; filesystem details stay local."""
        self._expect(
            "POST",
            f"/api/workers/{worker_id}/cleanups/{cleanup_id}/result",
            {"success": success},
            expected=204,
        )

    def claim_cleanup(self, worker_id: int) -> dict[str, Any] | None:
        """Claim an adapter filesystem cleanup, never an execution payload."""
        status, raw = self._request("POST", f"/api/workers/{worker_id}/cleanups/claim")
        if status >= 500:
            raise ControlUnavailableError(f"control answered {status}")
        if status == 204:
            return None
        if status != 200:
            raise ClientError(status, raw.decode(errors="replace"))
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("kind") != "adapter_cleanup":
            raise ClientError(502, "cleanup response is not a cleanup task")
        return body

    def claim_v3(self, worker_id: int, dispatch: Mapping[str, Any]) -> dict[str, Any]:
        """Submit only the small dispatch body; Control owns all DB reads."""
        raw = self._expect(
            "POST",
            f"/api/workers/{worker_id}/v3/claim",
            dict(dispatch),
            timeout=self._timeout_seconds,
        )
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ClientError(502, "claim response is not an object")
        return body

    def _attempt_request(
        self,
        worker_id: int,
        attempt_id: int,
        action: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = self._expect(
            "POST",
            f"/api/workers/{worker_id}/attempts/{attempt_id}/{action}",
            dict(payload),
            timeout=self.PROGRESS_TIMEOUT_SECONDS
            if action == "progress"
            else self._timeout_seconds,
        )
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ClientError(502, "attempt response is not an object")
        return body

    def start_attempt(
        self, worker_id: int, attempt_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._attempt_request(worker_id, attempt_id, "start", payload)

    def renew_attempt(
        self, worker_id: int, attempt_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._attempt_request(worker_id, attempt_id, "renew", payload)

    def progress_attempt(
        self, worker_id: int, attempt_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._attempt_request(worker_id, attempt_id, "progress", payload)

    def result_attempt(
        self, worker_id: int, attempt_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._attempt_request(worker_id, attempt_id, "result", payload)

    def prepare_failed_attempt(
        self, worker_id: int, attempt_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._attempt_request(worker_id, attempt_id, "prepare-failed", payload)
