# ruff: noqa: E501 -- the entry content below is app-shipped prose data, not code
"""M5.7 Wave C1: the app-shipped DLR Runtime Contract / platform help docs.

This is the ONLY content collection the C1 read-only knowledge tools may
touch. The entries are static Python data shipped with the application —
there is deliberately no filesystem, database or HTTP access anywhere in
this module or in the tool handlers, so the docs tools can never become an
arbitrary path / URL reader.

Every entry carries a stable id, a category, a bounded summary/content and an
auditable ``source`` identifier of the form ``dlr-docs:v1:<id>`` that the
tool results expose. All lookups are deterministic: list order, search
ranking and read results never depend on time, locale or request state.
"""

from __future__ import annotations

from dataclasses import dataclass

DLR_DOCS_VERSION = "v1"

# Deterministic bounds for the whole collection. Individual entries are
# bounded at authoring time; the bounds below are enforced by the tools layer
# before any content reaches the model or the browser.
MAX_DOC_ENTRIES = 64
MAX_DOC_CONTENT_CHARS = 8000
MAX_DOC_SUMMARY_CHARS = 300


@dataclass(frozen=True)
class DocEntry:
    """One immutable, app-shipped platform help document."""

    id: str
    title: str
    category: str
    summary: str
    content: str

    @property
    def source(self) -> str:
        return f"dlr-docs:{DLR_DOCS_VERSION}:{self.id}"


def _entry(
    doc_id: str,
    title: str,
    category: str,
    summary: str,
    content: str,
) -> DocEntry:
    if (
        len(summary) > MAX_DOC_SUMMARY_CHARS
        or len(content) > MAX_DOC_CONTENT_CHARS
        or not doc_id.isascii()
        or any(character.isspace() for character in doc_id)
    ):
        raise ValueError(f"invalid docs entry: {doc_id}")
    return DocEntry(doc_id, title, category, summary, content)


_PYTHON_CONTRACT = """\
The Python Adapter runtime contract:

- Entry point: def handle(context, input) -> JSON-serializable value.
- context.config: dict of the Adapter runtime_config (plain values only).
- context.secrets.get("ENV_KEY"): the bound Credential field value for the
  request. Only keys listed in the Adapter's credential bindings are
  available; missing bindings return None.
- context.logger: standard logger (context.logger.info / warning / error).
- input is JSON-compatible; the return value must be JSON-serializable
  (dict / list / str / int / float / bool / None).
- A raised exception fails the Execution with its message (sanitized).
- Output and stdout are size-bounded and truncated with markers; the
  platform never persists or logs Credential truth.
"""

_JAVASCRIPT_CONTRACT = """\
The JavaScript Adapter runtime contract:

- Entry point: export async function handle(context, input) -> JSON value.
- context.config: the Adapter runtime_config object.
- context.secrets.get("ENV_KEY"): bound Credential field for the request;
  unknown keys resolve to null.
- context.logger.info / warning / error write to the platform log.
- input is JSON-compatible; resolve with a JSON-serializable value.
- A rejected Promise fails the Execution with its message (sanitized).
- Execution output and stdout are bounded; Credential truth never enters
  output, logs or history.
"""

_JAVA_CONTRACT = """\
The Java Adapter runtime contract:

- Entry point: public Object handle(Context context, Object input).
- context.config(): the Adapter runtime_config map.
- context.secrets().get("ENV_KEY"): bound Credential field for the request;
  unknown keys return null.
- context.logger().info / warning / error write to the platform log.
- input is JSON-compatible (Map / List / String / Number / Boolean / null);
  the return value must be JSON-serializable.
- A thrown Exception fails the Execution with its message (sanitized).
- Execution output and stdout are bounded; Credential truth never enters
  output, logs or history.
"""

