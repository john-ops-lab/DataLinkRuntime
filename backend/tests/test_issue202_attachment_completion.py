"""Prepared attachment completion requires request-local successful content reads."""

import asyncio
import base64
import copy
import json
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import ai_sdk_fixtures
from dlr.control.ai import context_budget, providers, tool_audit, tools
from dlr.control.ai.attachment_documents import DocumentConversionError
from dlr.control.ai.attachment_reading import AttachmentReadResult
from dlr.control.ai.attachment_store import AiAttachmentStore
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.schemas.ai import AiAssistRequest, AiAttachment, AiSettingDraft
from dlr.control.services import ai

REF = "frozen_reference_123456"
OTHER_REF = "outside_reference_123456"
FORGED = "I read the original workbook: the total is 20800."
REAL_VALIDATE_SETTING = ai._validate_setting


def final(message=FORGED, *, candidate=False, intent="consultation"):
    return json.dumps(
        {
            "message": message,
            "candidate": (
                {"summary": "updated", "code": "return input", "required_secret_keys": []}
                if candidate
                else None
            ),
            "request_intent": intent,
        }
    ), None


def call(name="read_attachment_sheet", **arguments):
    if not arguments:
        arguments = {"attachment_ref": REF, "sheet": "Sheet1", "range": "A76:D81"}
    return None, [providers.NormalizedToolCall("call-1", name, json.dumps(arguments))]


class Reader:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def overview(self):
        return {"attachments": [{"attachment_ref": REF, "page_count": 1}]}

    def read_sheet(self, reference, sheet, range_a1, *, timeout_seconds):
        self.calls.append((reference, sheet, range_a1, timeout_seconds))
        if self.error:
            raise DocumentConversionError(self.error)
        return AttachmentReadResult({"sheet": sheet, "range": range_a1, "sum": 46500})

    def read_page(self, reference, page, view, *, timeout_seconds):
        self.calls.append((reference, page, view, timeout_seconds))
        if self.error:
            raise DocumentConversionError(self.error)
        return AttachmentReadResult({"page": page, "text": "actual original content"})


