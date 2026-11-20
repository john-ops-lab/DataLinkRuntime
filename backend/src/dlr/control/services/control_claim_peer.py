"""One-hop continuation of a pending Claim over the configured Control link."""

import http.client
import json
import ssl
import threading
from pathlib import Path
from urllib.parse import urlsplit

_MAX_RESPONSE = 4096


class ClaimPeerUnavailable(Exception):
    """The peer did not confirm the original Claim."""


class ClaimPeer:
    def __init__(self, endpoint: str | None, ca_file: str | None) -> None:
        try:
            url = urlsplit(endpoint or "")
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.path not in ("", "/")
                or url.query
                or url.fragment
                or not ca_file
                or not Path(ca_file).is_absolute()
            ):
                raise ValueError
            self._host = url.hostname
            self._port = url.port or 443
            self._tls = ssl.create_default_context(cafile=ca_file)
        except (ValueError, OSError):
            raise ClaimPeerUnavailable("Control Claim peer unavailable") from None
        self._permits = threading.BoundedSemaphore(2)

    def finalize(
        self,
        worker_id: int,
        *,
        proposal: str,
        credential: str,
        instance: str,
        generation: int,
        instance_proof: str,
    ) -> str:
        if not self._permits.acquire(blocking=False):
            raise ClaimPeerUnavailable("Control Claim peer busy")
        connection = None
        try:
            connection = http.client.HTTPSConnection(
                self._host, self._port, timeout=3.5, context=self._tls
            )
            connection.request(
                "POST",
                f"/api/workers/{worker_id}/v4/claim/finalize",
                body=b"",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-DLR-Worker-Instance": instance,
                    "X-DLR-Worker-Generation": str(generation),
                    "X-DLR-Worker-Lease-Proof": instance_proof,
                    "X-DLR-Claim-Proposal": proposal,
                    "X-DLR-Claim-Forwarded": "1",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError
            payload = response.read(_MAX_RESPONSE + 1)
            if len(payload) > _MAX_RESPONSE:
                raise ValueError
            value = json.loads(payload)
            if (
                type(value) is not dict
                or set(value) != {"attempt_proof"}
                or type(value["attempt_proof"]) is not str
                or not value["attempt_proof"]
            ):
                raise ValueError
            return value["attempt_proof"]
        except Exception:
            raise ClaimPeerUnavailable("Control Claim peer unavailable") from None
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._permits.release()
