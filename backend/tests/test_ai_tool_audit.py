"""Request-correlation and bounded AI tool audit logging tests."""

import hashlib
import inspect
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dlr.common.config import Settings, settings
from dlr.control.ai import providers, tool_audit
from dlr.control.ai import tools as tools_service
from dlr.control.schemas.ai import AiAssistRequest
from dlr.control.services import ai as ai_service
from test_ai import assist_body, configure, create_adapter, fake_chat_response, valid_output


@pytest.fixture(autouse=True)
def _close_audit_handler() -> None:
    tool_audit.close_ai_tool_audit_logging()
    yield
    tool_audit.close_ai_tool_audit_logging()


def _request_payload() -> dict[str, object]:
    return {
        "message": "Explain this adapter.",
        "working_copy": {"code": "return input", "requirements": "", "runtime_config": {}},
        "recent_messages": [],
        "base_version_id": None,
    }


def _configure_for_test(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_bytes: int,
    backup_count: int,
) -> logging.Logger:
    monkeypatch.setattr(settings, "platform_log_root", str(root))
    monkeypatch.setattr(settings, "ai_tool_audit_max_bytes", max_bytes)
    monkeypatch.setattr(settings, "ai_tool_audit_backup_count", backup_count)
    assert tool_audit.configure_ai_tool_audit_logging()
    return logging.getLogger(tool_audit.AUDIT_LOGGER_NAME)


def _records() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in tool_audit.audit_log_path().read_text(encoding="utf-8").splitlines()
    ]


def _call(name: str, arguments: str = "{}", call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _tool_response(calls: list[dict[str, Any]]) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {"content": None, "role": "assistant", "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_response(message: str = "Audit final answer.") -> dict[str, object]:
    output = valid_output()
    output["message"] = message
    return fake_chat_response(output)


def test_assist_conversation_id_is_optional_and_canonical_uuid_v4() -> None:
    legacy = AiAssistRequest.model_validate(_request_payload())
    assert legacy.conversation_id is None

    conversation_id = "3b241101-e2bb-4255-8caf-4136c566a962"
    current = AiAssistRequest.model_validate(
        {**_request_payload(), "conversation_id": conversation_id}
    )
    assert current.conversation_id == conversation_id

    for invalid in (
        "not-a-uuid",
        "3B241101-E2BB-4255-8CAF-4136C566A962",
        "3b241101-e2bb-1255-8caf-4136c566a962",
        "3b241101-e2bb-4255-7caf-4136c566a962",
    ):
        with pytest.raises(ValidationError):
            AiAssistRequest.model_validate({**_request_payload(), "conversation_id": invalid})


def test_request_correlation_is_unique_with_old_client_fallback() -> None:
    conversation_id = "3b241101-e2bb-4255-8caf-4136c566a962"
    first = tool_audit.new_request_correlation(conversation_id)
    second = tool_audit.new_request_correlation(conversation_id)
    legacy = tool_audit.new_request_correlation(None)

    assert first.request_id != second.request_id
    assert first.conversation_id == second.conversation_id == conversation_id
    assert legacy.conversation_id != legacy.request_id
    assert uuid.UUID(legacy.request_id).version == 4
    assert uuid.UUID(legacy.conversation_id).version == 4


def test_ai_tool_audit_config_defaults_and_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLR_AI_TOOL_AUDIT_MAX_BYTES", raising=False)
    monkeypatch.delenv("DLR_AI_TOOL_AUDIT_BACKUP_COUNT", raising=False)
    assert Settings().ai_tool_audit_max_bytes == 10 * 1024 * 1024
    assert Settings().ai_tool_audit_backup_count == 10

    for max_bytes in (1, 100 * 1024 * 1024):
        monkeypatch.setenv("DLR_AI_TOOL_AUDIT_MAX_BYTES", str(max_bytes))
        assert Settings().ai_tool_audit_max_bytes == max_bytes
    for backup_count in (1, 100):
        monkeypatch.setenv("DLR_AI_TOOL_AUDIT_BACKUP_COUNT", str(backup_count))
        assert Settings().ai_tool_audit_backup_count == backup_count


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DLR_AI_TOOL_AUDIT_MAX_BYTES", 0),
        ("DLR_AI_TOOL_AUDIT_MAX_BYTES", 100 * 1024 * 1024 + 1),
        ("DLR_AI_TOOL_AUDIT_BACKUP_COUNT", 0),
        ("DLR_AI_TOOL_AUDIT_BACKUP_COUNT", 101),
    ],
)
def test_ai_tool_audit_config_rejects_unbounded_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: int,
) -> None:
    monkeypatch.setenv(name, str(value))
    with pytest.raises(ValidationError):
        Settings()