@pytest.fixture
def harness(monkeypatch):
    draft = AiSettingDraft(
        provider="minimax", model="MiniMax-M3", base_url="https://api.minimax.cn"
    )
    adapter = providers.resolve_output_adapter(
        draft, providers.resolve_attachment_adapter(draft, providers.get_provider("minimax"))
    )
    state = SimpleNamespace(
        calls=[],
        audits=[],
        states=[],
        purposes=[],
        adapter=adapter,
        knowledge_calls=[],
        knowledge_accepts=[],
        knowledge_records=[],
    )
    monkeypatch.setattr(
        ai.adapter_service, "get_adapter", lambda *_: SimpleNamespace(id=1, language="python")
    )
    monkeypatch.setattr(ai, "get_setting", lambda *_: object())
    monkeypatch.setattr(ai, "_setting_draft", lambda *_: draft)
    monkeypatch.setattr(ai, "_validate_setting", lambda *_, **__: state.adapter)
    monkeypatch.setattr(ai, "_resolve_api_key", lambda *_: None)
    monkeypatch.setattr(ai.locale_service, "get_system_locale", lambda *_: "en")
    monkeypatch.setattr(ai.settings, "ai_assist_total_timeout_seconds", 150)
    monkeypatch.setattr(tool_audit, "_emit_record", state.audits.append)
    monkeypatch.setattr(
        ai.knowledge_source_service_config, "knowledge_search_capability", lambda *_: (True, None)
    )
    monkeypatch.setattr(ai.knowledge_service, "redact_values_for", lambda *_: ())
    # These pure domain trajectories use no database. The production resource
    # factory is verified separately against real PostgreSQL and Reader pins.
    monkeypatch.setattr(
        ai,
        "_tool_resources",
        lambda _session, context, **_kwargs: SimpleNamespace(
            template=context, open=lambda: nullcontext(context)
        ),
    )

    def knowledge_result(name, result):
        def handler(*args):
            state.knowledge_calls.append(name)
            return result

        return handler

    monkeypatch.setattr(
        tools.knowledge,
        "list_knowledge_bases",
        knowledge_result(
            "list_knowledge_bases",
            {"tool": "list_knowledge_bases", "items": [{"id": "kb-1", "source": "ima:v1:kb-1"}]},
        ),
    )
    monkeypatch.setattr(
        tools.knowledge,
        "search_knowledge",
        knowledge_result(
            "search_knowledge",
            {
                "tool": "search_knowledge",
                "returned_matches": 1,
                "is_end": True,
                "items": [
                    {"id": "item-1", "source": "ima:v1:item-1", "summary": "actual KB summary"}
                ],
            },
        ),
    )
    monkeypatch.setattr(
        tools.knowledge,
        "read_knowledge",
        knowledge_result(
            "read_knowledge",
            {
                "tool": "read_knowledge",
                "item": {"id": "item-1", "source": "ima:v1:item-1", "content": "actual KB content"},
            },
        ),
    )
    original_accepts = ai._KnowledgeRetrievalState.accepts_call
    original_record = ai._KnowledgeRetrievalState.record_execution

    def accepts(retrieval, name, arguments):
        state.knowledge_accepts.append(name)
        return original_accepts(retrieval, name, arguments)

    def record(retrieval, arguments, execution):
        state.knowledge_records.append(execution.tool_name)
        return original_record(retrieval, arguments, execution)

    monkeypatch.setattr(ai._KnowledgeRetrievalState, "accepts_call", accepts)
    monkeypatch.setattr(ai._KnowledgeRetrievalState, "record_execution", record)

    def messages(*args, **kwargs):
        return [
            {"role": "system", "content": "Original strict AiModelOutput JSON contract"},
            {"role": "user", "content": args[4].message},
            {"role": "user", "content": json.dumps(kwargs["attachment_overview"])},
        ]

    monkeypatch.setattr(ai, "_assist_messages", messages)
    original_prepare = context_budget.prepare_call

    def prepare(*args, **kwargs):
        state.purposes.append(kwargs["purpose"])
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(context_budget, "prepare_call", prepare)
    original_call = ai._prepare_assist

    def audited(*args, **kwargs):
        state.states.append(args[4])
        return original_call(*args, **kwargs)

    monkeypatch.setattr(ai, "_prepare_assist", audited)

    def run(responses, *, prepared=True, inline=False, reader=None, intent="auto", knowledge=False):
        pending = iter(responses)

        def chat(setting, key, messages, **kwargs):
            state.calls.append({"messages": copy.deepcopy(messages), **kwargs})
            answer = next(pending)
            if callable(answer):
                answer = answer()
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(ai_sdk_fixtures, "round_reply", chat)
        body = {
            "message": "Read the attached workbook and calculate its total.",
            "task_intent": intent,
            "knowledge_search_enabled": knowledge,
            "working_copy": {"code": "return input", "requirements": "", "runtime_config": {}},
        }
        if prepared:
            body.update(
                attachment_scope_id=uuid4(),
                attachment_refs=[
                    {
                        "attachment_ref": REF,
                        "sha256": "a" * 64,
                        "representation_revision": "test-v1",
                    }
                ],
            )
        if inline:
            body["attachments"] = [
                {
                    "filename": "legacy.txt",
                    "content_type": "text/plain",
                    "data_base64": base64.b64encode(b"legacy content").decode(),
                }
            ]
        state.reader = reader or Reader()
        return ai.assist(
            None, 1, AiAssistRequest(**body), attachment_reader=state.reader if prepared else None
        )

    state.run = run
    return state


def knowledge_calls():
    return [
        providers.NormalizedToolCall("kb-list", "list_knowledge_bases", '{"source":"ima"}'),
        providers.NormalizedToolCall(
            "kb-search",
            "search_knowledge",
            '{"source":"ima","knowledge_base_id":"kb-1","query":"total"}',
        ),
        providers.NormalizedToolCall(
            "kb-read", "read_knowledge", '{"source":"ima","item_id":"item-1"}'
        ),
    ]


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_prepared_and_knowledge_complete_independent_chains(harness, position):
    batch = knowledge_calls()
    batch[position:position] = call()[1]
    response = harness.run(
        [(None, batch), final("source-backed combined answer")],
        knowledge=True,
        intent="consultation",
    )
    assert response.task_status == "answered" and len(harness.reader.calls) == 1
    assert len(response.tool_calls) == 4 and all(
        item.status == "success" for item in response.tool_calls
    )
    assert harness.knowledge_calls == ["list_knowledge_bases", "search_knowledge", "read_knowledge"]
    assert harness.knowledge_accepts == harness.knowledge_calls
    assert harness.knowledge_records == harness.knowledge_calls
    assert not harness.states[0].prepared_knowledge_incomplete


