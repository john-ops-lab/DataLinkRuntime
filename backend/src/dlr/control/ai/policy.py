"""Shared Assist policies, independent of either runtime implementation."""

import json
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from dlr.control.ai import providers, tool_audit
from dlr.control.ai import tools as tools_service
from dlr.control.ai.diagnostics import AiResponseInvalid, StrictJsonError
from dlr.control.ai.output_safety import contains_secret
from dlr.control.schemas.ai import (
    AiAssistRequest,
    AiAssistResponse,
    AiModelOutput,
    contains_unicode_surrogate,
)

_FINALIZATION_RESERVE_SECONDS = 30.0


_MAX_CONSECUTIVE_TOOL_FAILURES = 3


_STOP_ROUND_BUDGET = "tool_round_budget"


_STOP_CALL_BUDGET = "tool_call_budget"


_STOP_DUPLICATE = "duplicate_tool_call"


_STOP_CONSECUTIVE_FAILURES = "consecutive_tool_failures"


_STOP_RESULT_BUDGET = "tool_result_budget"


_STOP_DEADLINE = "assist_deadline"


_STOP_PROVIDER_FAILURE = "provider_followup_failure"


_STOP_CONTEXT_BUDGET = "context_budget_exceeded"


_STOP_ATTACHMENT_UNREAD = "attachment_content_unread"


_STOP_KNOWLEDGE_UNAVAILABLE = "knowledge_unavailable"


_STOP_KNOWLEDGE_SEQUENCE = "knowledge_sequence_incomplete"


_STOP_KNOWLEDGE_LIST_EMPTY = "knowledge_list_empty"


_STOP_KNOWLEDGE_LIST_FAILED = "knowledge_list_failed"


_STOP_KNOWLEDGE_SEARCH_EMPTY = "knowledge_search_empty"


_STOP_KNOWLEDGE_SEARCH_FAILED = "knowledge_search_failed"


_STOP_KNOWLEDGE_READ_FAILED = "knowledge_read_failed"


_STOP_KNOWLEDGE_READY = "knowledge_ready"


_MAX_KNOWLEDGE_SEARCHES = 6


_KNOWLEDGE_EXPECTED_TOOL = {
    "need_list": "list_knowledge_bases",
    "need_search": "search_knowledge",
    "need_read": "read_knowledge",
}


@dataclass
class _AssistToolState:
    """Request-local tool-loop counters and monotonic deadlines."""

    started_at: float
    tool_deadline: float
    hard_deadline: float
    request_id: str
    conversation_id: str
    format_corrections: int = 0
    attachment_read_required: bool = False
    attachment_read_attempts: int = 0
    successful_attachment_reads: int = 0
    attachment_read_correction: bool = False
    prepared_knowledge_incomplete: bool = False
    # Only the server fallback helper may mark a result safe to return after
    # the deadline. A tool stop or a provider's candidate:null is insufficient.
    server_fallback_returned: bool = False
    tool_rounds: int = 0
    total_tool_calls: int = 0
    consecutive_failures: int = 0
    accumulated_result_chars: int = 0
    seen_fingerprints: set[str] = field(default_factory=set)
    stop_reason: str | None = None

    @classmethod
    def create(
        cls,
        total_timeout_seconds: float,
        *,
        now: float | None = None,
        correlation: tool_audit.AiAuditCorrelation | None = None,
    ) -> "_AssistToolState":
        started_at = time.monotonic() if now is None else now
        correlation = correlation or tool_audit.new_request_correlation(None)
        return cls(
            started_at=started_at,
            tool_deadline=started_at
            + max(0.0, total_timeout_seconds - _FINALIZATION_RESERVE_SECONDS),
            hard_deadline=started_at + total_timeout_seconds,
            request_id=correlation.request_id,
            conversation_id=correlation.conversation_id,
        )

    def remaining_tool_seconds(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.tool_deadline - current)

    def remaining_total_seconds(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.hard_deadline - current)

    def begin_round(self, incoming_calls: int) -> bool:
        """Accept a complete Provider tool batch without crossing a budget."""
        if self.tool_rounds + 1 > tools_service.MAX_TOOL_ROUNDS:
            self.stop_reason = _STOP_ROUND_BUDGET
            return False
        if self.total_tool_calls + incoming_calls > tools_service.MAX_TOOL_CALLS_PER_ASSIST:
            self.stop_reason = _STOP_CALL_BUDGET
            return False
        self.tool_rounds += 1
        return True

    def register_fingerprint(self, fingerprint: str | None) -> bool:
        """Return ``False`` for an equivalent call already seen this request."""
        if fingerprint is None:
            return True
        if fingerprint in self.seen_fingerprints:
            self.stop_reason = _STOP_DUPLICATE
            return False
        self.seen_fingerprints.add(fingerprint)
        return True

    def record_execution(self, execution: tools_service.ToolExecution) -> None:
        self.total_tool_calls += 1
        self.accumulated_result_chars += execution.result_size
        if execution.tool_name in ("read_attachment_page", "read_attachment_sheet"):
            self.attachment_read_attempts += 1
            if execution.status == "success":
                self.successful_attachment_reads += 1
        if execution.status == "success":
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
        if self.accumulated_result_chars > tools_service.MAX_TOOL_RESULT_TOTAL_CHARS:
            self.stop_reason = _STOP_RESULT_BUDGET
        elif self.consecutive_failures >= _MAX_CONSECUTIVE_TOOL_FAILURES:
            self.stop_reason = _STOP_CONSECUTIVE_FAILURES
        elif self.total_tool_calls >= tools_service.MAX_TOOL_CALLS_PER_ASSIST:
            self.stop_reason = _STOP_CALL_BUDGET

    def record_protocol_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= _MAX_CONSECUTIVE_TOOL_FAILURES:
            self.stop_reason = _STOP_KNOWLEDGE_SEQUENCE


