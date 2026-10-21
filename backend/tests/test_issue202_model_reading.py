"""Exact capabilities, true media transport, scoped reads and actual wire budgets."""

import base64
import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

from dlr.control.ai import context_budget, providers, tools
from dlr.control.ai.attachment_reading import AttachmentMedia, AttachmentReadResult
from dlr.control.schemas.ai import AiSettingDraft

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def media(*, pdf: bool = False, ref: str = "ref-1", page: int = 3) -> AttachmentMedia:
    raw = b"%PDF-1.4 single-page" if pdf else PNG
    return AttachmentMedia(
        content_type="application/pdf" if pdf else "image/png",
        data_base64=base64.b64encode(raw).decode(),
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        attachment_ref=ref,
        page=page,
        width=None if pdf else 1,
        height=None if pdf else 1,
        page_count=1 if pdf else None,
    )


@pytest.mark.parametrize(
    "provider,url,model,image,pdf,source",
    [
        ("minimax", "https://api.minimax.cn", "MiniMax-M3", True, False, "verified_model_endpoint"),
        ("minimax", "https://api.minimax.cn", "MiniMax-M2.7", False, False, "unknown"),
        ("minimax", "https://api.minimax.cn/proxy", "MiniMax-M3", False, False, "unknown"),
        ("kimi", "https://api.kimi.com/coding", "k3-256k", True, False, "verified_model_endpoint"),
        ("kimi", "https://api.moonshot.cn", "k3-256k", False, False, "unknown"),
        (
            "openai",
            "https://api.openai.com/v1",
            "gpt-4o-2024-08-06",
            True,
            True,
            "verified_model_endpoint",
        ),
        ("openai", "https://api.openai.com/custom", "gpt-4o-2024-08-06", False, False, "unknown"),
        (
            "anthropic",
            "https://api.anthropic.com",
            "claude-sonnet-5-5",
            True,
            True,
            "verified_model_endpoint",
        ),
        (
            "gemini",
            "https://generativelanguage.googleapis.com",
            "gemini-2.5-pro",
            True,
            True,
            "verified_model_endpoint",
        ),
        ("gemini", "https://generativelanguage.googleapis.com", "unknown", False, False, "unknown"),
    ],
)
def test_actual_capability(provider, url, model, image, pdf, source) -> None:
    setting = AiSettingDraft(provider=provider, base_url=url, model=model)
    adapter = providers.resolve_attachment_adapter(setting, providers.get_provider(provider))
    assert (adapter.images_native, adapter.native_pdf, adapter.capability_source) == (
        image,
        pdf,
        source,
    )
    assert setting.model == model and setting.base_url == url


def test_custom_file_declaration_does_not_authorize_native_pdf() -> None:
    setting = AiSettingDraft(
        provider="custom_openai_compatible", model="vision", base_url="https://custom.invalid"
    )
    custom = providers.custom_provider_adapter(
        "anthropic", images_native=True, files_native=True, tools_supported=True
    )
    adapter = providers.resolve_attachment_adapter(setting, custom)
    assert adapter.images_native and adapter.files_native and not adapter.native_pdf
    assert adapter.capability_source == "custom_declared"
    with pytest.raises(providers.AiProviderError) as error:
        providers._build_protocol_payload(
            setting,
            [{"role": "user", "content": providers.media_content_parts((media(pdf=True),))}],
            structured=True,
            adapter=adapter,
        )
    assert error.value.code == "ai_attachment_type_unsupported"


