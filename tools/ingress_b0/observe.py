"""Opt-in, bounded B0 observation; never import or configure DLR on import.

Repeated blocked scans are sampled per stage/identity, while exact aggregate
counts/times are retained. These are diagnostic observations, not a new runtime
protocol. SQL text, parameters, arguments and exception messages are never saved.
"""

from __future__ import annotations

import atexit
import contextvars
import functools
import importlib.abc
import inspect
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Self

ROLE_LIMIT = 12 * 1024 * 1024
DETAIL_PER_KEY = 2
MAX_DETAIL_KEYS = 12_000
SUMMARY_SECONDS = 5
_ID_KEYS = frozenset(
    {
        "subscription_id",
        "receipt_id",
        "delivery_id",
        "execution_id",
        "attempt_id",
        "adapter_id",
        "worker_id",
        "activation_id",
        "outbox_id",
        "message_id",
        "source_event_id",
        "dispatch_generation",
        "generation",
        "attempt_no",
        "version_id",
    }
)
_MODEL_IDS = {
    "EventReceipt": "receipt_id",
    "EventDelivery": "delivery_id",
    "Execution": "execution_id",
    "ExecutionAttempt": "attempt_id",
    "ExecutionOutbox": "outbox_id",
    "LeasedOutbox": "outbox_id",
    "EventSubscription": "subscription_id",
    "Adapter": "adapter_id",
    "SubscriptionActivation": "activation_id",
}
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,128}\Z")
_TABLES = frozenset(
    {
        "adapters",
        "adapter_versions",
        "event_subscriptions",
        "event_receipts",
        "event_deliveries",
        "subscription_quotas",
        "subscription_source_checkpoints",
        "source_checkpoints",
        "subscription_runtime_leases",
        "subscription_activations",
        "subscription_config_versions",
        "activation_credential_references",
        "executions",
        "execution_attempts",
        "execution_outbox",
        "execution_outboxes",
        "adapter_execution_slots",
        "execution_admission_counters",
        "admission_counters",
        "workers",
        "worker_cache_guards",
        "execution_artifact_holds",
        "subscription_leases",
        "subscription_sync_batches",
        "adapter_execution_admission",
        "global_execution_admission",
        "rabbitmq_runtime_capabilities",
        "execution_credential_binding_snapshots",
    }
)
_SQL_HEAD = re.compile(
    r"\s*(SELECT|INSERT|UPDATE|DELETE|WITH|COMMIT|ROLLBACK)\b", re.IGNORECASE
)
_SQL_TABLE = re.compile(r'\b(?:FROM|JOIN|UPDATE|INTO)\s+"?([a-z_]+)"?', re.IGNORECASE)
_IDS: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "b0_ids", default=None
)
_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "b0_stage", default="outside"
)
_ACTIVE: Observer | None = None


def _scalar(value: Any) -> bool:
    return (type(value) is int and 0 <= value < 2**63) or (
        type(value) is str and _SAFE_ID.fullmatch(value) is not None
    )