def test_ai_tool_audit_rotates_complete_json_lines_and_removes_oldest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _configure_for_test(tmp_path, monkeypatch, max_bytes=180, backup_count=2)
    for sequence in range(8):
        logger.info(
            json.dumps(
                {"sequence": sequence, "padding": "x" * 90},
                separators=(",", ":"),
            )
        )

    tool_audit.close_ai_tool_audit_logging()
    current = tool_audit.audit_log_path()
    files = [current, Path(f"{current}.1"), Path(f"{current}.2")]
    assert all(path.exists() for path in files)
    assert not Path(f"{current}.3").exists()

    retained_sequences: list[int] = []
    for path in files:
        raw_lines = path.read_bytes().splitlines(keepends=True)
        assert raw_lines
        assert all(line.endswith(b"\n") for line in raw_lines)
        retained_sequences.extend(json.loads(line)["sequence"] for line in raw_lines)
    assert 0 not in retained_sequences
    assert 7 in retained_sequences


def test_ai_tool_audit_restart_continues_appending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _configure_for_test(tmp_path, monkeypatch, max_bytes=4096, backup_count=2)
    logger.info('{"request_id":"first"}')
    tool_audit.close_ai_tool_audit_logging()

    assert tool_audit.configure_ai_tool_audit_logging()
    logger.info('{"request_id":"second"}')
    tool_audit.close_ai_tool_audit_logging()

    records = [json.loads(line) for line in tool_audit.audit_log_path().read_text().splitlines()]
    assert records == [{"request_id": "first"}, {"request_id": "second"}]


def test_ai_tool_audit_logger_does_not_propagate_or_match_external_log_glob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _configure_for_test(tmp_path, monkeypatch, max_bytes=4096, backup_count=2)
    root_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            root_records.append(record)

    root_handler = _Capture()
    root = logging.getLogger()
    root.addHandler(root_handler)
    try:
        logger.info('{"request_id":"isolated"}')
    finally:
        root.removeHandler(root_handler)

    assert logger.level == logging.INFO
    assert logger.propagate is False
    assert root_records == []
    assert tool_audit.audit_log_path().name == "ai-tool-audit.jsonl"
    assert not tool_audit.audit_log_path().match("*.log")