_ENTRIES: tuple[DocEntry, ...] = (
    _entry(
        "runtime-contract-python",
        "Python Adapter Runtime Contract",
        "runtime",
        "The Python handle(context, input) contract: config, secrets, logger, input and JSON output.",
        _PYTHON_CONTRACT,
    ),
    _entry(
        "runtime-contract-javascript",
        "JavaScript Adapter Runtime Contract",
        "runtime",
        "The JavaScript handle(context, input) contract: config, secrets, logger, async output.",
        _JAVASCRIPT_CONTRACT,
    ),
    _entry(
        "runtime-contract-java",
        "Java Adapter Runtime Contract",
        "runtime",
        "The Java handle(Context, Object) contract: config, secrets, logger, typed output.",
        _JAVA_CONTRACT,
    ),
    _entry(
        "lifecycle-revisions",
        "Adapter Versions and the Working Copy",
        "platform",
        "How Revisions, the browser Working Copy and the AI Candidate relate: Apply only writes the browser.",
        (
            "The browser Working Copy is the only authoritative code snapshot of the current editing "
            "session. Saving writes an immutable AdapterVersion (Revision) that Executions run against. "
            "AI Candidate generation never writes lifecycle state: the Candidate is a complete snapshot "
            "that the administrator reviews as a Diff and explicitly applies to the browser Working Copy "
            "only. Apply never saves, tests or runs automatically; the administrator triggers those "
            "actions. An AI Candidate is stale when the Working Copy changed after generation."
        ),
    ),
    _entry(
        "secrets-and-bindings",
        "Secrets: Credential Bindings and the AI Boundary",
        "platform",
        "How Credential truth is stored, bound per Adapter and kept out of prompts, tools, logs and UI.",
        (
            "Credentials are stored encrypted in the Secret Store; APIs only return metadata (name, "
            "type). An Adapter exposes bound Credential fields to its runtime through "
            'context.secrets.get("ENV_KEY") using binding names only. Secret truth never joins the AI '
            "prompt, Tool parameters or results, platform logs, errors or the browser. The AI assistant "
            "only knows the binding names (available_secret_keys) and may reference them in code. "
            "Never write passwords, tokens or keys into Adapter code or chat attachments."
        ),
    ),
    _entry(
        "execution-model",
        "Executions, Timeout and Output Bounds",
        "platform",
        "How a run is queued, executed on a fixed Worker, bounded in time and truncated in output.",
        (
            "An Execution is queued per Adapter (one active run at a time) and executed on the "
            "Adapter's chosen Worker. A run that exceeds the Adapter timeout_seconds is killed and "
            "marked timeout. Output, stdout and stderr are size-bounded and truncated with explicit "
            "markers. History rows never carry the full input/output of past runs. Cancellation is "
            "explicit and immediate; schedule runs share the same timeout and bounds."
        ),
    ),
    _entry(
        "worker-model",
        "Workers and Capabilities",
        "platform",
        "How runtime Workers register, heartbeat, go offline and declare language capabilities.",
        (
            "Workers register with a name and capabilities (python / javascript / java) and heartbeat "
            "on a schedule. An Adapter pins one runtime Worker; only online Workers with the Adapter's "
            "language capability can be chosen. A Worker whose heartbeat expired is reported offline "
            "without rewriting its stored status. Deleting a Worker only archives it; past Executions "
            "and Revisions stay intact."
        ),
    ),
    _entry(
        "task-mode",
        "Task Adapters: Manual and Schedule Runs",
        "platform",
        "Manual Run Once, Schedule triggers and the Run Mode lifecycle of Task Adapters.",
        (
            "Task Adapters run manually or on a schedule. Manual runs use the latest Revision; schedule "
            "runs keep an independent cursor (next_run_at) and never mutate the schedule configuration. "
            "While an Execution is active the Adapter is runtime-locked: Revisions, schedule changes, "
            "binding changes and deletion are blocked until the run finishes."
        ),
    ),
    _entry(
        "webhook-mode",
        "Webhook Adapters: Inbound HTTP Entry",
        "platform",
        "Random stopped path, Worker/Token first-save gates and the readable public entry contract.",
        (
            "Webhook Adapters expose one public entry path (public_id) that is random and stopped by "
            "default. Enabling requires a saved Revision, an online compatible Worker and an explicit "
            "receiving Token Credential. Requests without the correct Token are rejected; a stopped "
            "path is not routable. One Webhook Adapter receives at a time; Clones share the path and "
            "take over only after the current owner stops."
        ),
    ),
    _entry(
        "ai-assistant-usage",
        "AI Assistant: Candidates, Tools and Privacy",
        "platform",
        "How the AI assistant builds Candidates, what read-only tools it may call and what never leaves the server.",
        (
            "The AI assistant sends the current Working Copy snapshot, ordered context snippets, "
            "request-only attachments and the visible recent conversation to the configured model "
            "service. It may call DLR's registered read-only documentation tools (dlr_docs_list / "
            "dlr_docs_search / dlr_docs_read) which only read this app-shipped help collection with "
            "fixed bounds, and the registered read-only knowledge sources (list_knowledge_bases / "
            "search_knowledge / read_knowledge, e.g. Tencent ima) which only read official "
            "configured knowledge endpoints with the same bounds and strict HTTPS / host-allowlist "
            "guards. Every tool call is bounded (rounds, count, size, timeout, sequential "
            "execution) and sanitized; unknown or write tools are rejected. The final answer must "
            "still be a strict AiModelOutput JSON: a message and an optional complete Candidate "
            "snapshot. Secret truth, hidden reasoning and raw tool payloads never reach the browser."
        ),
    ),
    _entry(
        "tool-call-contract",
        "Read-only Tool Call Contract",
        "platform",
        "The C1/C2 tool whitelist, parameter schemas, bounds and sanitization rules.",
        (
            "Registered read-only tools: dlr_docs_list (optional category filter), "
            "dlr_docs_search (query and optional limit) and dlr_docs_read (doc_id) "
            "for the app-shipped help docs; list_knowledge_bases, search_knowledge "
            "and read_knowledge (source id, e.g. 'ima') for registered read-only "
            "knowledge sources. All are read-only, validate arguments against strict "
            "JSON schemas, run sequentially with a fixed per-call timeout, and return "
            "deterministic bounded results with a stable source identifier "
            "(dlr-docs:v1 / ima:v1). Per assist: at most 8 tool calls across at most "
            "4 tool rounds; per-call results and the accumulated result budget are "
            "capped; results are sanitized (secret patterns and the round's credential "
            "truth redacted by value) before reaching the model or the browser. "
            "Unknown, unregistered or write tools are rejected with a stable error "
            "result and never executed."
        ),
    ),
    _entry(
        "knowledge-source-contract",
        "Read-only KnowledgeSource Boundary (Tencent ima)",
        "platform",
        "The unified read-only knowledge boundary: list/search/read only, official hosts, HTTPS, bounded and sanitized.",
        (
            "DLR exposes exactly three read-only knowledge operations per source: "
            "list_knowledge_bases, search_knowledge and read_knowledge. The first "
            "real source is Tencent ima through a thin official OpenAPI adapter. "
            "Every source endpoint must be HTTPS on the official host allowlist; "
            "redirects are never followed, IP literals are rejected, response bodies "
            "are size-bounded and schema-validated before redaction, and every "
            "connection/read/total deadline interrupts the external call. Upload, "
            "write, delete, permission, share and sync operations do not exist in "
            "the boundary and are rejected. ima Client ID / API Key / Token live "
            "only in DLR Credentials (Secret Store) and are resolved at the "
            "server-side execution point for the duration of one tool call; they are "
            "redacted by value from prompts, tool parameters, summaries, results, "
            "logs and errors. The browser only ever sees sanitized summaries and "
            "binding status."
        ),
    ),
    _entry(
        "runtime-config-json",
        "runtime_config: the Adapter Configuration Object",
        "platform",
        "Plain finite JSON configuration exposed to the Adapter as context.config.",
        (
            "runtime_config is an arbitrary JSON object saved with each Revision. It must contain "
            "only finite JSON values (no NaN / Infinity, no duplicate keys) and is exposed to the "
            "runtime as context.config (Python), context.config (JavaScript) or context.config() "
            "(Java). The AI Candidate may propose runtime_config changes; lifecycle fields such as "
            "language, adapter_type and runtime_worker_id can never be changed by AI."
        ),
    ),
)


