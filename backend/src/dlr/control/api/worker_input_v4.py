"""Bounded file delivery from the existing proof-bound Control child."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from dlr.common.attempt_lease_proof_wire_v4 import parse_untrusted_attempt_proof
from dlr.control.api.shared_backend import require_backend
from dlr.control.api.worker_claim_v4 import _coordinator
from dlr.control.api.worker_instance_v4 import _HEADERS, _one_header, _process, _proved_identity
from dlr.control.services.claim_child_v4 import _clock
from dlr.control.services.worker_protocol import CLAIM_TOKEN_HEADER

router = APIRouter()


def _error() -> HTTPException:
    return HTTPException(
        status_code=503, detail={"code": "input_download_unavailable"}, headers=_HEADERS
    )


@router.get(
    "/api/workers/{worker_id}/v4/executions/{execution_id}/input-artifacts/{artifact_id}/content"
)
async def download(
    request: Request, worker_id: int, execution_id: int, artifact_id: int
) -> StreamingResponse:
    return await _download(request, worker_id, execution_id, artifact_id, builtin_package=False)


@router.get(
    "/api/workers/{worker_id}/v4/executions/{execution_id}/builtin-packages/{file_id}/content"
)
async def download_builtin(
    request: Request, worker_id: int, execution_id: int, file_id: int
) -> StreamingResponse:
    return await _download(request, worker_id, execution_id, file_id, builtin_package=True)


async def _download(
    request: Request, worker_id: int, execution_id: int, artifact_id: int, *, builtin_package: bool
) -> StreamingResponse:
    if request.url.scheme != "https":
        raise _error()
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    require_backend("builtin_packages" if builtin_package else "managed_files")
    try:
        if (
            request.url.query
            or type(execution_id) is not int
            or not 0 < execution_id < 2**63
            or type(artifact_id) is not int
            or not 0 < artifact_id < 2**63
            or len(instance_proof) > 4096
            or request.headers.getlist("range")
            or request.headers.getlist("transfer-encoding")
            or any(
                len(request.headers.getlist(name)) > 1
                for name in (
                    "content-encoding",
                    "accept-encoding",
                    "content-length",
                )
            )
            or request.headers.get("content-encoding", "identity") != "identity"
            or request.headers.get("accept-encoding", "identity") != "identity"
            or request.headers.get("content-length", "0") != "0"
            or request.headers.getlist("x-dlr-cleanup-token")
        ):
            raise ValueError
        attempt_proof = _one_header(request, "x-dlr-worker-attempt-proof")
        claim_token = _one_header(request, CLAIM_TOKEN_HEADER)
        public = parse_untrusted_attempt_proof(attempt_proof)
        if (
            public is None
            or public.worker_id != worker_id
            or public.instance_id != instance
            or public.generation != generation
            or not 1 <= len(claim_token) <= 512
        ):
            raise ValueError
        # Parsed fields only select a bounded slot. The child verifies both
        # signatures/current candidates and every database ownership relation.
        metadata = {
            "worker_id": worker_id,
            "instance_id": str(instance),
            "generation": generation,
            "credential": credential,
            "instance_proof": instance_proof,
            "attempt_proof": attempt_proof,
            "attempt_id": public.attempt_id,
            "fencing_token": public.fencing_token,
            "claim_token": claim_token,
            "execution_id": execution_id,
            "artifact_id": artifact_id,
            "builtin_package": builtin_package,
        }
        coordinator = _coordinator(request)
        entry = await run_in_threadpool(
            coordinator.begin_input, _process(request), metadata=metadata
        )
    except Exception:
        raise _error() from None

    async def chunks() -> AsyncGenerator[bytes]:
        try:
            while True:
                value = await run_in_threadpool(coordinator.read_input, entry)
                if not value:
                    return
                yield value
                del value
        finally:
            await run_in_threadpool(coordinator.close_input, entry)

    class InputResponse(StreamingResponse):
        async def stream_response(self, send: Send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            async for value in self.body_iterator:
                if not isinstance(value, bytes):
                    raise _error()
                await send({"type": "http.response.body", "body": value, "more_body": True})
                del value
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            try:
                async with asyncio.timeout(max(0.001, entry.deadline - _clock())):
                    await super().__call__(scope, receive, send)
            finally:
                try:
                    await body.aclose()
                finally:
                    await run_in_threadpool(coordinator.close_input, entry)

    body = chunks()
    return InputResponse(
        body,
        media_type="application/octet-stream",
        headers={**_HEADERS, "Content-Length": str(entry.size)},
    )
