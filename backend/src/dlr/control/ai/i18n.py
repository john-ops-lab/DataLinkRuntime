"""Canonical AI zh/en compatibility copy and server-owned evidence formatters."""

from dlr.control.ai.policy import (
    _STOP_ATTACHMENT_UNREAD,
    _STOP_CALL_BUDGET,
    _STOP_CONSECUTIVE_FAILURES,
    _STOP_CONTEXT_BUDGET,
    _STOP_DEADLINE,
    _STOP_DUPLICATE,
    _STOP_KNOWLEDGE_LIST_EMPTY,
    _STOP_KNOWLEDGE_LIST_FAILED,
    _STOP_KNOWLEDGE_READ_FAILED,
    _STOP_KNOWLEDGE_READY,
    _STOP_KNOWLEDGE_SEARCH_EMPTY,
    _STOP_KNOWLEDGE_SEARCH_FAILED,
    _STOP_KNOWLEDGE_SEQUENCE,
    _STOP_KNOWLEDGE_UNAVAILABLE,
    _STOP_PROVIDER_FAILURE,
    _STOP_RESULT_BUDGET,
    _STOP_ROUND_BUDGET,
    _KnowledgeRetrievalState,
)
from dlr.control.schemas.ai import (
    AiToolCallSummary,
)


def _fallback_message(
    system_locale: str,
    stop_reason: str,
    tool_calls: list[AiToolCallSummary],
) -> str:
    if stop_reason == _STOP_ATTACHMENT_UNREAD:
        if system_locale == "en":
            return (
                "No attachment content was successfully read in this request, so the task "
                "is incomplete. The attachment directory and preparation status do not "
                "confirm a content read. Retry or attach the files again."
            )
        return (
            "本轮未成功读取附件内容，请求尚未完成。附件目录和准备状态不代表已读取内容。"
            "请重试或重新附加文件。"
        )
    if stop_reason == _STOP_KNOWLEDGE_UNAVAILABLE:
        if system_locale == "en":
            return (
                "Knowledge search is unavailable, so no retrieval was performed for this "
                "request. Ask an administrator to configure a searchable knowledge source."
            )
        return "知识库检索当前不可用，本轮未执行检索。请管理员配置可检索的知识源。"
    successful = sum(item.status == "success" for item in tool_calls)
    unsuccessful = len(tool_calls) - successful
    stable_errors = sorted({item.error_code for item in tool_calls if item.error_code})
    error_suffix_en = f" Stable error code(s): {', '.join(stable_errors)}." if stable_errors else ""
    error_suffix_zh = f" 稳定错误码：{', '.join(stable_errors)}。" if stable_errors else ""
    if system_locale == "en":
        reasons = {
            _STOP_ROUND_BUDGET: "tool round limit reached",
            _STOP_CALL_BUDGET: "tool call limit reached",
            _STOP_DUPLICATE: "repeated tool call blocked",
            _STOP_CONSECUTIVE_FAILURES: "three consecutive tool failures",
            _STOP_RESULT_BUDGET: "tool result size limit reached",
            _STOP_DEADLINE: "Assist deadline reached",
            _STOP_PROVIDER_FAILURE: "Provider follow-up failed",
            _STOP_CONTEXT_BUDGET: "model context budget exceeded",
            _STOP_KNOWLEDGE_UNAVAILABLE: "knowledge retrieval is unavailable",
            _STOP_KNOWLEDGE_SEQUENCE: "knowledge retrieval order was not completed",
            _STOP_KNOWLEDGE_LIST_EMPTY: "no searchable knowledge base was found",
            _STOP_KNOWLEDGE_LIST_FAILED: "knowledge base listing failed",
            _STOP_KNOWLEDGE_SEARCH_EMPTY: "knowledge search returned no matches",
            _STOP_KNOWLEDGE_SEARCH_FAILED: "knowledge search failed",
            _STOP_KNOWLEDGE_READ_FAILED: "knowledge item reading failed",
            _STOP_KNOWLEDGE_READY: "knowledge retrieval completed but finalization failed",
        }
        reason = reasons.get(stop_reason, "tool safety boundary reached")
        return (
            f"Tool use stopped safely: {reason}. Preserved {successful} successful "
            f"result(s) and {unsuccessful} failed or blocked record(s). The model did not "
            "produce a valid final answer in the remaining time, so unconfirmed parts "
            f"still require review.{error_suffix_en}"
        )
    reasons = {
        _STOP_ROUND_BUDGET: "已达到工具轮次上限",
        _STOP_CALL_BUDGET: "已达到工具调用上限",
        _STOP_DUPLICATE: "已拦截重复工具调用",
        _STOP_CONSECUTIVE_FAILURES: "工具连续失败三次",
        _STOP_RESULT_BUDGET: "已达到工具结果大小上限",
        _STOP_DEADLINE: "已达到 Assist 总时限",
        _STOP_PROVIDER_FAILURE: "模型后续请求失败",
        _STOP_CONTEXT_BUDGET: "模型上下文预算已耗尽",
        _STOP_KNOWLEDGE_UNAVAILABLE: "知识库检索当前不可用",
        _STOP_KNOWLEDGE_SEQUENCE: "知识库检索顺序未完成",
        _STOP_KNOWLEDGE_LIST_EMPTY: "没有可检索的知识库",
        _STOP_KNOWLEDGE_LIST_FAILED: "知识库列表阶段失败",
        _STOP_KNOWLEDGE_SEARCH_EMPTY: "知识库搜索未找到匹配内容",
        _STOP_KNOWLEDGE_SEARCH_FAILED: "知识库搜索阶段失败",
        _STOP_KNOWLEDGE_READ_FAILED: "知识条目正文读取失败",
        _STOP_KNOWLEDGE_READY: "知识库检索已完成但最终回答失败",
    }
    reason = reasons.get(stop_reason, "已触发工具安全边界")
    return (
        f"工具调用已安全停止：{reason}。已保留 {successful} 个成功结果和 "
        f"{unsuccessful} 个失败或拦截记录。模型未能在剩余时间内生成有效最终答复，"
        f"尚未确认的部分仍需人工核实。{error_suffix_zh}"
    )


