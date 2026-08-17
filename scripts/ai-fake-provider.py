"""Smoke-only local OpenAI-compatible provider.

This server is launched as a temporary one-off container by compose-smoke.sh.
It deliberately has no production configuration or external network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_ID = "dlr-smoke-model"
REASONING_SENTINEL = "SMOKE_REASONING_MUST_NOT_REACH_BROWSER"


class RequestMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models = 0
        self._completions = 0

    def increment_models(self) -> None:
        with self._lock:
            self._models += 1

    def increment_completions(self) -> None:
        with self._lock:
            self._completions += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"models": self._models, "chat_completions": self._completions}


METRICS = RequestMetrics()


def detect_language(payload: dict[str, Any]) -> str:
    """Select a deterministic Candidate from the current Working Copy prompt."""

    prompt = json.dumps(payload, ensure_ascii=False)
    if "public Object handle" in prompt or '"language": "java"' in prompt:
        return "java"
    if "export async function handle" in prompt or '"language": "javascript"' in prompt:
        return "javascript"
    return "python"


def detect_selected_context(payload: dict[str, Any]) -> bool:
    """M5.5.13: prove the structured context_snippets block reached us.

    The context dict is serialized inside the system prompt, so a real
    context_snippets KEY appears with JSON-escaped quotes (\\"context_snippets\\").
    The prose explanation in the system prompt contains the bare word
    context_snippets without quotes, so the key match cannot be a false
    positive. When SMOKE_SELECTED_TEXT is configured (compose-smoke.sh), the
    actual selected text must also be present: if the backend silently dropped
    the snippets, the sentinel would be nowhere in the payload.
    """
    encoded = json.dumps(payload, ensure_ascii=False)
    has_key = '\\"context_snippets\\"' in encoded
    sentinel = os.environ.get("SMOKE_SELECTED_TEXT", "")
    if sentinel:
        return has_key and sentinel in encoded
    return has_key


def detect_log_snippet(payload: dict[str, Any]) -> bool:
    """M5.5.13: prove a masked live-log snippet reached us.

    Like detect_selected_context, the real key appears JSON-escaped inside the
    system prompt. When SMOKE_LOG_TEXT is configured (compose-smoke.sh), the
    masked log text must also be present: if the backend silently dropped the
    log snippet, the sentinel would be nowhere in the payload.
    """
    encoded = json.dumps(payload, ensure_ascii=False)
    has_log = '\\"source\\": \\"log\\"' in encoded
    sentinel = os.environ.get("SMOKE_LOG_TEXT", "")
    if sentinel:
        return has_log and sentinel in encoded
    return has_log


def candidate_for(language: str, selected: bool, log_snippet: bool = False) -> dict[str, Any]:
    code = {
        "python": (
            "def handle(context, input):\n    return {'ai_smoke': 'python', 'input': input}\n"
        ),
        "javascript": (
            "export async function handle(context, input) {\n"
            "  return { ai_smoke: 'javascript', input };\n"
            "}\n"
        ),
        "java": (
            "import java.util.Map;\n"
            "public class Adapter {\n"
            "  public Object handle(Context context, Object input) throws Exception {\n"
            '    return Map.of("ai_smoke", "java", "input", input);\n'
            "  }\n"
            "}\n"
        ),
    }[language]
    suffix = ""
    if selected:
        suffix += " with selected context"
    if log_snippet:
        suffix += " with log snippet"
    suffix = suffix + "." if suffix else "."
    return {
        "message": f"Generated a local smoke Candidate for {language}{suffix}",
        "candidate": {
            "summary": f"Exercise the {language} AI assist contract",
            "code": code,
            "requirements": "",
            "runtime_config": {"ai_smoke": language},
            "required_secret_keys": [],
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DLRSmokeProvider/1.0"

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError("request body must be an object")
        return payload

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            METRICS.increment_models()
            # Smoke mode for M5.5.2: emulate a Provider that answers but does
            # not expose a compatible /v1/models endpoint. Test Connection
            # must stay independent of model discovery.
            if os.environ.get("SMOKE_DISABLE_MODELS") == "1":
                self._write_json(
                    404,
                    {"error": {"message": "model list disabled for smoke"}},
                )
                return
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": MODEL_ID, "object": "model", "owned_by": "dlr-smoke"}],
                },
            )
            return
        if self.path == "/_smoke/metrics":
            self._write_json(200, METRICS.snapshot())
            return
        self._write_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._write_json(404, {"error": {"message": "not found"}})
            return
        try:
            request_payload = self._read_json()
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            self._write_json(400, {"error": {"message": str(error)}})
            return

        METRICS.increment_completions()
        # Optional artificial latency for UI verification (default off):
        # SMOKE_CHAT_DELAY_SECONDS keeps the request lifecycle observable.
        delay_seconds = float(os.environ.get("SMOKE_CHAT_DELAY_SECONDS", "0"))
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        language = detect_language(request_payload)
        selected = detect_selected_context(request_payload)
        log_snippet = detect_log_snippet(request_payload)
        content = json.dumps(
            candidate_for(language, selected, log_snippet), ensure_ascii=False
        )
        self._write_json(
            200,
            {
                "id": "chatcmpl-dlr-smoke",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request_payload.get("model", MODEL_ID),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content,
                            "reasoning_content": REASONING_SENTINEL,
                            "reasoning_details": [{"text": REASONING_SENTINEL}],
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    def log_message(self, format_string: str, *args: object) -> None:
        # Keep smoke logs useful without ever logging headers or request bodies.
        print(f"ai-fake: {format_string % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