def test_assist_audit_flushes_success_before_provider_followup_failure_and_terminal(
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "audit-provider-followup")
    configure(api_client)
    _configure_for_test(tmp_path, monkeypatch, max_bytes=64 * 1024, backup_count=2)
    provider_tool_rounds = 0
    records_before_failure: list[dict[str, Any]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal provider_tool_rounds
        if payload is not None and "tools" not in payload:
            return _final_response("Finalized after Provider follow-up failure.")
        provider_tool_rounds += 1
        if provider_tool_rounds == 1:
            return _tool_response([_call("dlr_docs_list")])
        records_before_failure.extend(_records())
        raise providers.AiProviderError("ai_provider_unreachable")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text

    assert len(records_before_failure) == 1
    assert records_before_failure[0]["event_type"] == "tool_attempt"
    assert records_before_failure[0]["status"] == "success"
    records = _records()
    assert [record["event_type"] for record in records] == [
        "tool_attempt",
        "guard",
        "request_terminal",
    ]
    assert len({record["request_id"] for record in records}) == 1
    assert records[1]["stop_reason"] == ai_service._STOP_PROVIDER_FAILURE
    assert records[1]["error_code"] == "ai_provider_unreachable"
    assert records[2]["status"] == "stopped"
    assert records[2]["successful_calls"] == 1
    assert records[2]["failed_calls"] == 0
    assert records[2]["blocked_calls"] == 0


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [("handler", tools_service.CODE_FAILED), ("timeout", tools_service.CODE_TIMEOUT)],
)
def test_assist_audit_records_tool_failure_and_timeout_immediately(
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_code: str,
) -> None:
    adapter = create_adapter(api_client, f"audit-tool-{failure}")
    configure(api_client)
    _configure_for_test(tmp_path, monkeypatch, max_bytes=64 * 1024, backup_count=2)

    if failure == "handler":

        def handler(_args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("private failure detail")

    else:

        def handler(_args: dict[str, Any]) -> dict[str, Any]:
            import time

            time.sleep(0.01)
            return {"ok": True}

        monkeypatch.setattr(tools_service, "TOOL_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", handler)
    provider_calls = 0

    def fake_request(*_: object, **__: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            return _tool_response([_call("dlr_docs_list")])
        return _final_response()

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    records = _records()
    assert records[0]["event_type"] == "tool_attempt"
    assert records[0]["status"] == "error"
    assert records[0]["error_code"] == expected_code
    assert records[0]["duration_ms"] >= 0
    assert records[-1]["event_type"] == "request_terminal"
    assert records[-1]["status"] == "success"
    assert records[-1]["failed_calls"] == 1


def test_assist_audit_records_duplicate_and_consecutive_failure_guards(
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "audit-duplicate")
    configure(api_client)
    _configure_for_test(tmp_path, monkeypatch, max_bytes=64 * 1024, backup_count=2)
    provider_calls = 0

    def duplicate_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal provider_calls
        if payload is not None and "tools" not in payload:
            return _final_response()
        provider_calls += 1
        arguments = '{"query":"secrets","limit":2}'
        if provider_calls == 2:
            arguments = '{ "limit": 2, "query": "secrets" }'
        return _tool_response(
            [_call("dlr_docs_search", arguments, call_id=f"duplicate-{provider_calls}")]
        )

    monkeypatch.setattr(providers, "_request_json", duplicate_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    records = _records()
    attempts = [record for record in records if record["event_type"] == "tool_attempt"]
    assert [record["status"] for record in attempts] == ["success", "blocked"]
    assert attempts[1]["call_index"] == 2
    assert attempts[1]["error_code"] == tools_service.CODE_DUPLICATE
    assert attempts[1]["stop_reason"] == ai_service._STOP_DUPLICATE
    assert records[-1]["blocked_calls"] == 1

    tool_audit.close_ai_tool_audit_logging()
    next_root = tmp_path / "consecutive"
    _configure_for_test(next_root, monkeypatch, max_bytes=64 * 1024, backup_count=2)
    consecutive_adapter = create_adapter(api_client, "audit-consecutive")
    provider_calls = 0

    def consecutive_request(*_: object, **__: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return _tool_response(
            [_call(f"unknown-write-{provider_calls}", call_id=f"failure-{provider_calls}")]
        )

    monkeypatch.setattr(providers, "_request_json", consecutive_request)
    response = api_client.post(
        f"/api/adapters/{consecutive_adapter['id']}/ai/assist",
        json=assist_body(),
    )
    assert response.status_code == 200, response.text
    records = _records()
    attempts = [record for record in records if record["event_type"] == "tool_attempt"]
    assert len(attempts) == 3
    assert all(record["status"] == "error" for record in attempts)
    assert attempts[-1]["stop_reason"] == ai_service._STOP_CONSECUTIVE_FAILURES
    assert records[-1]["failed_calls"] == 3
    assert records[-1]["status"] == "stopped"


def test_assist_audit_records_call_budget_and_deadline_intercepts(
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "audit-call-budget")
    configure(api_client)
    _configure_for_test(tmp_path, monkeypatch, max_bytes=128 * 1024, backup_count=2)

    def oversized_batch(*_: object, **__: object) -> object:
        return _tool_response(
            [
                _call("dlr_docs_list", call_id=f"budget-{index}")
                for index in range(tools_service.MAX_TOOL_CALLS_PER_ASSIST + 1)
            ]
        )

    monkeypatch.setattr(providers, "_request_json", oversized_batch)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    records = _records()
    attempts = [record for record in records if record["event_type"] == "tool_attempt"]
    assert len(attempts) == tools_service.MAX_TOOL_CALLS_PER_ASSIST + 1
    assert [record["call_index"] for record in attempts] == list(range(1, 18))
    assert all(record["status"] == "blocked" for record in attempts)
    assert all(record["stop_reason"] == ai_service._STOP_CALL_BUDGET for record in attempts)
    assert records[-1]["blocked_calls"] == 17

    tool_audit.close_ai_tool_audit_logging()
    deadline_root = tmp_path / "deadline"
    _configure_for_test(deadline_root, monkeypatch, max_bytes=64 * 1024, backup_count=2)
    deadline_adapter = create_adapter(api_client, "audit-deadline")
    clock = {"now": 0.0}
    monkeypatch.setattr(ai_service.time, "monotonic", lambda: clock["now"])

    def timeout_request(*_: object, **__: object) -> object:
        clock["now"] = 121.0
        raise providers.AiProviderError("ai_timeout")

    monkeypatch.setattr(providers, "_request_json", timeout_request)
    response = api_client.post(
        f"/api/adapters/{deadline_adapter['id']}/ai/assist",
        json=assist_body(),
    )
    assert response.status_code == 200, response.text
    records = _records()
    assert records[0]["event_type"] == "guard"
    assert records[0]["stop_reason"] == ai_service._STOP_DEADLINE
    assert records[0]["error_code"] == "ai_timeout"
    assert records[-1]["event_type"] == "request_terminal"
    assert records[-1]["status"] == "stopped"


def test_tool_argument_audit_uses_per_tool_whitelists_and_rejects_content_fields() -> None:
    query = "FULL_USER_PROMPT_DO_NOT_PERSIST"
    query_digest = hashlib.sha256(query.encode()).hexdigest()[:12]
    search = tool_audit.summarize_tool_arguments(
        "dlr_docs_search",
        json.dumps({"query": query, "limit": 2}),
        {"query": query, "limit": 2, "prompt": query},
    )
    assert search == {
        "query_length": len(query),
        "query_sha256": query_digest,
        "limit": 2,
    }
    assert query not in json.dumps(search)

    assert set(
        tool_audit.summarize_tool_arguments(
            "dlr_docs_list", '{"category":"runtime"}', {"category": "runtime"}
        )
    ) == {"category"}
    assert set(
        tool_audit.summarize_tool_arguments(
            "dlr_docs_read",
            '{"doc_id":"runtime-contract-python"}',
            {"doc_id": "runtime-contract-python"},
        )
    ) == {"doc_id"}
    assert set(
        tool_audit.summarize_tool_arguments(
            "list_knowledge_bases", '{"source":"ima"}', {"source": "ima"}
        )
    ) == {"source"}
    assert set(
        tool_audit.summarize_tool_arguments(
            "search_knowledge",
            "{}",
            {
                "source": "ima",
                "knowledge_base_id": "kb-1",
                "query": query,
                "limit": 5,
                "attachment": "must-not-persist",
            },
        )
    ) == {"source", "knowledge_base_id", "query_length", "query_sha256", "limit"}
    assert set(
        tool_audit.summarize_tool_arguments(
            "read_knowledge",
            "{}",
            {"source": "ima", "item_id": "item-1", "reasoning": "must-not-persist"},
        )
    ) == {"source", "item_id"}

    invalid = '{"query":"SECRET","unknown":true}'
    assert tool_audit.summarize_tool_arguments("dlr_docs_search", invalid, None) == {
        "raw_bytes": len(invalid.encode())
    }
    assert tool_audit.summarize_tool_arguments("unknown_tool", invalid, {}) == {
        "raw_bytes": len(invalid.encode())
    }

    audit_parameters = set(
        inspect.signature(tool_audit.AiToolAuditTrail.record_tool_attempt).parameters
    )
    forbidden = {
        "result",
        "result_summary",
        "model_content",
        "prompt",
        "attachment",
        "source_code",
        "reasoning",
        "raw_response",
    }
    assert audit_parameters.isdisjoint(forbidden)
    trail = tool_audit.AiToolAuditTrail(
        correlation=tool_audit.new_request_correlation(None),
        adapter_id=1,
    )
    with pytest.raises(TypeError):
        trail.record_tool_attempt(  # type: ignore[call-arg]
            round_index=1,
            tool_name="dlr_docs_list",
            raw_arguments="{}",
            validated_arguments={},
            status="success",
            duration_ms=1,
            result_size=1,
            result_truncated=False,
            prompt=query,
        )


def test_audit_sensitive_values_never_reach_current_or_rotated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_for_test(tmp_path, monkeypatch, max_bytes=350, backup_count=20)
    sensitive_values = (
        "PLACEHOLDER_SECRET_DO_NOT_PERSIST",
        "sk-PLACEHOLDERTOKEN1234567890",
        "session=PLACEHOLDER_COOKIE_DO_NOT_PERSIST",
        "FULL_USER_PROMPT_DO_NOT_PERSIST",
        "ATTACHMENT_BODY_DO_NOT_PERSIST",
        "def sensitive_adapter(context, input): return input",
        "REASONING_BODY_DO_NOT_PERSIST",
        "RAW_PROVIDER_RESPONSE_DO_NOT_PERSIST",
    )
    trail = tool_audit.AiToolAuditTrail(
        correlation=tool_audit.AiAuditCorrelation(
            request_id="2d7c132d-2a4f-4c22-a3cf-27f39a167e99",
            conversation_id="3b241101-e2bb-4255-8caf-4136c566a962",
        ),
        adapter_id=7,
    )
    attempts = (
        (
            "dlr_docs_list",
            {"category": sensitive_values[0]},
        ),
        (
            "dlr_docs_search",
            {"query": " ".join(sensitive_values), "limit": 3},
        ),
        (
            "dlr_docs_read",
            {"doc_id": sensitive_values[5]},
        ),
        (
            "list_knowledge_bases",
            {"source": sensitive_values[1]},
        ),
        (
            "search_knowledge",
            {
                "source": sensitive_values[2],
                "knowledge_base_id": sensitive_values[4],
                "query": sensitive_values[3],
                "limit": 5,
            },
        ),
        (
            "read_knowledge",
            {"source": sensitive_values[7], "item_id": sensitive_values[6]},
        ),
    )
    for round_index, (tool_name, validated) in enumerate(attempts, start=1):
        trail.record_tool_attempt(
            round_index=round_index,
            tool_name=tool_name,
            raw_arguments=json.dumps(validated),
            validated_arguments=validated,
            status="success",
            duration_ms=round_index,
            result_size=100,
            result_truncated=False,
            redact_values=sensitive_values,
        )

    invalid_raw = json.dumps({"query": " ".join(sensitive_values), "unknown": True})
    trail.record_tool_attempt(
        round_index=7,
        tool_name="dlr_docs_search",
        raw_arguments=invalid_raw,
        validated_arguments=None,
        status="error",
        duration_ms=0,
        result_size=0,
        result_truncated=False,
        error_code=tools_service.CODE_ARGS_INVALID,
        redact_values=sensitive_values,
    )
    trail.record_guard(round_index=7, stop_reason="assist_deadline", error_code="ai_timeout")
    trail.finish(status="stopped")
    tool_audit.close_ai_tool_audit_logging()

    current = tool_audit.audit_log_path()
    files = sorted(current.parent.glob(f"{current.name}*"))
    assert len(files) > 1
    all_records: list[dict[str, Any]] = []
    combined = ""
    for path in files:
        raw_lines = path.read_bytes().splitlines(keepends=True)
        assert raw_lines and all(line.endswith(b"\n") for line in raw_lines)
        all_records.extend(json.loads(line) for line in raw_lines)
        combined += path.read_text(encoding="utf-8")

    assert len(all_records) == len(attempts) + 3
    for sensitive in sensitive_values:
        assert sensitive not in combined
    for forbidden_field in (
        "result_summary",
        "model_content",
        "prompt",
        "attachment",
        "source_code",
        "reasoning",
        "raw_response",
    ):
        assert f'"{forbidden_field}"' not in combined
    invalid_event = next(
        record
        for record in all_records
        if record.get("error_code") == tools_service.CODE_ARGS_INVALID
    )
    assert invalid_event["args_summary"] == {"raw_bytes": len(invalid_raw.encode())}