def identifiers(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Read only already-loaded dict storage, never ORM descriptors/properties."""
    if depth > 2:
        return {}
    if type(value) in (tuple, list):
        result: dict[str, Any] = {}
        for item in value[:8]:
            result.update(identifiers(item, depth=depth + 1))
        return result
    if type(value) is dict:
        data = value
    else:
        try:
            data = object.__getattribute__(value, "__dict__")
        except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            return {}
        if type(data) is not dict:
            return {}
    result = {key: data[key] for key in _ID_KEYS if key in data and _scalar(data[key])}
    name = type(value).__name__
    key = _MODEL_IDS.get(name)
    if key and "id" in data and _scalar(data["id"]):
        result[key] = data["id"]
    # A prepared-result dataclass can contain a validated payload. No general
    # recursive traversal of arbitrary input/configuration is permitted.
    if name in {"_PreparedExecution", "_PreparationResult"}:
        for nested in ("payload", "prepared"):
            if nested in data:
                result.update(identifiers(data[nested], depth=depth + 1))
    return result


def _merge(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, children in (
        (
            "source_event_id",
            ("receipt_id", "delivery_id", "execution_id", "attempt_id"),
        ),
        ("message_id", ("receipt_id", "delivery_id", "execution_id", "attempt_id")),
        ("delivery_id", ("receipt_id", "execution_id", "attempt_id")),
        ("execution_id", ("attempt_id",)),
    ):
        if key in incoming and key in target and incoming[key] != target[key]:
            for child in children:
                target.pop(child, None)
    target.update(incoming)


class Writer:
    """One role file, process-safe cap including a reserved exhaustion marker."""

    def __init__(self, directory: str | Path, role: str, max_bytes: int = ROLE_LIMIT):
        if role not in {"control", "worker"}:
            raise ValueError("invalid observation role")
        self.directory = Path(directory)
        self.role = role
        self.max_bytes = min(ROLE_LIMIT, max(1024, max_bytes))
        self.dropped = 0
        self.errors = 0
        self._lock = threading.RLock()

    def write(self, record: dict[str, Any]) -> None:
        # All callers supply only allowlisted scalars, not default=str/repr.
        try:
            encoded = (
                json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode()
                + b"\n"
            )
            if len(encoded) > 24_576:
                self.dropped += 1
                return
            import fcntl

            with self._lock:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    self.directory / f"{self.role}.jsonl",
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_APPEND
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    size = os.fstat(descriptor).st_size
                    if size + len(encoded) <= self.max_bytes - 256:
                        os.write(descriptor, encoded)
                    else:
                        self.dropped += 1
                        marker = (
                            b'{"kind":"budget_exhausted","detail_complete":false}\n'
                        )
                        if (
                            size <= self.max_bytes - 256
                            and size + len(marker) <= self.max_bytes
                        ):
                            os.write(descriptor, marker)
                finally:
                    os.close(descriptor)
        except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            # Diagnosis must not turn a successful transaction/ACK into a failure.
            self.errors += 1


class Observer:
    def __init__(self, writer: Writer):
        self.writer = writer
        self._lock = threading.RLock()
        self._details: Counter[tuple[Any, ...]] = Counter()
        self._aggregates: dict[str, dict[str, int]] = {}
        self._last_summary = time.monotonic()
        self.wrappers: dict[Any, Any] = {}
        self.patched: set[str] = set()
        self.sql_registered = False

    def emit(self, kind: str, **values: Any) -> None:
        self.writer.write(
            {
                "kind": kind,
                "role": self.writer.role,
                "pid": os.getpid(),
                "thread": threading.get_ident(),
                "utc_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                **values,
            }
        )

    def detail(
        self, stage: str, ids: dict[str, Any], extra: tuple[Any, ...] = ()
    ) -> bool:
        key = (stage, tuple(sorted(ids.items())), *extra)
        with self._lock:
            if key not in self._details and len(self._details) >= MAX_DETAIL_KEYS:
                return False
            self._details[key] += 1
            return self._details[key] <= DETAIL_PER_KEY

    def aggregate(self, stage: str, elapsed_ns: int, error: bool) -> None:
        with self._lock:
            item = self._aggregates.setdefault(
                stage,
                {
                    "count": 0,
                    "sum_ns": 0,
                    "min_ns": elapsed_ns,
                    "max_ns": elapsed_ns,
                    "errors": 0,
                },
            )
            item["count"] += 1
            item["sum_ns"] += elapsed_ns
            item["min_ns"] = min(item["min_ns"], elapsed_ns)
            item["max_ns"] = max(item["max_ns"], elapsed_ns)
            item["errors"] += int(error)
            if time.monotonic() - self._last_summary >= SUMMARY_SECONDS:
                self.flush()

    def flush(self) -> None:
        with self._lock:
            items = self._aggregates
            self._aggregates = {}
            self._last_summary = time.monotonic()
        # One record per stage avoids losing the entire summary to a large row.
        for stage, item in sorted(items.items()):
            self.emit("aggregate", stage=stage, **item)

    def span(
        self, stage: str, function: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Span:
        return Span(self, stage, function, args, kwargs)

    def wrap(self, function: Any, stage: str) -> Any:
        if function in self.wrappers:
            return self.wrappers[function]
        if getattr(function, "__b0_wrapped__", False):
            return function
        if inspect.isasyncgenfunction(function):
            # No B0 target is an async generator. Do not replace its protocol
            # with async-for, which would lose asend/athrow/close semantics.
            return function
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                with self.span(stage, function, args, kwargs) as span:
                    result = await function(*args, **kwargs)
                    span.result(result)
                    return result
        elif inspect.isgeneratorfunction(function):

            @functools.wraps(function)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                # Do not hold a ContextVar token while the generator is suspended:
                # callers may resume/close it from another context. Each resume
                # is timed independently and preserves send/throw/close.
                return (yield from _GeneratorProxy(self, stage, function, args, kwargs))
        else:

            @functools.wraps(function)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                with self.span(stage, function, args, kwargs) as span:
                    result = function(*args, **kwargs)
                    span.result(result)
                    return result

        wrapped.__b0_wrapped__ = True
        self.wrappers[function] = wrapped
        return wrapped


class _GeneratorProxy:
    def __init__(
        self,
        observer: Observer,
        stage: str,
        function: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ):
        self.observer, self.stage, self.function = observer, stage, function
        self.args, self.kwargs = args, kwargs
        self.generator = function(*args, **kwargs)

    def __iter__(self) -> _GeneratorProxy:
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def _step(self, operation: str, *values: Any) -> Any:
        terminal = None
        with self.observer.span(
            self.stage + ":" + operation, self.function, self.args, self.kwargs
        ) as span:
            try:
                result = getattr(self.generator, operation)(*values)
            except StopIteration as stop:
                span.result(stop.value)
                terminal = stop
            else:
                span.result(result)
        if terminal is not None:
            raise terminal
        return result

    def send(self, value: Any) -> Any:
        return self._step("send", value)

    def throw(self, *values: Any) -> Any:
        return self._step("throw", *values)

    def close(self) -> Any:
        return self._step("close")


class Span:
    def __init__(
        self,
        observer: Observer,
        stage: str,
        function: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ):
        self.observer, self.stage = observer, stage
        self.parent = _IDS.get()
        self.ids = dict(self.parent or {})
        for value in args[:8]:
            _merge(self.ids, identifiers(value))
        # Parameter names let integer IDs be associated without serializing args.
        try:
            names = function.__code__.co_varnames[: function.__code__.co_argcount]
        except AttributeError:
            names = ()
        for name, value in zip(names, args):
            if name in _ID_KEYS and _scalar(value):
                _merge(self.ids, {name: value})
        for name, value in kwargs.items():
            if name in _ID_KEYS and _scalar(value):
                _merge(self.ids, {name: value})
            else:
                _merge(self.ids, identifiers(value))

    def __enter__(self) -> Self:
        self.start_ns = time.monotonic_ns()
        self.utc_start_ns = time.time_ns()
        self.ids_token = _IDS.set(self.ids)
        self.stage_token = _STAGE.set(self.stage)
        self.has_begin = self.observer.detail(self.stage, self.ids)
        if self.has_begin:
            self.observer.emit(
                "span",
                stage=self.stage,
                phase="begin",
                ids=self.ids.copy(),
                start_ns=self.start_ns,
            )
        return self

    def result(self, value: Any) -> None:
        _merge(self.ids, identifiers(value))

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        elapsed = time.monotonic_ns() - self.start_ns
        outcome = (
            "ok" if kind is None else ("closed" if kind is GeneratorExit else "error")
        )
        self.observer.aggregate(self.stage, elapsed, outcome == "error")
        if self.has_begin or self.observer.detail(self.stage + ":end", self.ids):
            self.observer.emit(
                "span",
                stage=self.stage,
                phase="end",
                ids=self.ids.copy(),
                start_ns=self.start_ns,
                utc_start_ns=self.utc_start_ns,
                elapsed_ns=elapsed,
                outcome=outcome,
                error_type=kind.__name__[:64] if kind else None,
            )
        _IDS.reset(self.ids_token)
        _STAGE.reset(self.stage_token)
        if self.parent is not None:
            _merge(self.parent, self.ids)
        return False


_TARGETS: dict[str, tuple[str, ...]] = {
    "dlr.control.services.subscription_queue": (
        "envelope",
        "capacity_ready",
        "take_custody",
    ),
    "dlr.control.services.subscription_rabbitmq": (
        "RabbitMQTransport.poll",
        "RabbitMQTransport.ack",
    ),
    "dlr.control.services.subscription_delivery": (
        "lock_subscription",
        "_quotas",
        "_reserve",
        "receive_event",
        "_delivery_context",
        "dispatch_delivery",
        "observe_terminal",
        "refresh_delivery_state",
        "cleanup_history",
    ),
    "dlr.control.services.subscription_management": (
        "bounded_page",
        "maintenance_tick",
    ),
    "dlr.control.services.reliable_execution": ("accept_execution",),
    "dlr.control.services.outbox": (
        "lease_due_outbox",
        "relay_once",
        "_relay_once",
        "_publish_row",
        "mark_outbox_published",
        "_close_connection_bounded",
    ),
    "dlr.control.services.rabbitmq": ("connect", "bootstrap_worker_topology"),
    "dlr.control.services.attempt": (
        "claim_dispatch",
        "start_attempt",
        "finish_attempt",
    ),
    "dlr.worker.consumer": (
        "V3Consumer._on_delivery",
        "V3Consumer._process_ticket",
        "V3Consumer._prepare_execute",
        "V3Consumer._run_attempt",
        "V3Consumer._send_disposition",
        "V3Consumer._complete_ticket",
    ),
    "dlr.worker.client": (
        "ControlClient.claim_v3",
        "ControlClient.start_attempt",
        "ControlClient.result_attempt",
        "ControlClient.report_cleanup_receipt",
    ),
    "dlr.worker.executor": ("run",),
    "dlr.worker.workspace": (
        "write_attempt_journal",
        "write_cleanup_journal",
        "create_workspace",
        "prepare_input_files",
        "validate_input_manifest",
        "cleanup_workspace",
        "remove_attempt_journal",
        "remove_cleanup_journal",
    ),
    "dlr.worker.sandbox": (
        "AttemptSandbox.__init__",
        "AttemptSandbox.mount_dependency_tmpfs",
        "AttemptSandbox.unmount_dependency_tmpfs",
        "AttemptSandbox.start",
        "AttemptSandbox.cleanup",
    ),
    "dlr.worker.venv": ("prepare_version_venv",),
    "sqlalchemy.pool.impl": ("QueuePool._do_get",),
    "sqlalchemy.orm.session": ("Session.commit",),
    "pika.adapters.blocking_connection": (
        "BlockingConnection.__init__",
        "BlockingConnection.channel",
        "BlockingConnection.close",
        "BlockingChannel.confirm_delivery",
        "BlockingChannel.basic_publish",
        "BlockingChannel.basic_ack",
        "BlockingChannel.basic_nack",
        "BlockingChannel.close",
    ),
}


def _sql_classification(statement: Any) -> dict[str, Any]:
    if type(statement) is not str:
        return {"operation": "other", "tables": [], "for_update": False}
    head = statement[:8192]
    match = _SQL_HEAD.match(head)
    tables = sorted({m.group(1).lower() for m in _SQL_TABLE.finditer(head)} & _TABLES)[
        :4
    ]
    return {
        "operation": match.group(1).lower() if match else "other",
        "tables": tables,
        "for_update": bool(
            re.search(r"\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\b", head, re.IGNORECASE)
        ),
    }


def _sql_events(observer: Observer) -> None:
    module = sys.modules.get("sqlalchemy")
    if module is None or getattr(
        getattr(module, "__spec__", None), "_initializing", False
    ):
        return
    if observer.sql_registered:
        return
    try:
        event = module.event
        engine = module.engine.Engine
    except AttributeError:
        return

    def before(
        _conn: Any, _cursor: Any, statement: Any, _params: Any, context: Any, _many: Any
    ) -> None:
        try:
            context._dlr_b0_observation = (
                time.monotonic_ns(),
                time.time_ns(),
                _STAGE.get(),
                dict(_IDS.get() or {}),
                _sql_classification(statement),
            )
        except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            observer.emit(
                "hook", target="sqlalchemy.before_cursor_execute", outcome="failed"
            )

    def finish(context: Any, error: bool) -> None:
        data = getattr(context, "_dlr_b0_observation", None)
        if data is None:
            return
        del context._dlr_b0_observation
        start, utc_start, parent, ids, classification = data
        elapsed = time.monotonic_ns() - start
        category = (
            "sql:"
            + parent
            + ":"
            + classification["operation"]
            + ":"
            + ",".join(classification["tables"])
            + (":for_update" if classification["for_update"] else "")
        )
        observer.aggregate(category, elapsed, error)
        if observer.detail(category, ids):
            observer.emit(
                "sql",
                stage=parent,
                phase="end",
                start_ns=start,
                utc_start_ns=utc_start,
                elapsed_ns=elapsed,
                ids=ids,
                outcome="error" if error else "ok",
                **classification,
            )

    def after(
        _conn: Any,
        _cursor: Any,
        _statement: Any,
        _params: Any,
        context: Any,
        _many: Any,
    ) -> None:
        try:
            finish(context, False)
        except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            observer.emit(
                "hook", target="sqlalchemy.after_cursor_execute", outcome="failed"
            )

    def failed(error_context: Any) -> None:
        try:
            if error_context.execution_context is not None:
                finish(error_context.execution_context, True)
        except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            observer.emit("hook", target="sqlalchemy.handle_error", outcome="failed")

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failed)
    observer.sql_registered = True
    observer.emit("hook", target="sqlalchemy.engine.Engine.events", outcome="installed")


def _patch(observer: Observer, module: ModuleType) -> None:
    name = module.__name__
    for dotted in _TARGETS.get(name, ()):
        identifier = name + "." + dotted
        if identifier in observer.patched:
            continue
        owner: Any = module
        parts = dotted.split(".")
        try:
            for part in parts[:-1]:
                owner = vars(owner)[part]
            original = vars(owner)[parts[-1]]
        except (KeyError, TypeError):
            observer.emit("hook", target=identifier, outcome="missing")
            observer.patched.add(identifier)
            continue
        if not inspect.isfunction(original):
            observer.emit("hook", target=identifier, outcome="unsupported")
            observer.patched.add(identifier)
            continue
        wrapped = observer.wrap(original, identifier)
        setattr(owner, parts[-1], wrapped)
        # Existing 'from x import f' aliases must use the exact same wrapper.
        for loaded_name, loaded in tuple(sys.modules.items()):
            if not loaded_name.startswith("dlr.") or loaded is None:
                continue
            for alias, value in tuple(vars(loaded).items()):
                if value is original:
                    setattr(loaded, alias, wrapped)
        observer.patched.add(identifier)
        observer.emit("hook", target=identifier, outcome="installed")
    _sql_events(observer)


class _Loader(importlib.abc.Loader):
    def __init__(self, original: Any, observer: Observer):
        self.original, self.observer = original, observer

    def create_module(self, spec: Any) -> Any:
        create = getattr(self.original, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module: ModuleType) -> None:
        self.original.exec_module(module)
        try:
            _patch(self.observer, module)
        except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            self.observer.emit("hook", target=module.__name__, outcome="failed")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, observer: Observer):
        self.observer = observer

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _TARGETS and fullname != "sqlalchemy":
            return None
        for finder in tuple(sys.meta_path):
            if finder is self:
                continue
            method = getattr(finder, "find_spec", None)
            if method is None:
                continue
            spec = method(fullname, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _Loader(spec.loader, self.observer)
                return spec
        return None


def install() -> Observer | None:
    """Only the explicit role AND private output directory enable observation."""
    global _ACTIVE
    directory = os.environ.get("DLR_B0_DIAGNOSTICS_DIR")
    role = os.environ.get("DLR_B0_ROLE")
    if not directory or role not in {"control", "worker"}:
        return None
    if _ACTIVE is not None:
        return _ACTIVE
    observer = Observer(Writer(directory, role))
    _ACTIVE = observer
    observer.emit(
        "configuration",
        detail_per_identity=DETAIL_PER_KEY,
        sql_detail_sampled=True,
        aggregate_exact=True,
        summary_seconds=SUMMARY_SECONDS,
        max_bytes=ROLE_LIMIT,
    )
    sys.meta_path.insert(0, _Finder(observer))
    for name in (*_TARGETS, "sqlalchemy"):
        module = sys.modules.get(name)
        if module is not None:
            _patch(observer, module)
    atexit.register(observer.flush)
    return observer