@pytest.mark.parametrize(
    "provider,url,model",
    [
        ("openai", "https://api.openai.com", "gpt-4o-2024-08-06"),
        ("anthropic", "https://api.anthropic.com", "claude-sonnet-5-5"),
        ("gemini", "https://generativelanguage.googleapis.com", "gemini-2.5-pro"),
    ],
)
@pytest.mark.parametrize("pdf", [False, True])
def test_tool_media_is_transmitted_as_true_parts(provider, url, model, pdf) -> None:
    setting = AiSettingDraft(provider=provider, base_url=url, model=model)
    adapter = providers.resolve_output_adapter(
        setting, providers.resolve_attachment_adapter(setting, providers.get_provider(provider))
    )
    item = media(pdf=pdf)
    result, supplement = providers.tool_result_messages(
        "call-1", "read_attachment_page", '{"page":3}', (item,), adapter
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_attachment_page", "arguments": "{}"},
                }
            ],
        },
        result,
    ]
    if supplement is not None:
        messages.append(supplement)
    payload = providers._build_protocol_payload(setting, messages, structured=True, adapter=adapter)
    serialized = json.dumps(payload)
    assert item.data_base64 in serialized
    assert (
        "dlr_media" not in serialized and "attachment_ref" in serialized and "call-1" in serialized
    )
    if provider == "openai":
        assert payload["messages"][1]["role"] == "tool"
        assert item.data_base64 not in payload["messages"][1]["content"]
        assert payload["messages"][2]["content"][1]["type"] == ("file" if pdf else "image_url")
    elif provider == "anthropic":
        assert supplement is None
        block = payload["messages"][1]["content"][0]
        assert block["type"] == "tool_result" and block["tool_use_id"] == "call-1"
        assert block["content"][-1]["type"] == ("document" if pdf else "image")
    else:
        assert (
            payload["contents"][1]["parts"][0]["functionResponse"]["name"] == "read_attachment_page"
        )
        assert payload["contents"][2]["parts"][1]["inlineData"]["mimeType"] == item.content_type
    assert providers.assist_payload_size(setting, messages, None, adapter=adapter) == len(
        json.dumps(payload, ensure_ascii=False).encode()
    )


class Reader:
    def __init__(self) -> None:
        self.calls = []

    def overview(self):
        return {"attachments": [{"attachment_ref": "ref-1", "page_count": 3}]}

    def read_page(self, ref, page, view, *, timeout_seconds):
        self.calls.append((ref, page, view, timeout_seconds))
        return AttachmentReadResult(
            {"page": page, "coverage": "partial"}, (media(ref=ref, page=page, pdf=view == "pdf"),)
        )

    def read_sheet(self, ref, sheet, range_a1, *, timeout_seconds):
        self.calls.append((ref, sheet, range_a1, timeout_seconds))
        return AttachmentReadResult(
            {"sheet": sheet, "range": range_a1, "raw": 0.25, "number_format": "0%"}
        )


def tool_context(reader, *, remaining=30, native_pdf=True):
    return tools.ToolExecutionContext(
        attachment_reader=reader,
        attachment_refs=frozenset({"ref-1"}),
        hard_deadline=time.monotonic() + remaining,
        images_native=True,
        native_pdf=native_pdf,
    )


@pytest.mark.parametrize(
    "args,code",
    [
        ({"attachment_ref": "../outside", "page": 3}, "ai_tool_args_invalid"),
        ({"attachment_ref": "not-this-request", "page": 3}, "ai_attachment_scope_invalid"),
        ({"attachment_ref": "ref-1", "page": 0}, "ai_tool_args_invalid"),
        ({"attachment_ref": "ref-1", "page": True}, "ai_tool_args_invalid"),
        ({"attachment_ref": "ref-1", "page": 3, "view": "shell"}, "ai_tool_args_invalid"),
        (
            {"attachment_ref": "ref-1", "page": 3, "url": "https://outside.invalid"},
            "ai_tool_args_invalid",
        ),
    ],
)
def test_scoped_page_tool_rejects_before_reader(args, code) -> None:
    reader = Reader()
    result = tools.execute_tool_call(
        "read_attachment_page", json.dumps(args), None, context=tool_context(reader)
    )
    assert result.error_code == code and not reader.calls


def test_sheet_range_deadline_and_separate_media() -> None:
    reader = Reader()
    context = tool_context(reader, remaining=2)
    result = tools.execute_tool_call(
        "read_attachment_page", '{"attachment_ref":"ref-1","page":3}', None, context=context
    )
    assert result.status == "success" and len(result.media) == 1
    assert result.media[0].data_base64 not in result.model_content + result.result_summary
    assert 0 < reader.calls[0][-1] <= 2
    invalid = tools.execute_tool_call(
        "read_attachment_sheet",
        '{"attachment_ref":"ref-1","sheet":"Data","range":"A1:B101"}',
        None,
        context=context,
    )
    assert invalid.error_code == "ai_tool_args_invalid" and len(reader.calls) == 1
    expired = tools.execute_tool_call(
        "read_attachment_page",
        '{"attachment_ref":"ref-1","page":3}',
        None,
        context=tool_context(reader, remaining=-1),
    )
    assert expired.error_code == "ai_tool_timeout" and len(reader.calls) == 1
    pdf = tools.execute_tool_call(
        "read_attachment_page",
        '{"attachment_ref":"ref-1","page":3,"view":"pdf"}',
        None,
        context=tool_context(reader, native_pdf=False),
    )
    assert pdf.error_code == "ai_attachment_type_unsupported" and len(reader.calls) == 1