def test_knowledge_success_cannot_unlock_a_failed_attachment_read(harness):
    response = harness.run(
        [(None, [*call()[1], *knowledge_calls()]), final(candidate=True)],
        reader=Reader("ai_attachment_cancelled"),
        knowledge=True,
        intent="consultation",
    )
    assert response.task_status == "incomplete" and response.candidate is None
    assert harness.knowledge_records == [
        "list_knowledge_bases",
        "search_knowledge",
        "read_knowledge",
    ]
    assert len(harness.reader.calls) == 1 and not harness.states[0].attachment_read_correction
    assert len(harness.calls) == 2


def test_only_knowledge_success_cannot_bypass_attachment_read_gate(harness):
    response = harness.run(
        [(None, knowledge_calls()), final(), final()], knowledge=True, intent="consultation"
    )
    assert response.task_status == "incomplete" and response.candidate is None
    assert harness.states[0].successful_attachment_reads == 0
    assert harness.states[0].attachment_read_correction and len(harness.calls) == 3


def test_only_attachment_success_cannot_bypass_required_knowledge_chain(harness):
    response = harness.run(
        [call(), final(), final(), final(), final("finalizer supplement", candidate=True)],
        knowledge=True,
        intent="consultation",
    )
    assert harness.states[0].successful_attachment_reads == 1
    assert not harness.knowledge_calls
    assert harness.states[0].prepared_knowledge_incomplete
    assert response.task_status == "incomplete" and response.candidate is None
    assert "required retrieval order was not completed" in response.message


def test_wrong_knowledge_order_stays_blocked_with_a_successful_attachment(harness):
    wrong = knowledge_calls()[1:]
    response = harness.run(
        [(None, [*call()[1], *wrong]), final(), final("finalizer supplement", candidate=True)],
        knowledge=True,
        intent="consultation",
    )
    assert harness.states[0].successful_attachment_reads == 1 and not harness.knowledge_calls
    assert [item.error_code for item in response.tool_calls[1:]] == [
        tools.CODE_KNOWLEDGE_SEQUENCE
    ] * 2
    assert response.task_status == "incomplete" and response.candidate is None


@pytest.mark.parametrize("prepared", [False, True])
def test_attachment_progress_preserves_pending_knowledge_order_correction(harness, prepared):
    response = harness.run(
        [
            (None, [knowledge_calls()[1], *(call()[1] if prepared else [])]),
            (None, knowledge_calls()),
            final("both evidence channels complete"),
        ],
        prepared=prepared,
        knowledge=True,
        intent="consultation",
    )
    assert response.tool_calls[0].error_code == tools.CODE_KNOWLEDGE_SEQUENCE
    assert all(item.status == "success" for item in response.tool_calls[1:])
    assert harness.knowledge_calls == ["list_knowledge_bases", "search_knowledge", "read_knowledge"]
    assert len(harness.reader.calls) == int(prepared)
    assert harness.states[0].stop_reason is None
    assert any(
        "server-enforced stage need_list" in str(item.get("content"))
        for item in harness.calls[1]["messages"]
    )
    assert response.task_status == "answered"


@pytest.mark.parametrize("prepared", [False, True])
def test_sequence_corrected_search_still_executes_only_once(harness, prepared):
    response = harness.run(
        [
            (None, [knowledge_calls()[1], *(call()[1] if prepared else [])]),
            (None, [*knowledge_calls(), knowledge_calls()[1]]),
            final("duplicate finalizer"),
        ],
        prepared=prepared,
        knowledge=True,
        intent="consultation",
    )
    assert response.tool_calls[0].error_code == tools.CODE_KNOWLEDGE_SEQUENCE
    assert response.tool_calls[-1].error_code == tools.CODE_DUPLICATE
    assert harness.knowledge_calls == ["list_knowledge_bases", "search_knowledge", "read_knowledge"]
    assert len(harness.reader.calls) == int(prepared)
    assert harness.states[0].stop_reason == ai._STOP_DUPLICATE
    assert response.candidate is None


