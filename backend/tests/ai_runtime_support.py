"""Synthetic protocol fixtures for production runtime contract tests."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, cast

Protocol = Literal["openai", "anthropic", "google"]
FINAL_TEXT = '{"message":"Contract answer","candidate":null,"request_intent":"consultation"}'


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
                reply.wait.wait(10)
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