def test_attachment_tools_are_absent_without_request_reader() -> None:
    assert all(
        item["function"]["name"] not in tools._ATTACHMENT_TOOLS for item in tools.tools_payload()
    )
    names = {item["function"]["name"] for item in tools.tools_payload(include_attachments=True)}
    assert names >= tools._ATTACHMENT_TOOLS


def test_budget_includes_schema_media_and_exact_bytes_and_keeps_tool_group() -> None:
    draft = AiSettingDraft(
        provider="openai", base_url="https://api.openai.com", model="gpt-4o-2024-08-06"
    )
    adapter = providers.resolve_output_adapter(
        draft, providers.resolve_attachment_adapter(draft, providers.get_provider("openai"))
    )
    result, supplement = providers.tool_result_messages(
        "call-1", "read_attachment_page", "page 3", (media(),), adapter
    )
    messages = [
        {
            "role": "user",
            "content": context_budget.REQUEST_PREFIX + '{"USER_REQUEST":"read page 3"}',
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_attachment_page", "arguments": "{}"},
                }
            ],
        },
        result,
        supplement,
    ]
    original_length = len(messages)
    budget = context_budget.prepare_call(draft, messages, None, adapter=adapter)
    assert budget.fits and budget.diagnostics.schema_tokens > 0
    assert budget.diagnostics.image_count == 1
    assert budget.diagnostics.pdf_page_count == 0
    assert budget.diagnostics.media_bytes == media().size_bytes
    assert budget.diagnostics.payload_bytes == 0  # No SDK request exists at this boundary.
    assert budget.diagnostics.estimated_payload_bytes == context_budget.estimated_payload_bytes(
        draft, messages, None
    )
    assert (
        context_budget.estimate_tokens(messages, None, adapter=adapter)
        > context_budget.IMAGE_RESERVE_TOKENS
    )
    rejected = context_budget.prepare_call(
        draft, messages, None, adapter=replace(adapter, max_request_bytes=128)
    )
    assert not rejected.fits and len(messages) == original_length


def test_cold_page_reader_has_30s_without_extending_other_tools(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: clock[0])

    class ColdReader(Reader):
        def read_page(self, ref, page, view, *, timeout_seconds):
            assert timeout_seconds == 30.0
            clock[0] += 16
            return AttachmentReadResult({"text": "scanned page", "coverage": "partial"})

    reader = ColdReader()
    context = tools.ToolExecutionContext(
        attachment_reader=reader,
        attachment_refs=frozenset({"ref-1"}),
        hard_deadline=100,
        attachment_deadline=60,
    )
    result = tools.execute_tool_call(
        "read_attachment_page",
        '{"attachment_ref":"ref-1","page":3,"view":"text"}',
        None,
        context=context,
    )
    assert result.status == "success"
    assert tools._TOOLS["read_attachment_page"].timeout_seconds == 30
    assert tools._TOOLS["read_attachment_sheet"].timeout_seconds is None
    assert tools._TOOLS["dlr_docs_list"].timeout_seconds is None


def test_attachment_truncation_keeps_source_and_clamps_whole_cells() -> None:
    reader = Reader()
    reader.read_sheet = lambda ref, sheet, range_a1, **kwargs: AttachmentReadResult(
        {"cells": [{"coordinate": f"A{i}", "raw": "x" * 80} for i in range(1, 201)]}
    )
    result = tools.execute_tool_call(
        "read_attachment_sheet",
        '{"attachment_ref":"ref-1","sheet":"Data","range":"A1:A200"}',
        None,
        context=tool_context(reader),
    )
    data = json.loads(result.model_content)
    assert result.status == "success" and result.result_truncated
    assert data["attachment_ref"] == "ref-1" and data["range"] == "A1:A200"
    assert data["coverage"] == "partial" and data["omitted_cells"] > 0
    assert len(result.model_content) <= tools.MAX_TOOL_RESULT_CHARS