def test_existing_attachment_free_knowledge_chain_keeps_its_contract(harness):
    response = harness.run(
        [(None, knowledge_calls()), final("KB answer")],
        prepared=False,
        knowledge=True,
        intent="consultation",
    )
    assert response.task_status == "answered" and not harness.states[0].attachment_read_required
    assert harness.knowledge_records == [
        "list_knowledge_bases",
        "search_knowledge",
        "read_knowledge",
    ]


def test_attachment_read_does_not_bypass_required_second_knowledge_base(harness, monkeypatch):
    monkeypatch.setattr(
        tools.knowledge,
        "list_knowledge_bases",
        lambda *_: {
            "tool": "list_knowledge_bases",
            "items": [
                {"id": "kb-1", "source": "ima:v1:kb-1"},
                {"id": "kb-2", "source": "ima:v1:kb-2"},
            ],
        },
    )
    response = harness.run(
        [
            (None, [*call()[1], *knowledge_calls()[:2]]),
            final(),
            final(),
            final(),
            final("finalizer"),
        ],
        knowledge=True,
        intent="consultation",
    )
    assert harness.states[0].successful_attachment_reads == 1
    assert harness.states[0].prepared_knowledge_incomplete
    assert response.task_status == "incomplete" and response.candidate is None


def test_optional_knowledge_fulltext_failure_keeps_valid_summary_semantics(harness, monkeypatch):
    def failed(*_):
        raise tools.knowledge.KnowledgeSourceError("ks_read_failed", "source failed")

    monkeypatch.setattr(tools.knowledge, "read_knowledge", failed)
    response = harness.run(
        [(None, [*call()[1], *knowledge_calls()]), final("summary-backed answer")],
        knowledge=True,
        intent="consultation",
    )
    assert harness.states[0].successful_attachment_reads == 1
    assert not harness.states[0].prepared_knowledge_incomplete
    assert response.task_status == "answered" and response.candidate is None
    assert response.tool_calls[-1].error_code == "ks_read_failed"


def test_non_attachment_tools_keep_old_knowledge_order_restriction(harness):
    batch = [providers.NormalizedToolCall("docs", "dlr_docs_list", "{}"), *knowledge_calls()]
    response = harness.run([(None, batch), final("KB answer")], prepared=False, knowledge=True)
    assert response.tool_calls[0].error_code == tools.CODE_KNOWLEDGE_SEQUENCE
    assert harness.knowledge_records == [
        "list_knowledge_bases",
        "search_knowledge",
        "read_knowledge",
    ]


@pytest.mark.parametrize("candidate", [False, True])
def test_zero_reads_get_one_correction_then_server_incomplete(harness, candidate):
    response = harness.run([final(candidate=candidate), final(candidate=candidate)])
    assert len(harness.calls) == 2
    assert response.task_status == "incomplete" and response.candidate is None
    assert "No attachment content was successfully read" in response.message
    assert "20800" not in response.message
    assert not harness.reader.calls
    assert all(FORGED not in json.dumps(item["messages"]) for item in harness.calls)
    assert harness.purposes == ["assist_initial", "assist_followup"]
    assert harness.audits[-1]["status"] == "stopped"


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("read_attachment_sheet", {"attachment_ref": REF, "sheet": "Sheet1", "range": "A76:D81"}),
        ("read_attachment_page", {"attachment_ref": REF, "page": 1, "view": "text"}),
    ],
)
def test_correction_can_recover_with_actual_registered_content_read(harness, name, arguments):
    response = harness.run(
        [
            final(),
            call(name, **arguments),
            final("actual content read", candidate=True, intent="code_change"),
        ]
    )
    assert len(harness.calls) == 3 and len(harness.reader.calls) == 1
    assert harness.reader.calls[0][0] == REF
    assert response.task_status == "candidate_ready" and response.candidate is not None
    assert response.tool_calls[0].tool_name == name and response.tool_calls[0].status == "success"
    assert {id(state) for state in harness.states} == {id(harness.states[0])}
    assert harness.states[0].successful_attachment_reads == 1
    assert harness.states[0].tool_rounds == harness.states[0].total_tool_calls == 1
    assert all(item["hard_deadline"] == harness.calls[0]["hard_deadline"] for item in harness.calls)


