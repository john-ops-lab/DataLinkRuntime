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


def detect_attachment(payload: dict[str, Any]) -> bool:
    """M5.7 Wave B2: prove server-side parsed attachment text reached us.

    The context dict is serialized inside the system prompt, so the real
    attachments KEY appears JSON-escaped (\\"attachments\\"). When
    SMOKE_ATTACH_TEXT is configured (compose-smoke.sh), the extracted text
    must also be present: if the backend silently dropped or failed to parse
    the attachment, the sentinel would be nowhere in the payload.
    """
    encoded = json.dumps(payload, ensure_ascii=False)
    has_key = '\\"attachments\\"' in encoded
    sentinel = os.environ.get("SMOKE_ATTACH_TEXT", "")
    if sentinel:
        return has_key and sentinel in encoded
    return has_key


def detect_native_image(payload: dict[str, Any]) -> bool:
    """M5.7 Wave B2: prove a Provider-native image part reached us.

    Native images travel as OpenAI-style content parts in the final user
    message ({\"type\": \"image_url\", ...}); they must never be re-sent as
    parsed text and never appear in the system-prompt context. This check
    only matches the user-message image part, so a backend that silently
    dropped the image (or faked OCR text instead) fails the smoke.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict):
        return False
    content = last.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict)
        and part.get("type") == "image_url"
        and isinstance(part.get("image_url"), dict)
        and str(part["image_url"].get("url", "")).startswith("data:image/")
        for part in content
    )


def last_user_text(payload: dict[str, Any]) -> str:
    """The plain text of the most recent user message (tool scenario probe)."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "".join(parts)
    return ""


def has_tool_messages(payload: dict[str, Any]) -> bool:
    """True once DLR fed sanitized tool results back into the conversation."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    return any(isinstance(message, dict) and message.get("role") == "tool" for message in messages)


def tool_rounds(payload: dict[str, Any]) -> int:
    """Count assistant rounds that carried tool_calls (loop detection)."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0
    return sum(
        1
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "assistant"
        and isinstance(message.get("tool_calls"), list)
    )


TOOL_SCENARIOS = (
    "SMOKE_TOOL_SINGLE",
    "SMOKE_TOOL_MULTI",
    "SMOKE_TOOL_READ",
    "SMOKE_TOOL_UNKNOWN",
    "SMOKE_TOOL_WRITE",
    "SMOKE_TOOL_LOOP",
    "SMOKE_KNOWLEDGE",
)


def detect_tool_scenario(payload: dict[str, Any]) -> str | None:
    """M5.7 Wave C1: the scripted tool-call scenario for this request.

    Only requests that actually carry the DLR ``tools`` whitelist can enter a
    tool scenario; the scenario marker lives in the browser-visible user
    message, so the smoke proves the marker-driven chain end to end.
    """
    if "tools" not in payload:
        return None
    text = last_user_text(payload)
    for scenario in TOOL_SCENARIOS:
        if scenario in text:
            return scenario
    return None


def tool_calls_for(scenario: str) -> list[dict[str, Any]]:
    """The first-round tool calls of one smoke scenario (write-style tool
    names are intentionally NOT in the DLR whitelist)."""
    if scenario == "SMOKE_KNOWLEDGE":
        # M5.7 Wave C2: the read-only KnowledgeSource chain against the fake
        # official ima service (official OpenAPI contract): list -> search
        # (inside the DLR接口库 knowledge base) -> read.
        return [
            {
                "id": "call-smoke-kb-list",
                "type": "function",
                "function": {"name": "list_knowledge_bases", "arguments": '{"source": "ima"}'},
            },
            {
                "id": "call-smoke-kb-search",
                "type": "function",
                "function": {
                    "name": "search_knowledge",
                    "arguments": (
                        '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                        '"query": "contract", "limit": 2}'
                    ),
                },
            },
            {
                "id": "call-smoke-kb-read",
                "type": "function",
                "function": {
                    "name": "read_knowledge",
                    "arguments": '{"source": "ima", "item_id": "kb-item-2"}',
                },
            },
        ]
    if scenario == "SMOKE_TOOL_MULTI":
        return [
            {
                "id": "call-smoke-list",
                "type": "function",
                "function": {"name": "dlr_docs_list", "arguments": "{}"},
            },
            {
                "id": "call-smoke-search",
                "type": "function",
                "function": {"name": "dlr_docs_search", "arguments": '{"query": "secrets"}'},
            },
        ]
    if scenario == "SMOKE_TOOL_READ":
        # Reads the longest docs entry: the sanitized result gets truncated
        # server-side, exercising the bounded long-summary UI path.
        return [
            {
                "id": "call-smoke-read",
                "type": "function",
                "function": {
                    "name": "dlr_docs_read",
                    "arguments": '{"doc_id": "ai-assistant-usage"}',
                },
            }
        ]
    if scenario == "SMOKE_TOOL_UNKNOWN":
        return [
            {
                "id": "call-smoke-unknown",
                "type": "function",
                "function": {"name": "not_registered_tool", "arguments": "{}"},
            }
        ]
    if scenario == "SMOKE_TOOL_WRITE":
        return [
            {
                "id": "call-smoke-write",
                "type": "function",
                "function": {"name": "dlr_runtime_save", "arguments": '{"code": "x"}'},
            }
        ]
    return [
        {
            "id": "call-smoke-list",
            "type": "function",
            "function": {"name": "dlr_docs_list", "arguments": "{}"},
        }
    ]