@dataclass
class _KnowledgeRetrievalState:
    """Authoritative server-side retrieval order, evidence and true trajectory.

    Search ``title`` + ``summary`` fields are usable evidence.  Reading a hit
    is an optional upgrade to full-text evidence, so an unreadable subscribed
    item never erases a successful search or falsely turns the base into an
    empty result.
    """

    phase: str
    source_id: str | None = None
    knowledge_base_ids: set[str] = field(default_factory=set)
    item_ids: set[str] = field(default_factory=set)
    evidence_sources: set[str] = field(default_factory=set)
    summary_sources: set[str] = field(default_factory=set)
    title_only_sources: set[str] = field(default_factory=set)
    full_text_sources: set[str] = field(default_factory=set)
    search_attempts: list[dict[str, object]] = field(default_factory=list)
    read_attempts: int = 0
    empty_searches: int = 0
    degraded_error_codes: set[str] = field(default_factory=set)
    stop_reason: str | None = None

    @classmethod
    def create(cls, enabled: bool, available: bool) -> "_KnowledgeRetrievalState":
        if not enabled:
            return cls(phase="disabled")
        if not available:
            return cls(phase="stopped", stop_reason=_STOP_KNOWLEDGE_UNAVAILABLE)
        return cls(phase="need_list")

    @property
    def expected_tool(self) -> str | None:
        if self.phase in ("summary_ready", "ready") and self.needs_cross_base_search:
            return "search_knowledge"
        return _KNOWLEDGE_EXPECTED_TOOL.get(self.phase)

    @property
    def requires_tool(self) -> bool:
        return self.expected_tool is not None

    @property
    def has_search_evidence(self) -> bool:
        return bool(self.summary_sources or self.title_only_sources)

    @property
    def searched_knowledge_base_ids(self) -> set[str]:
        return {
            knowledge_base_id
            for attempt in self.search_attempts
            if isinstance((knowledge_base_id := attempt.get("knowledge_base_id")), str)
            and knowledge_base_id
        }

    @property
    def needs_cross_base_search(self) -> bool:
        required_bases = min(2, len(self.knowledge_base_ids))
        return self.has_search_evidence and len(self.searched_knowledge_base_ids) < required_bases

    @property
    def has_more_search_pages(self) -> bool:
        return any(attempt.get("is_end") is False for attempt in self.search_attempts)

    @property
    def has_unknown_search_page_end(self) -> bool:
        return any(
            attempt.get("is_end") is None and "error_code" not in attempt
            for attempt in self.search_attempts
        )

    def accepts_call(self, tool_name: str, validated_args: dict[str, object] | None) -> bool:
        if self.phase == "disabled":
            return True
        if self.phase in ("summary_ready", "ready"):
            if self.needs_cross_base_search:
                if tool_name != "search_knowledge":
                    return False
            elif tool_name not in ("search_knowledge", "read_knowledge"):
                return False
        elif tool_name != self.expected_tool:
            return False
        # Invalid arguments still reach the existing strict dispatcher so the
        # browser receives its established ai_tool_args_invalid code.
        if validated_args is None:
            return True
        source = validated_args.get("source")
        if self.phase == "need_list":
            return isinstance(source, str)
        if source != self.source_id:
            return False
        if tool_name == "search_knowledge":
            knowledge_base_id = validated_args.get("knowledge_base_id")
            return (
                isinstance(knowledge_base_id, str) and knowledge_base_id in self.knowledge_base_ids
            )
        if tool_name == "read_knowledge":
            item_id = validated_args.get("item_id")
            return isinstance(item_id, str) and item_id in self.item_ids
        return False

    def normalize_call_arguments(
        self,
        tool_name: str,
        validated_args: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Repair one harmless Provider quoting artifact at the ID boundary.

        Some prompt-only Providers copy the closing JSON quote into the
        ``knowledge_base_id`` value. Only remove one trailing quote when the
        repaired value exactly matches an ID returned by this request's list
        result. Every other unknown or forged ID remains unchanged and is
        rejected by :meth:`accepts_call`.
        """

        if tool_name != "search_knowledge" or validated_args is None:
            return validated_args
        knowledge_base_id = validated_args.get("knowledge_base_id")
        if (
            not isinstance(knowledge_base_id, str)
            or knowledge_base_id in self.knowledge_base_ids
            or not knowledge_base_id.endswith(('"', "'"))
        ):
            return validated_args
        repaired_id = knowledge_base_id[:-1]
        if repaired_id not in self.knowledge_base_ids:
            return validated_args
        return {**validated_args, "knowledge_base_id": repaired_id}

    def record_execution(
        self,
        validated_args: dict[str, object] | None,
        execution: tools_service.ToolExecution,
    ) -> None:
        if self.phase == "disabled":
            return
        phase = self.phase
        if execution.status == "error" or validated_args is None:
            if phase in ("summary_ready", "ready") and self.has_search_evidence:
                if execution.error_code:
                    self.degraded_error_codes.add(execution.error_code)
                if execution.tool_name == "read_knowledge":
                    self.read_attempts += 1
                    # Search summaries remain valid evidence. One failed
                    # optional full-text upgrade is enough: stop this tool
                    # batch immediately and synthesize from those summaries.
                    self.phase = "stopped"
                    self.stop_reason = _STOP_KNOWLEDGE_READ_FAILED
                    return
                elif execution.tool_name == "search_knowledge" and validated_args is not None:
                    knowledge_base_id = validated_args.get("knowledge_base_id")
                    query = validated_args.get("query")
                    if isinstance(knowledge_base_id, str) and isinstance(query, str):
                        self.search_attempts.append(
                            {
                                "knowledge_base_id": knowledge_base_id,
                                "query": query,
                                "returned_matches": None,
                                "is_end": None,
                                "error_code": execution.error_code,
                            }
                        )
                    if self._stop_at_search_limit():
                        return
                self.phase = "summary_ready"
                return
            self._stop_failed(phase)
            return
        try:
            result = json.loads(execution.model_content)
        except (json.JSONDecodeError, RecursionError):
            self._stop_failed(phase)
            return
        if not isinstance(result, dict):
            self._stop_failed(phase)
            return
        if phase == "need_list":
            items = self._result_items(result)
            if items is None:
                self._stop_failed(phase)
                return
            self.source_id = str(validated_args["source"])
            self.knowledge_base_ids = self._ids_from(items)
            self._collect_sources(items)
            if not self.knowledge_base_ids:
                self.phase = "stopped"
                self.stop_reason = _STOP_KNOWLEDGE_LIST_EMPTY
            else:
                self.phase = "need_search"
            return
        if execution.tool_name == "search_knowledge" and phase in (
            "need_search",
            "summary_ready",
            "ready",
        ):
            items = self._result_items(result)
            if items is None:
                self._stop_failed(phase)
                return
            knowledge_base_id = validated_args.get("knowledge_base_id")
            query = validated_args.get("query")
            returned_matches = result.get("returned_matches")
            is_end = result.get("is_end")
            if (
                not isinstance(knowledge_base_id, str)
                or not isinstance(query, str)
                or isinstance(returned_matches, bool)
                or not isinstance(returned_matches, int)
                or returned_matches < len(items)
                or (is_end is not None and not isinstance(is_end, bool))
            ):
                self._stop_failed(phase)
                return
            self.search_attempts.append(
                {
                    "knowledge_base_id": knowledge_base_id,
                    "query": query,
                    "returned_matches": returned_matches,
                    "is_end": is_end,
                }
            )
            found_ids = self._ids_from(items)
            self.item_ids.update(found_ids)
            self._collect_sources(items)
            self._collect_search_evidence(items)
            if not found_ids:
                self.empty_searches += 1
            if self._stop_at_search_limit():
                return
            if not found_ids:
                if self.has_search_evidence:
                    self.phase = "summary_ready"
                else:
                    self.phase = "need_search"
            else:
                self.phase = "summary_ready"
            return
        if execution.tool_name == "read_knowledge" and phase in ("summary_ready", "ready"):
            item = result.get("item")
            expected_id = validated_args.get("item_id")
            if not isinstance(item, dict) or item.get("id") != expected_id:
                self._stop_failed(phase)
                return
            self._collect_sources([item])
            self.full_text_sources.update(self._sources_from([item]))
            self.read_attempts += 1
            self.phase = "ready"
            return
        self._stop_failed(phase)

    def correction_message(self) -> str:
        expected = self.expected_tool or "the required knowledge tool"
        search_guidance = ""
        if self.needs_cross_base_search:
            search_guidance = (
                " Search at least one different, not-yet-searched knowledge base returned by "
                "list_knowledge_bases before answering. An empty or failed second-base search "
                "still completes this bounded cross-base attempt; preserve any existing search "
                "evidence and report the limitation transparently."
            )
        elif self.phase == "need_search" and self.search_attempts:
            search_guidance = (
                " The completed searches were empty. Change the search fingerprint: use a "
                "shorter core keyword or synonym and try another plausibly relevant knowledge "
                "base returned by list_knowledge_bases."
            )
        return (
            "Knowledge retrieval is enabled and the previous response did not satisfy the "
            f"server-enforced stage {self.phase}. Do not answer yet. Call {expected} next, "
            "using only identifiers returned by earlier knowledge tool results." + search_guidance
        )

    def progress_message(self) -> str:
        trajectory = json.dumps(self.search_attempts, ensure_ascii=False, separators=(",", ":"))
        if self.phase == "need_search":
            return self.correction_message() + f" Exact completed search trajectory: {trajectory}."
        return (
            "Knowledge hits with a non-empty summary are usable search-summary evidence. A hit "
            "with an empty summary is title-only: retain its source for audit, label it as "
            "title-only, and never cite or invent summary content for it. "
            "Search every other plausibly relevant knowledge base within the fixed tool budget "
            "and aggregate the relevant snippets. read_knowledge is optional and only upgrades "
            "one returned item to full-text evidence; a read error must not be described as an "
            "empty knowledge base. Clearly label summary-based and title-only claims. Exact "
            f"completed search trajectory: {trajectory}. Full-text source identifiers: "
            f"{sorted(self.full_text_sources)}. Never claim a search or read not present here."
            + (
                " At least one response had is_end=false, so use only the returned page and "
                "state that more upstream pages may exist."
                if self.has_more_search_pages
                else ""
            )
            + (
                " At least one response omitted is_end; do not claim the search exhausted all "
                "upstream pages."
                if self.has_unknown_search_page_end
                else ""
            )
        )

    def finalization_instruction(self, system_locale: str) -> str:
        labels = (
            ("知识库检索结果", "模型综合")
            if system_locale != "en"
            else ("Knowledge retrieval result", "Model synthesis")
        )
        trajectory = json.dumps(self.search_attempts, ensure_ascii=False, separators=(",", ":"))
        state_label = self.stop_reason or self.phase
        return (
            f"Knowledge retrieval ended with state {state_label}. In message, explicitly "
            f"separate sections labeled '{labels[0]}' and '{labels[1]}'. State any empty or "
            "failed stage and its stable error code. Only non-empty search summaries are citable "
            "search-summary evidence; empty summaries are title-only hits and must not be cited "
            "or invented as summary content. Only full-text source identifiers below were read. "
            "Cite "
            "only knowledge base, item and source identifiers present in sanitized tool messages. "
            f"Exact search trajectory: {trajectory}. Full-text sources: "
            f"{sorted(self.full_text_sources)}. Never invent a search, read or source. "
            "If any trajectory entry has is_end=false, say more upstream pages may exist; if "
            "is_end is null, do not claim the search exhausted all upstream pages."
        )

    @staticmethod
    def _result_items(result: dict[str, object]) -> list[dict[str, object]] | None:
        items = result.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return None
        return items

    @staticmethod
    def _ids_from(items: list[dict[str, object]]) -> set[str]:
        return {
            item_id for item in items if isinstance((item_id := item.get("id")), str) and item_id
        }

    def _collect_sources(self, items: list[dict[str, object]]) -> None:
        self.evidence_sources.update(self._sources_from(items))

    def _collect_search_evidence(self, items: list[dict[str, object]]) -> None:
        for item in items:
            source = item.get("source")
            if not isinstance(source, str) or not source:
                continue
            summary = item.get("summary")
            if isinstance(summary, str) and summary.strip():
                self.summary_sources.add(source)
                self.title_only_sources.discard(source)
            elif source not in self.summary_sources:
                self.title_only_sources.add(source)

    def _stop_at_search_limit(self) -> bool:
        if len(self.search_attempts) < _MAX_KNOWLEDGE_SEARCHES:
            return False
        self.phase = "stopped"
        self.stop_reason = (
            _STOP_KNOWLEDGE_READY if self.has_search_evidence else _STOP_KNOWLEDGE_SEARCH_EMPTY
        )
        return True

    @staticmethod
    def _sources_from(items: list[dict[str, object]]) -> set[str]:
        return {
            source for item in items if isinstance((source := item.get("source")), str) and source
        }

    def _stop_failed(self, phase: str) -> None:
        reasons = {
            "need_list": _STOP_KNOWLEDGE_LIST_FAILED,
            "need_search": _STOP_KNOWLEDGE_SEARCH_FAILED,
            "summary_ready": _STOP_KNOWLEDGE_READ_FAILED,
            "ready": _STOP_KNOWLEDGE_READ_FAILED,
        }
        self.phase = "stopped"
        self.stop_reason = reasons.get(phase, _STOP_KNOWLEDGE_SEQUENCE)


def _reject_secret_reflection(value: object, api_key: str | None) -> None:
    if api_key and contains_secret(value, api_key):
        raise AiResponseInvalid("output_safety", "secret_reflection")


def _parse_model_output(final_text: str, api_key: str | None = None) -> AiModelOutput:
    _reject_secret_reflection(final_text, api_key)
    try:
        raw = providers.load_json_strict(final_text)
    except StrictJsonError as error:
        raise AiResponseInvalid("final_json", error.reason) from None
    except json.JSONDecodeError:
        raise AiResponseInvalid("final_json", "malformed_json") from None
    except RecursionError:
        raise AiResponseInvalid("final_json", "json_limit") from None
    except ValueError:
        raise AiResponseInvalid("final_json", "json_value_invalid") from None
    if contains_unicode_surrogate(raw):
        raise AiResponseInvalid("unicode", "invalid_unicode")
    try:
        output = AiModelOutput.model_validate(raw, strict=True)
        visible_output = output.model_dump(mode="json")
        if contains_unicode_surrogate(visible_output):
            raise AiResponseInvalid("unicode", "invalid_unicode")
        _reject_secret_reflection(visible_output, api_key)
        return output
    except (ValueError, ValidationError, RecursionError):
        raise AiResponseInvalid("output_schema", "schema_mismatch") from None


def _reject_candidate_configuration_changes(
    output: AiModelOutput, payload: AiAssistRequest
) -> None:
    """Keep the AI boundary code-only while accepting old envelope echoes.

    A Provider may omit the historical configuration fields entirely. If it
    sends either field, however, the value is a compatibility echo and must
    match the browser Working Copy structurally. This prevents natural-
    language requirements or runtime settings from ever becoming an
    applicable Candidate while keeping the strict response parser intact.
    """
    candidate = output.candidate
    if candidate is None:
        return
    fields_set = candidate.model_fields_set
    if "requirements" in fields_set and candidate.requirements != payload.working_copy.requirements:
        raise AiResponseInvalid("candidate_configuration", "requirements_mismatch")
    if (
        "runtime_config" in fields_set
        and candidate.runtime_config != payload.working_copy.runtime_config
    ):
        raise AiResponseInvalid("candidate_configuration", "runtime_config_mismatch")


def complete_response(
    payload: AiAssistRequest, response: AiAssistResponse, state: _AssistToolState
) -> AiAssistResponse:
    """Compute task status only after evidence, intent and Candidate validation."""
    intent = payload.task_intent if payload.task_intent != "auto" else response.request_intent
    response.task_status = (
        "incomplete"
        if (state.attachment_read_required and not state.successful_attachment_reads)
        or state.prepared_knowledge_incomplete
        else "candidate_ready"
        if response.candidate is not None
        else "incomplete"
        if intent == "code_change"
        else "answered"
        if intent == "consultation"
        else "unclassified"
    )
    return response