def test_directory_list_success_does_not_count_as_content(harness):
    response = harness.run(
        [
            (None, [providers.NormalizedToolCall("list-1", "list_attachments", "{}")]),
            final(),
            final(),
        ]
    )
    assert response.task_status == "incomplete" and response.candidate is None
    assert response.tool_calls[0].status == "success"
    assert not harness.reader.calls
    assert harness.states[0].successful_attachment_reads == 0


def test_successful_directory_stopped_by_budget_never_calls_finalizer(harness, monkeypatch):
    monkeypatch.setattr(tools, "MAX_TOOL_ROUNDS", 1)
    response = harness.run(
        [(None, [providers.NormalizedToolCall("list-1", "list_attachments", "{}")])]
    )
    assert len(harness.calls) == 1
    assert response.tool_calls[0].status == "success"
    assert response.task_status == "incomplete" and response.candidate is None
    assert harness.states[0].successful_attachment_reads == 0


def test_read_correction_does_not_expand_total_tool_call_budget(harness, monkeypatch):
    monkeypatch.setattr(tools, "MAX_TOOL_CALLS_PER_ASSIST", 1)
    two_calls = call()[1] * 2
    response = harness.run([final(), (None, two_calls)])
    assert len(harness.calls) == 2 and not harness.reader.calls
    assert harness.states[0].total_tool_calls == 0
    assert harness.states[0].stop_reason == ai._STOP_CALL_BUDGET
    assert response.task_status == "incomplete" and response.candidate is None


@pytest.mark.parametrize(
    "error", ["ai_attachment_cancelled", "ai_attachment_expired", "ai_attachment_parse_failed"]
)
def test_failed_content_reads_cannot_complete_or_earn_new_correction(harness, error):
    response = harness.run([call(), final(candidate=True)], reader=Reader(error))
    assert len(harness.calls) == 2 and len(harness.reader.calls) == 1
    assert response.task_status == "incomplete" and response.candidate is None
    assert response.tool_calls[0].status == "error"
    assert harness.states[0].attachment_read_attempts == 1
    assert harness.states[0].successful_attachment_reads == 0
    assert not harness.states[0].attachment_read_correction


def test_non_frozen_reference_cannot_unlock_completion(harness):
    response = harness.run([call(attachment_ref=OTHER_REF, sheet="Sheet1", range="A1:B2"), final()])
    assert response.task_status == "incomplete" and not harness.reader.calls
    assert response.tool_calls[0].status == "error"
    assert harness.states[0].successful_attachment_reads == 0
    assert not harness.states[0].attachment_read_correction


@pytest.mark.parametrize("inline", [False, True])
def test_old_inline_and_no_attachment_consultations_are_unchanged(harness, inline):
    response = harness.run([final("ordinary answer")], prepared=False, inline=inline)
    assert response.message == "ordinary answer" and response.task_status == "answered"
    assert len(harness.calls) == 1 and not harness.states[0].attachment_read_required


@pytest.mark.parametrize("intent", ["consultation", "code_change"])
def test_tools_disabled_rejects_prepared_before_credentials_or_provider(
    harness, monkeypatch, intent
):
    harness.adapter = replace(harness.adapter, tools_supported=False)
    # Exercise the real setting validation chain: it must not decrypt a credential
    # before reporting that this model cannot read the prepared attachment.
    monkeypatch.setattr(ai, "_validate_setting", REAL_VALIDATE_SETTING)
    monkeypatch.setattr(ai, "_adapter_for_setting", lambda *_: harness.adapter)

    def unexpected_credentials(*_):
        pytest.fail("unsupported prepared requests must not resolve credentials")

    monkeypatch.setattr(ai, "_resolve_api_key", unexpected_credentials)
    with pytest.raises(HTTPException) as error:
        harness.run([], intent=intent)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "ai_attachment_tools_unsupported"
    assert harness.calls == [] and harness.reader.calls == []
    assert not any(item["event_type"] == "provider_result" for item in harness.audits)


@pytest.mark.parametrize("inline", [False, True])
def test_tools_disabled_keeps_ordinary_and_inline_consultation(harness, inline):
    harness.adapter = replace(harness.adapter, tools_supported=False)
    response = harness.run(
        [final("ordinary answer")], prepared=False, inline=inline, intent="consultation"
    )
    assert response.task_status == "answered" and response.candidate is None
    assert response.message == "ordinary answer"
    assert len(harness.calls) == 1 and harness.calls[0]["tools"] is None