def last_tool_result(payload: dict[str, Any]) -> str:
    """The sanitized tool result DLR sent back (probe for the docs source)."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def candidate_for(
    language: str,
    selected: bool,
    log_snippet: bool = False,
    attachment: bool = False,
    native_image: bool = False,
) -> dict[str, Any]:
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
    if attachment:
        suffix += " with attachment"
    if native_image:
        suffix += " with native image"
    suffix = suffix + "." if suffix else "."
    return {
        "message": f"Generated a local smoke Candidate for {language}{suffix}",
        "candidate": {
            "summary": f"Exercise the {language} AI assist contract",
            "code": code,
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

    def _completion(
        self,
        request_payload: dict[str, Any],
        *,
        message: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """One Chat Completions fixture; hidden-reasoning sentinels are always
        attached so the smoke can pin that they never reach the browser."""
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message,
            "reasoning_content": REASONING_SENTINEL,
            "reasoning_details": [{"text": REASONING_SENTINEL}],
        }
        if tool_calls is not None:
            assistant_message["tool_calls"] = tool_calls
        return {
            "id": "chatcmpl-dlr-smoke",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request_payload.get("model", MODEL_ID),
            "choices": [
                {
                    "index": 0,
                    "message": assistant_message,
                    "finish_reason": "tool_calls" if tool_calls is not None else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

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
        attachment = detect_attachment(request_payload)
        native_image = detect_native_image(request_payload)

        # M5.7 Wave C1: the controlled tool-call chain. The fake only enters
        # a scenario when the DLR whitelist actually reached it AND the user
        # message carries the scenario marker. Each scenario proves one part
        # of the bounded loop: tool call -> DLR executes -> sanitized tool
        # result returns on the same chain -> final strict AiModelOutput.
        scenario = detect_tool_scenario(request_payload)
        if scenario is not None:
            if scenario == "SMOKE_TOOL_LOOP":
                # A looping model: always ask for tools again. DLR must stop
                # deterministically with ai_tool_limit_exceeded (no hang).
                self._write_json(
                    200,
                    self._completion(
                        request_payload,
                        message=None,
                        tool_calls=tool_calls_for("SMOKE_TOOL_SINGLE"),
                    ),
                )
                return
            if not has_tool_messages(request_payload):
                self._write_json(
                    200,
                    self._completion(
                        request_payload,
                        message=None,
                        tool_calls=tool_calls_for(scenario),
                    ),
                )
                return
            # The sanitized tool result really came back to the model.
            tool_result = last_tool_result(request_payload)
            suffix = ""
            if scenario == "SMOKE_TOOL_UNKNOWN":
                suffix = " with rejected tool"
            elif scenario == "SMOKE_TOOL_WRITE":
                suffix = " with write tool rejected"
            elif scenario == "SMOKE_KNOWLEDGE":
                # M5.7 Wave C2: the knowledge chain result carries the ima:v1
                # source identifiers, and the read content echoes the ima
                # Token which DLR must have redacted to [REDACTED] before the
                # result ever rejoined this provider chain.
                if "ima:v1:kb-item-2" not in tool_result:
                    raise AssertionError("knowledge read source missing in tool result")
                token = os.environ.get("SMOKE_IMA_TOKEN", "")
                if token and token in tool_result:
                    raise AssertionError("ima credential truth leaked into the provider chain")
                if token and "[REDACTED]" not in tool_result:
                    raise AssertionError("ima credential truth was not redacted in tool result")
                suffix = " with knowledge result"
            else:
                suffix = " with tool result"
            if "dlr-docs:v1" in tool_result:
                suffix += " with docs source"
            content = json.dumps(
                candidate_for(language, selected, log_snippet, attachment, native_image),
                ensure_ascii=False,
            )
            output = json.loads(content)
            output["message"] = (
                output["message"].rstrip(".") + suffix + "."
            )
            self._write_json(200, self._completion(request_payload, message=json.dumps(output)))
            return

        content = json.dumps(
            candidate_for(language, selected, log_snippet, attachment, native_image),
            ensure_ascii=False,
        )
        self._write_json(200, self._completion(request_payload, message=content))

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
