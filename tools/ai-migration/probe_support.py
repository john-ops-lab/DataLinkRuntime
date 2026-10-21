"""Real SDKs against a scripted loopback server; never a production runtime."""

from __future__ import annotations

import base64
import io
import json
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, cast

import httpx2
from anthropic import AsyncAnthropic
from google.genai.types import HttpRetryOptions
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pypdf import PdfWriter

Protocol = Literal["openai", "anthropic", "google"]
Mode = Literal["json_schema", "json_object", "prompt_only"]
PROTOCOLS: tuple[Protocol, ...] = ("openai", "anthropic", "google")
CANARY = "b0-synthetic-secret-canary"
FINAL_TEXT = '{"message":"B0 answer","candidate":null,"request_intent":"consultation"}'
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str
    code: str
    required_secret_keys: list[str]


class Final(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str
    candidate: Candidate | None
    request_intent: Literal["consultation", "code_change"] | None = None


@dataclass
class Reply:
    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    wait: threading.Event | None = None
    entered: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


@dataclass
class CapturedRequest:
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.body))


@dataclass
class Script:
    replies: deque[Reply]
    requests: list[CapturedRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    url: str = ""


@contextmanager
def server(
    *replies: Reply, responder: Callable[[CapturedRequest], Reply] | None = None
) -> Iterator[Script]:
    script = Script(deque(replies))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with script.lock:
                captured = CapturedRequest(
                    self.path, {k.lower(): v for k, v in self.headers.items()}, body
                )
                script.requests.append(captured)
                reply = (
                    responder(captured)
                    if responder is not None
                    else (
                        script.replies.popleft()
                        if script.replies
                        else Reply(b'{"error":{"message":"unexpected extra request"}}', 500)
                    )
                )
            reply.entered.set()
            if reply.wait is not None:
                reply.wait.wait(5)
            try:
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply.body)))
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(reply.body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                reply.finished.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    script.url = f"http://127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield script
    finally:
        for reply in replies:
            if reply.wait is not None:
                reply.wait.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
        assert not thread.is_alive()


def response(
    protocol: Protocol, text: str = FINAL_TEXT, calls: list[tuple[str, str, str]] | None = None
) -> Reply:
    if protocol == "openai":
        message: dict[str, Any] = {"role": "assistant", "content": text if not calls else None}
        if calls:
            message["tool_calls"] = [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
                for name, cid, args in calls
            ]
        value = {
            "id": "b0-response",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o",
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        }
    elif protocol == "anthropic":
        content = (
            [
                {"type": "tool_use", "id": cid, "name": name, "input": json.loads(args)}
                for name, cid, args in calls
            ]
            if calls
            else [{"type": "text", "text": text}]
        )
        value = {
            "id": "msg_b0",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": content,
            "stop_reason": "tool_use" if calls else "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 3},
        }
    else:
        parts = (
            [
                {"functionCall": {"id": cid, "name": name, "args": json.loads(args)}}
                for name, cid, args in calls
            ]
            if calls
            else [{"text": text}]
        )
        value = {
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3},
        }
    return Reply(json.dumps(value).encode())


@asynccontextmanager
async def model(
    protocol: Protocol,
    url: str,
    *,
    mode: Mode = "prompt_only",
    transport: httpx2.AsyncBaseTransport | None = None,
    timeout: float = 2,
) -> AsyncIterator[Model]:
    async with httpx2.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=timeout
    ) as client:
        if protocol == "openai":
            sdk = AsyncOpenAI(
                api_key=CANARY,
                base_url=url + "/gateway/v1",
                http_client=client,
                max_retries=0,
            )
            yield OpenAIChatModel(
                "gpt-4o",
                provider=OpenAIProvider(openai_client=sdk),
                profile={
                    "supports_json_schema_output": True,
                    "supports_json_object_output": mode != "prompt_only",
                },
            )
        elif protocol == "anthropic":
            sdk_anthropic = AsyncAnthropic(
                api_key=CANARY, base_url=url + "/gateway", http_client=client, max_retries=0
            )
            yield AnthropicModel(
                "claude-sonnet-4-6",
                provider=AnthropicProvider(anthropic_client=sdk_anthropic),
                profile={"supports_json_schema_output": True},
                settings={"max_tokens": 4096},
            )
        else:
            provider = GoogleProvider(
                api_key=CANARY,
                base_url=url + "/gateway",
                http_client=client,
                retry_options=HttpRetryOptions(attempts=1),
            )
            try:
                yield GoogleModel("gemini-2.5-flash", provider=provider)
            finally:
                await provider.client.aio.aclose()