def test_format_correction_is_followed_by_read_gate(harness):
    response = harness.run([("{broken", None), final(), final()])
    assert len(harness.calls) == 3
    assert harness.calls[1]["tools"] is None and harness.calls[2]["tools"] is not None
    assert harness.states[0].format_corrections == 1
    assert response.task_status == "incomplete" and "20800" not in response.message


def test_safety_error_is_not_repaired_as_missing_read(harness):
    with pytest.raises(AiResponseInvalid) as error:
        harness.run([('{"message":"\\ud800","candidate":null}', None)])
    assert error.value.stage == "unicode" and len(harness.calls) == 1


def test_correction_provider_failure_returns_safe_incomplete(harness):
    response = harness.run([final(), providers.AiProviderError("ai_provider_unreachable")])
    assert len(harness.calls) == 2
    assert response.task_status == "incomplete" and response.candidate is None
    assert "20800" not in response.message


def test_correction_does_not_reset_deadline(harness, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock[0])

    def late_answer():
        clock[0] = 121.0
        return final()

    response = harness.run([late_answer])
    # The initial answer already exhausted the original tool phase: no recovery.
    assert len(harness.calls) == 1 and response.task_status == "incomplete"
    assert not harness.states[0].attachment_read_correction


def test_read_recovery_keeps_configuration_safety_gate(harness):
    changed = json.loads(final(candidate=True)[0])
    changed["candidate"]["requirements"] = "injected-package"
    with pytest.raises(AiResponseInvalid) as error:
        harness.run([(json.dumps(changed), None)])
    assert error.value.stage == "candidate_configuration" and len(harness.calls) == 1


def test_correction_cannot_expand_context_budget(harness, monkeypatch):
    original = context_budget.prepare_call
    seen = []

    def budget(*args, **kwargs):
        result = original(*args, **kwargs)
        seen.append(kwargs["purpose"])
        return result if len(seen) == 1 else replace(result, fits=False)

    monkeypatch.setattr(context_budget, "prepare_call", budget)
    response = harness.run([final()])
    assert len(harness.calls) == 1 and len(seen) == 2
    assert response.task_status == "incomplete" and response.candidate is None


def test_closed_scope_reason_is_distinct_from_active_cancellation(tmp_path):
    from test_issue202_attachment_store import PNG, Converter

    converter = Converter()
    store = AiAttachmentStore(tmp_path / "closed-scope", converter)
    scope = uuid4()
    attachment = AiAttachment(
        filename="image.png", content_type="image/png", data_base64=base64.b64encode(PNG).decode()
    )
    store.release(("account", 1), 1, scope)
    with pytest.raises(DocumentConversionError) as closed:
        store.prepare(("account", 1), 1, scope, attachment, deadline=time.monotonic() + 30)
    assert closed.value.code == "ai_attachment_cancelled"
    assert closed.value.reason == "scope_closed" and converter.calls == 0
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DocumentConversionError) as ordinary:
        store.prepare(
            ("account", 1),
            1,
            uuid4(),
            attachment,
            deadline=time.monotonic() + 30,
            cancel_event=cancel,
        )
    assert ordinary.value.code == "ai_attachment_cancelled" and ordinary.value.reason is None


def test_http_cancellation_reason_preserves_old_code_and_whitelists_reason(monkeypatch):
    from dlr.control import app as app_module

    # Factory only; do not enter lifespan or touch DB/broker/runtime logging.
    monkeypatch.setattr(app_module, "configure_platform_logging", lambda *_: None)
    monkeypatch.setattr(app_module, "configure_ai_tool_audit_logging", lambda: None)
    app = app_module.create_app()
    handler = app.exception_handlers[DocumentConversionError]
    for reason in ("scope_closed", None, "untrusted upstream text"):
        response = asyncio.run(
            handler(None, DocumentConversionError("ai_attachment_cancelled", reason=reason))
        )
        detail = json.loads(response.body)["detail"]
        assert response.status_code == 409 and detail["code"] == "ai_attachment_cancelled"
        if reason == "scope_closed":
            assert detail["params"] == {"reason": "scope_closed"}
        else:
            assert "params" not in detail