def _transparent_knowledge_message(
    system_locale: str,
    stop_reason: str,
    model_message: str,
    tool_calls: list[AiToolCallSummary],
) -> str:
    stable_errors = sorted({item.error_code for item in tool_calls if item.error_code})
    if system_locale == "en":
        statuses = {
            _STOP_KNOWLEDGE_SEQUENCE: "The required retrieval order was not completed.",
            _STOP_KNOWLEDGE_LIST_EMPTY: "No searchable knowledge base was available.",
            _STOP_KNOWLEDGE_LIST_FAILED: "Knowledge base listing failed.",
            _STOP_KNOWLEDGE_SEARCH_EMPTY: "Knowledge search returned no matching item.",
            _STOP_KNOWLEDGE_SEARCH_FAILED: "Knowledge search failed.",
            _STOP_KNOWLEDGE_READ_FAILED: "The selected knowledge item could not be read.",
        }
        status = statuses.get(stop_reason, "Knowledge retrieval did not complete.")
        if stable_errors:
            status += f" Stable error code(s): {', '.join(stable_errors)}."
        return f"Knowledge retrieval result: {status}\n\nModel supplement: {model_message}"
    statuses = {
        _STOP_KNOWLEDGE_SEQUENCE: "未完成服务端要求的检索顺序。",
        _STOP_KNOWLEDGE_LIST_EMPTY: "没有可检索的知识库。",
        _STOP_KNOWLEDGE_LIST_FAILED: "知识库列表阶段失败。",
        _STOP_KNOWLEDGE_SEARCH_EMPTY: "知识库搜索未找到匹配条目。",
        _STOP_KNOWLEDGE_SEARCH_FAILED: "知识库搜索阶段失败。",
        _STOP_KNOWLEDGE_READ_FAILED: "选中的知识条目正文读取失败。",
    }
    status = statuses.get(stop_reason, "知识库检索未完成。")
    if stable_errors:
        status += f" 稳定错误码：{', '.join(stable_errors)}。"
    return f"知识库检索结果：{status}\n\n模型补充：{model_message}"