@pytest.mark.parametrize("correct_final", [False, True])
def test_service_reader_hook_and_selected_capability(
    api_client, monkeypatch, session_factory, correct_final
) -> None:
    from dlr.common.config import settings
    from dlr.control.services import ai
    from test_ai import assist_body, configure, create_adapter, fake_chat_response

    adapter = create_adapter(api_client, "issue202-reader-hook")
    configure(api_client, provider="minimax", base_url="https://api.minimax.cn", model="MiniMax-M3")
    reader = Reader()
    frozen_ref = "attachment-test-1"
    reader.overview = lambda: {
        "attachments": [
            {
                "attachment_ref": frozen_ref,
                "page_count": 3,
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "representation_revision": "v1",
                "coverage": "partial",
            }
        ]
    }
    captured = []
    timeouts = []
    hard_deadlines = []
    original_budget = ai._audit_provider_budget

    def audit_budget(audit, state, budget, deadline, actual_payload_bytes):
        hard_deadlines.append(state.hard_deadline)
        return original_budget(audit, state, budget, deadline, actual_payload_bytes)

    monkeypatch.setattr(ai, "_audit_provider_budget", audit_budget)

    def request(*args, **kwargs):
        payload = args[3]
        captured.append(payload)
        timeouts.append(kwargs["timeout_seconds"])
        assert 0 < timeouts[-1] <= hard_deadlines[-1] - time.monotonic() + 0.01
        if len(captured) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "read-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_attachment_page",
                                        "arguments": json.dumps(
                                            {
                                                "attachment_ref": frozen_ref,
                                                "page": 3,
                                                "view": "both",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        if correct_final and len(captured) == 2:
            return fake_chat_response("not valid JSON")
        return fake_chat_response(
            {
                "message": "Page 3 color relationship read",
                "candidate": None,
                "request_intent": "consultation",
            }
        )

    monkeypatch.setattr(providers, "_request_json", request)
    from dlr.control.api import ai as ai_api

    class TestStore:
        @contextmanager
        def reader(self, owner, adapter_id, scope, references, **kwargs):
            kwargs["permission_check"]()
            yield reader

    monkeypatch.setattr(ai_api, "get_attachment_store", TestStore)
    body = assist_body()
    body.update(
        message="Explain page 3 chart colors only",
        attachment_scope_id="8cb28920-09b5-445b-861b-3b20905d48e3",
        attachment_refs=[
            {
                "attachment_ref": frozen_ref,
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "representation_revision": "v1",
            }
        ],
    )
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["task_status"] == "answered" and response.json()["candidate"] is None
    assert len(captured) == (3 if correct_final else 2)
    assert reader.calls[0][:3] == (frozen_ref, 3, "both")
    assert len(reader.calls) == 1
    assert len(set(hard_deadlines)) == 1
    if correct_final:
        assert "tools" not in captured[2]
        assert "not valid JSON" not in json.dumps(captured[2])
        assert media().data_base64 in json.dumps(captured[2])
    assert media().data_base64 not in response.text
    first = json.dumps(captured[0])
    assert "attachment_directory" in first and frozen_ref in first
    assert "read_attachment_page" in first
    followup = captured[1]["messages"]
    tool_idx = next(i for i, m in enumerate(followup) if m["role"] == "tool")
    assert followup[tool_idx]["tool_call_id"] == "read-1"
    assert media().data_base64 not in followup[tool_idx]["content"]
    assert followup[tool_idx + 1]["content"][1]["type"] == "image_url"
    with session_factory() as session:
        selected = ai.attachment_capabilities(session).selected
        assert selected is not None and selected.images_native and not selected.native_pdf
        assert selected.capability_source == "verified_model_endpoint"
    assert ai.attachment_capabilities().selected is None
    monkeypatch.setattr(settings, "ai_document_converter_url", "http://converter.invalid")
    assert ai.attachment_capabilities().prepared_attachments_enabled
    monkeypatch.setattr(settings, "ai_document_converter_url", "")
    assert not ai.attachment_capabilities().prepared_attachments_enabled


def test_refs_without_reader_fail_closed_before_provider(
    api_client, monkeypatch, session_factory
) -> None:
    from fastapi import HTTPException

    from dlr.common.config import settings
    from dlr.control.schemas.ai import AiAssistRequest
    from dlr.control.services import ai
    from test_ai import assist_body, create_adapter

    adapter = create_adapter(api_client, "issue202-no-reader")
    calls = []
    monkeypatch.setattr(providers, "_request_json", lambda *a, **kw: calls.append(a))
    body = assist_body()
    body.update(
        attachment_scope_id="8cb28920-09b5-445b-861b-3b20905d48e3",
        attachment_refs=[
            {
                "attachment_ref": "attachment-test-1",
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "representation_revision": "v1",
            }
        ],
    )
    monkeypatch.setattr(settings, "ai_document_converter_url", None)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_attachment_converter_unavailable"
    with session_factory() as session, pytest.raises(HTTPException) as error:
        ai.assist(session, adapter["id"], AiAssistRequest.model_validate(body))
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "ai_attachment_reader_unavailable"
    assert calls == []