def all_entries() -> tuple[DocEntry, ...]:
    """The immutable collection. Callers must not mutate entries."""
    return _ENTRIES


def list_entries(category: str | None = None) -> tuple[DocEntry, ...]:
    """Deterministic category-filtered listing (empty filter returns all)."""
    if category is None or category == "":
        return _ENTRIES
    return tuple(entry for entry in _ENTRIES if entry.category == category)


def search_entries(query: str, limit: int) -> tuple[DocEntry, ...]:
    """Deterministic case-insensitive substring search.

    Entries are ranked by the position of the first match in the searchable
    text (id, title, summary, content), then by id for a stable tie-break.
    The result list is bounded to ``limit`` (already clamped by the tool
    schema) so identical queries always return identical results.
    """
    needle = query.casefold()
    ranked: list[tuple[int, DocEntry]] = []
    for entry in _ENTRIES:
        index = entry.id.casefold().find(needle)
        if index < 0:
            index = entry.title.casefold().find(needle)
        if index < 0:
            index = entry.summary.casefold().find(needle)
        if index < 0:
            index = entry.content.casefold().find(needle)
        if index >= 0:
            ranked.append((index, entry))
    ranked.sort(key=lambda item: (item[0], item[1].id))
    return tuple(entry for _, entry in ranked[:limit])


def get_entry(doc_id: str) -> DocEntry | None:
    """Return the entry with the exact id, or None when unknown."""
    for entry in _ENTRIES:
        if entry.id == doc_id:
            return entry
    return None