def _knowledge_evidence_message(
    system_locale: str,
    model_message: str,
    knowledge_state: _KnowledgeRetrievalState,
) -> str:
    """Attach a server-owned, trajectory-derived evidence label.

    The Provider performs the synthesis, but it cannot rewrite these counts
    or turn a failed optional read into a claim that the search itself was
    empty.
    """
    search_count = len(knowledge_state.search_attempts)
    searched_bases = {
        attempt["knowledge_base_id"]
        for attempt in knowledge_state.search_attempts
        if isinstance(attempt.get("knowledge_base_id"), str)
    }
    summary_count = len(knowledge_state.summary_sources)
    title_only_count = len(knowledge_state.title_only_sources)
    full_text_count = len(knowledge_state.full_text_sources)
    stable_errors = sorted(knowledge_state.degraded_error_codes)
    if summary_count and title_only_count:
        evidence_basis_en = "search summaries and title-only hits"
        evidence_basis_zh = "搜索摘要及仅标题命中"
    elif summary_count:
        evidence_basis_en = "search summaries"
        evidence_basis_zh = "搜索摘要"
    else:
        evidence_basis_en = "title-only hits"
        evidence_basis_zh = "仅标题命中"
    if system_locale == "en":
        status = (
            f"Actually ran {search_count} search_knowledge call(s) across "
            f"{len(searched_bases)} knowledge base(s). "
        )
        if summary_count:
            status += f"Retained {summary_count} unique citable search-summary source(s). "
        else:
            status += "No non-empty search summaries were returned. "
        if title_only_count:
            status += (
                f"Retained {title_only_count} title-only hit source(s); their empty summaries "
                "are not citable content. "
            )
        if full_text_count:
            status += f"Full text was read for {full_text_count} result(s)."
        elif knowledge_state.read_attempts:
            status += (
                f"Full-text reading failed; the synthesis below uses {evidence_basis_en} only."
            )
        else:
            status += f"Full text was not read; the synthesis below uses {evidence_basis_en}."
        if stable_errors:
            status += f" Stable error code(s): {', '.join(stable_errors)}."
        if knowledge_state.has_more_search_pages:
            status += (
                " At least one search page reported is_end=false; more upstream pages may exist."
            )
        elif knowledge_state.has_unknown_search_page_end:
            status += (
                " Upstream page completion was not reported; the search is not claimed exhaustive."
            )
        return f"Knowledge retrieval result: {status}\n\nModel synthesis: {model_message}"
    status = f"实际执行 {search_count} 次 search_knowledge，覆盖 {len(searched_bases)} 个知识库；"
    if summary_count:
        status += f"保留 {summary_count} 个唯一、可引用的搜索摘要来源。"
    else:
        status += "未返回非空搜索摘要。"
    if title_only_count:
        status += f"共有 {title_only_count} 个仅标题命中；其空摘要不作为可引用内容。"
    if full_text_count:
        status += f"其中 {full_text_count} 条已读取全文。"
    elif knowledge_state.read_attempts:
        status += f"全文读取失败；下方综合仅依据{evidence_basis_zh}。"
    else:
        status += f"本次未读取全文；下方综合依据{evidence_basis_zh}。"
    if stable_errors:
        status += f" 稳定错误码：{', '.join(stable_errors)}。"
    if knowledge_state.has_more_search_pages:
        status += " 至少一个搜索响应为 is_end=false，可能仍有后续页。"
    elif knowledge_state.has_unknown_search_page_end:
        status += " 上游未提供分页结束状态，本次不宣称已穷尽全部结果。"
    return f"知识库检索结果：{status}\n\n模型综合：{model_message}"


def omission_note(omitted: int, system_locale: str) -> str:
    return (
        f"DLR omitted {omitted} optional history, reference, or image item(s) "
        "to fit the model context."
        if system_locale == "en"
        else f"DLR 为适配模型上下文，省略了 {omitted} 项可选历史、参考材料或图片。"
    )


def knowledge_finalization_labels(system_locale: str) -> tuple[str, str]:
    return (
        ("知识库检索结果", "模型综合")
        if system_locale != "en"
        else ("Knowledge retrieval result", "Model synthesis")
    )
