"""Bounded Kubernetes LIST/WATCH transport; all clients belong to one owner.

The generated SDK's Watch iterator and non-2xx decoder read unbounded bodies.
Use its TLS/auth configuration and urllib3 pool, but bound raw reads before JSON.
No source HTTP call runs inside a database transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import urllib3
from fastapi import HTTPException
from kubernetes import client
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from dlr.control.schemas.subscription import KubernetesSourceConfig
from dlr.control.services import subscription_sync as sync_service
from dlr.control.services.event_envelope import canonical, event_id, validate_envelope
from dlr.control.services.subscription_kubernetes_identity import (
    KubernetesIdentityError,
    effective_ca_bytes,
    identity_with_ca,
    source_identity,
)
from dlr.control.services.subscription_queue_failure import PLATFORM_RECEPTION_FAILURES
from dlr.control.services.subscription_source import (
    ApplicationBufferBudget,
    ScopedBufferBudget,
    SourceFailure,
    SourceRunContext,
)

RAW_CHUNK_BYTES = 4096
LIST_PAGE_ITEMS = 16
MAX_LIST_PAGES = 5001
LIST_PAGE_BYTES = 1024**2
WATCH_LINE_BYTES = 512 * 1024
SOURCE_RV_BYTES = 4096
ERROR_BODY_BYTES = 8192
CONNECT_SECONDS = 3.0
READ_SECONDS = 2.0
LIST_DEADLINE_SECONDS = 5.0
WATCH_DEADLINE_SECONDS = 5.0
_SDK_LOG_LOCK = threading.Lock()
_SDK_LOG_SINK = logging.NullHandler()


class _DiscardSdkRecord(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


_SDK_LOG_FILTER = _DiscardSdkRecord()


def isolate_sdk_logging() -> None:
    """Discard original records, including handlers attached directly by config."""
    with _SDK_LOG_LOCK:
        logging.getLogger("kubernetes")
        for name, logger in logging.Logger.manager.loggerDict.copy().items():
            if (name == "kubernetes" or name.startswith("kubernetes.")) and isinstance(
                logger, logging.Logger
            ):
                logger.addFilter(_SDK_LOG_FILTER)
                logger.handlers = [_SDK_LOG_SINK]
                logger.propagate = False


def classify_error(error: Exception) -> SourceFailure:
    if isinstance(error, SourceFailure):
        return error
    if isinstance(error, KubernetesIdentityError):
        return SourceFailure(error.code)
    if isinstance(error, (ValidationError, ValueError, UnicodeError, RecursionError)):
        return SourceFailure("source_content_invalid")
    if isinstance(error, HTTPException):
        detail = error.detail
        code = detail.get("code", "source_database_unavailable") if isinstance(detail, dict) else ""
        if code in PLATFORM_RECEPTION_FAILURES:
            return SourceFailure(code, "backoff")
        return SourceFailure(
            code or "source_database_unavailable",
            "capacity" if code == "subscription_capacity_exceeded" else "blocked",
        )
    if isinstance(error, SQLAlchemyError):
        return SourceFailure("source_database_unavailable", "backoff")
    if isinstance(error, urllib3.exceptions.SSLError):
        return SourceFailure("source_tls_invalid")
    if isinstance(error, urllib3.exceptions.MaxRetryError):
        return (
            classify_error(error.reason)
            if isinstance(error.reason, Exception)
            else SourceFailure("source_unavailable", "backoff")
        )
    if isinstance(error, (urllib3.exceptions.HTTPError, OSError, TimeoutError)):
        return SourceFailure("source_unavailable", "backoff")
    return SourceFailure("source_protocol_unsupported")


def resource_path(config: dict[str, Any]) -> str:
    prefix = "/api/v1" if config["group"] == "" else "/apis/apps/v1"
    namespace = config.get("namespace")
    if namespace is not None:
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace):
            raise SourceFailure("source_scope_invalid")
        prefix += "/namespaces/" + quote(namespace, safe="")
    return prefix + "/" + str(config["resource"])


def _json_document(raw: bytes | bytearray) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("nonfinite JSON")

    result = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=constant)
    if not isinstance(result, dict):
        raise SourceFailure("source_content_invalid")
    return result


class KubernetesTransport:
    """SDK 35 / urllib3 2.8 raw boundary, no generated deserializers or pool threads."""

    def __init__(
        self,
        config: dict[str, Any],
        credential_fields: dict[str, str],
        identity_snapshot: dict[str, Any] | None = None,
        budget: ScopedBufferBudget | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.api: Any = None
        self.pool: Any = None
        self.ca_file: Any = None
        self._close_failed = False
        self.closed = False
        self.budget = budget if budget is not None else ApplicationBufferBudget().scope()
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        try:
            self.config = KubernetesSourceConfig.model_validate(config).model_dump()
            bundle = effective_ca_bytes(self.config)
            self.identity = identity_with_ca(self.config, bundle)
            if identity_snapshot is not None and self.identity != identity_snapshot:
                raise SourceFailure("source_identity_changed")
            # Lifetime spans all owner requests; close() unlinks it after native close.
            self.ca_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
                prefix="dlr-source-ca-", mode="w+b"
            )
            self.ca_file.write(bundle)
            self.ca_file.flush()
            isolate_sdk_logging()
            native = client.Configuration()
            native.host = self.identity["origin"]
            native.verify_ssl = True
            native.ssl_ca_cert = self.ca_file.name
            native.retries = False
            native.connection_pool_maxsize = 1
            native.debug = False
            token = credential_fields.get("token")
            if self.config.get("credential_id") is not None and not token:
                raise SourceFailure("source_credential_invalid")
            if token is not None:
                if not token or len(token.encode()) > 65536 or any(c in token for c in "\r\n\x00"):
                    raise SourceFailure("source_credential_invalid")
                native.api_key["authorization"] = token
                native.api_key_prefix["authorization"] = "Bearer"
            self.api = client.ApiClient(native)
            # Synchronous only: SDK ApiClient._pool remains uncreated.
            self.api.rest_client.pool_manager.connection_pool_kw["block"] = True
            self.pool = self.api.rest_client.pool_manager.connection_from_url(
                str(self.identity["origin"])
            )
            isolate_sdk_logging()
            self.path = resource_path(self.config)
        except Exception as error:
            self.close()
            raise classify_error(error) from None

    @contextmanager
    def _response(self, query: dict[str, Any]) -> Iterator[Any]:
        if self.stop_event.is_set():
            raise SourceFailure("source_stopped", "backoff")
        isolate_sdk_logging()
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        pairs = list(query.items())
        self.api.update_params_for_auth(headers, pairs, ["BearerToken"])
        # Bypass RESTClientObject.request: ApiException(http_resp=...) accesses
        # response.data even when _preload_content=False, including error bodies.
        response = self.api.rest_client.pool_manager.request(
            "GET",
            str(self.identity["origin"]) + self.path,
            fields=pairs,
            headers=headers,
            preload_content=False,
            timeout=urllib3.Timeout(connect=CONNECT_SECONDS, read=READ_SECONDS),
            retries=False,
            redirect=False,
            pool_timeout=1.0,
        )
        try:
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise SourceFailure("source_encoding_unsupported")
            if response.status != 200:
                # Never decode/log error bodies. Even a hostile endless body is
                # closed after at most one bounded read; status is authoritative.
                size = min(RAW_CHUNK_BYTES, ERROR_BODY_BYTES)
                if not self.budget.reserve(size):
                    raise SourceFailure("source_buffer_capacity", "capacity")
                try:
                    with suppress(urllib3.exceptions.HTTPError, OSError):
                        response.read1(amt=size, decode_content=False)
                finally:
                    self.budget.release(size)
                if response.status in {401, 403}:
                    raise SourceFailure("source_permission_denied")
                if response.status == 410:
                    raise SourceFailure("source_history_gap")
                raise SourceFailure(
                    "source_http_unavailable" if response.status >= 500 else "source_http_invalid",
                    "backoff" if response.status >= 500 or response.status == 429 else "blocked",
                )
            yield response
        finally:
            try:
                response.close()
            except Exception:
                self._close_failed = True
            finally:
                try:
                    response.release_conn()
                except Exception:
                    self._close_failed = True
            if self._close_failed:
                raise SourceFailure("source_close_unconfirmed", closed=False)

    def _documents(
        self, response: Any, *, lines: bool, cap: int, seconds: float
    ) -> Generator[dict[str, Any]]:
        deadline = time.monotonic() + seconds
        buffer = bytearray()
        held = 0
        try:
            while True:
                if self.stop_event.is_set():
                    raise SourceFailure("source_stopped", "backoff")
                if time.monotonic() >= deadline:
                    raise SourceFailure("source_read_deadline", "backoff")
                if not self.budget.reserve(RAW_CHUNK_BYTES):
                    raise SourceFailure("source_buffer_capacity", "capacity")
                try:
                    chunk = response.read1(amt=RAW_CHUNK_BYTES, decode_content=False)
                except Exception:
                    self.budget.release(RAW_CHUNK_BYTES)
                    raise
                if len(chunk) > RAW_CHUNK_BYTES:
                    self.budget.release(RAW_CHUNK_BYTES)
                    raise SourceFailure("source_protocol_unsupported")
                self.budget.release(RAW_CHUNK_BYTES - len(chunk))
                if not chunk:
                    break
                held += len(chunk)
                buffer.extend(chunk)
                if lines:
                    while b"\n" in buffer:
                        index = buffer.index(b"\n")
                        if index > cap:
                            raise SourceFailure("source_response_too_large")
                        line = buffer[:index]
                        del buffer[: index + 1]
                        try:
                            if line.strip():
                                yield _json_document(line)
                        finally:
                            self.budget.release(index + 1)
                            held -= index + 1
                if len(buffer) > cap:
                    raise SourceFailure("source_response_too_large")
            if buffer:
                if lines:
                    raise SourceFailure("source_watch_truncated", "backoff")
                yield _json_document(buffer)
            elif not lines:
                raise SourceFailure("source_content_invalid")
        finally:
            self.budget.release(held)

    def _selectors(self) -> dict[str, Any]:
        return {
            "labelSelector": self.config["label_selector"],
            "fieldSelector": self.config["field_selector"],
        }

    @contextmanager
    def list_page(self, token: str | None = None, *, limit: int = LIST_PAGE_ITEMS) -> Iterator[Any]:
        if isinstance(limit, bool) or not 1 <= limit <= LIST_PAGE_ITEMS:
            raise SourceFailure("source_list_limit_invalid")
        query = {**self._selectors(), "limit": limit}
        if token:
            if len(token.encode()) > 16384:
                raise SourceFailure("source_continue_invalid")
            query["continue"] = token
        with self._response(query) as response:
            documents = self._documents(
                response, lines=False, cap=LIST_PAGE_BYTES, seconds=LIST_DEADLINE_SECONDS
            )
            try:
                page = next(documents)
                items, metadata = page.get("items"), page.get("metadata")
                kind = "PodList" if self.config["resource"] == "pods" else "DeploymentList"
                api_version = "v1" if not self.config["group"] else "apps/v1"
                if (
                    page.get("kind") != kind
                    or page.get("apiVersion") != api_version
                    or not isinstance(items, list)
                    or len(items) > limit
                    or not isinstance(metadata, dict)
                    or not isinstance(metadata.get("resourceVersion"), str)
                    or not metadata["resourceVersion"]
                    or len(metadata["resourceVersion"].encode()) > SOURCE_RV_BYTES
                    or not isinstance(metadata.get("continue", ""), str)
                    or len(metadata.get("continue", "").encode()) > 16384
                ):
                    raise SourceFailure("source_list_invalid")
                yield page
            finally:
                documents.close()

    def watch(self, resource_version: str, *, seconds: int = 3) -> Generator[dict[str, Any]]:
        if not resource_version or len(resource_version.encode()) > SOURCE_RV_BYTES:
            raise SourceFailure("source_cursor_invalid")
        if isinstance(seconds, bool) or not 1 <= seconds <= 3:
            raise SourceFailure("source_watch_timeout_invalid")
        query = {
            **self._selectors(),
            "watch": "true",
            "resourceVersion": resource_version,
            "allowWatchBookmarks": "true",
            "timeoutSeconds": seconds,
        }
        with self._response(query) as response:
            yield from self._documents(
                response, lines=True, cap=WATCH_LINE_BYTES, seconds=WATCH_DEADLINE_SECONDS
            )

    def close(self) -> None:
        if self.closed:
            return
        failed = self._close_failed
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                failed = True
            finally:
                try:
                    # clear() alone drops the manager's references and can defer
                    # idle connection close to GC. Close our sole origin pool
                    # explicitly before clearing the SDK's manager.
                    if self.pool is not None:
                        self.pool.close()
                except Exception:
                    failed = True
                try:
                    self.api.rest_client.pool_manager.clear()
                except Exception:
                    failed = True
        if self.ca_file is not None:
            try:
                self.ca_file.close()
            except Exception:
                failed = True
        if failed:
            raise SourceFailure("source_close_unconfirmed", closed=False)
        self.closed = True


def build_envelope(
    context: SourceRunContext,
    obj: dict[str, Any],
    watch_type: str,
    *,
    sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if context.identity_snapshot is None:
        raise SourceFailure("source_identity_unverified")
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict) or watch_type not in {"LIST", "ADDED", "MODIFIED", "DELETED"}:
        raise SourceFailure("source_object_invalid")
    for key, maximum in (("name", 253), ("uid", 128), ("resourceVersion", SOURCE_RV_BYTES)):
        if not isinstance(metadata.get(key), str) or not 0 < len(metadata[key].encode()) <= maximum:
            raise SourceFailure("source_object_invalid")
    config = context.source_config
    namespace = metadata.get("namespace")
    if (
        not isinstance(namespace, str)
        or not namespace
        or (config.get("namespace") is not None and namespace != config["namespace"])
    ):
        raise SourceFailure("source_object_invalid")
    expected_kind = "Pod" if config["resource"] == "pods" else "Deployment"
    api_version = "v1" if not config["group"] else "apps/v1"
    # A typed LIST can omit each member's TypeMeta; its enclosing page has
    # already proved the GVR. Preserve that raw payload without adding fields.
    # Standalone WATCH objects still require their versioned object identity.
    for field, expected in (("kind", expected_kind), ("apiVersion", api_version)):
        if (watch_type != "LIST" or field in obj) and obj.get(field) != expected:
            raise SourceFailure("source_object_invalid")
    attrs = {
        "cluster_id": config["cluster_id"],
        "group": config["group"],
        "version": config["version"],
        "resource": config["resource"],
        "namespace": namespace,
        "name": metadata["name"],
        "uid": metadata["uid"],
        "watch_type": watch_type,
    }
    object_key = hashlib.sha256(
        canonical(
            [
                config["cluster_id"],
                config["group"],
                config["version"],
                config["resource"],
                namespace,
                metadata["uid"],
            ]
        )
    ).hexdigest()
    result: dict[str, Any] = {
        "schema": "dlr.event.v1",
        "subscription": {
            "id": str(context.subscription_id),
            "config_version": context.config_version,
            "source_epoch": context.source_epoch,
        },
        "event": {
            "id": "",
            "kind": "object.removed" if watch_type == "DELETED" else "object.present",
            "object_key": object_key,
        },
        "source": {
            "type": "kubernetes",
            "identity": source_identity(context.identity_snapshot),
            "position": {"resource_version": metadata["resourceVersion"]},
            "attributes": attrs,
        },
        "received_at": datetime.now(UTC).isoformat(),
        "data": {"encoding": "json", "content_type": "application/json", "value": obj},
        "sync": sync,
    }
    result["event"]["id"] = event_id(result)
    validate_envelope(result)
    return result


def watch_event(document: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Status/error/bookmark records never masquerade as object deletions."""
    event_type, obj = document.get("type"), document.get("object")
    if not isinstance(obj, dict):
        raise SourceFailure("source_watch_invalid")
    if event_type == "ERROR":
        code = obj.get("code")
        if code == 410:
            raise SourceFailure("source_history_gap")
        if code in {401, 403}:
            raise SourceFailure("source_permission_denied")
        raise SourceFailure("source_watch_error", "backoff")
    if event_type not in {"ADDED", "MODIFIED", "DELETED", "BOOKMARK"}:
        raise SourceFailure("source_watch_invalid")
    if event_type == "BOOKMARK":
        metadata = obj.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("resourceVersion"), str):
            raise SourceFailure("source_watch_invalid")
        if not 0 < len(metadata["resourceVersion"].encode()) <= SOURCE_RV_BYTES:
            raise SourceFailure("source_cursor_invalid")
    return event_type, obj


class KubernetesSourceRunner:
    def __init__(self, transport_factory: Callable[..., KubernetesTransport] = KubernetesTransport):
        self.transport_factory = transport_factory

    def test_connection(
        self, source_config: dict[str, Any], credential_fields: dict[str, str]
    ) -> dict[str, Any]:
        transport: KubernetesTransport | None = None
        try:
            transport = self.transport_factory(source_config, credential_fields)
            with transport.list_page(limit=1) as page:
                rv = page["metadata"]["resourceVersion"]
            # A short raw WATCH proves RBAC on the same exact scope; it creates
            # no Batch/Receipt/checkpoint and never grants a business capability.
            events = transport.watch(rv, seconds=1)
            try:
                for event in events:
                    watch_event(event)
            finally:
                events.close()
            config = transport.config
            return {
                "source_type": "kubernetes",
                "cluster_id": config["cluster_id"],
                "identity_verified": True,
                "kubernetes": {
                    "scope_fingerprint": transport.identity["scope_fingerprint"],
                    "trust_verified": True,
                    "list_verified": True,
                    "watch_verified": True,
                    **{key: config[key] for key in ("group", "version", "resource", "namespace")},
                },
            }
        except Exception as error:
            raise classify_error(error) from None
        finally:
            if transport is not None:
                transport.close()

    def run(self, context: SourceRunContext) -> None:
        if context.identity_snapshot is None:
            raise SourceFailure("source_identity_unverified")
        transport: KubernetesTransport | None = None
        try:
            transport = self.transport_factory(
                context.source_config,
                context.credential_fields,
                context.identity_snapshot,
                context.buffer,
                context.stop_event,
            )
            self._run(context, transport)
        except Exception as error:
            raise classify_error(error) from None
        finally:
            if transport is not None:
                transport.close()

    def _plan(self, context: SourceRunContext) -> sync_service.SyncSourcePlan:
        with context.session_factory.begin() as session:
            return sync_service.sync_source_plan(session, context)

    def _gap(self, context: SourceRunContext, code: str) -> None:
        if not context.stop_event.is_set():
            with context.session_factory.begin() as session:
                sync_service.note_sync_gap(session, context, code)

    def _run(self, context: SourceRunContext, transport: KubernetesTransport) -> None:
        while not context.stop_event.is_set():
            plan = self._plan(context)
            if plan.action == "blocked":
                raise SourceFailure(plan.code or "source_sync_blocked")
            if plan.action == "hold":
                context.stop_event.wait(0.25)
                continue
            if plan.action == "new_list":
                with context.session_factory.begin() as session:
                    ref = sync_service.begin_sync_batch(session, context, context.identity_snapshot)
                try:
                    self._list(context, transport, ref)
                except Exception as error:
                    failure = classify_error(error)
                    if failure.code not in {"source_stopped", "subscription_owner_stale"}:
                        self._gap(context, failure.code)
                    raise failure from None
                continue
            if plan.action != "watch" or not plan.safe_rv:
                raise SourceFailure("source_cursor_invalid")
            context.connected()
            try:
                self._watch(context, transport, plan.safe_rv)
            except Exception as error:
                failure = classify_error(error)
                if failure.code == "source_history_gap":
                    self._gap(context, failure.code)
                    continue
                # Plain WATCH disconnects resume the DB cursor. A malformed,
                # forbidden or oversized range loses full-sync qualification.
                if failure.disposition == "blocked" and failure.code not in {
                    "source_stopped",
                    "subscription_owner_stale",
                }:
                    self._gap(context, failure.code)
                raise failure from None

    @staticmethod
    def _sync(ref: sync_service.SyncRef | None, phase: str) -> dict[str, Any] | None:
        if ref is None:
            return None
        return {
            "batch_id": str(ref.batch_id),
            "scope_fingerprint": ref.scope_fingerprint,
            "phase": phase,
            "read_complete": ref.read_complete if phase == "watch" else False,
            "item_count": ref.item_count if phase == "watch" else 0,
            "failure_code": None,
        }

    def _receive(
        self,
        context: SourceRunContext,
        envelope: dict[str, Any],
        ref: sync_service.SyncRef | None = None,
        *,
        page_index: int = 0,
        collection_rv: str = "",
    ) -> sync_service.SyncAcceptResult:
        _, size = validate_envelope(envelope)
        if not context.buffer.reserve(size):
            raise SourceFailure("source_buffer_capacity", "capacity")
        try:
            while not context.stop_event.is_set():
                try:
                    with context.session_factory.begin() as session:
                        if ref is None:
                            result = sync_service.receive_watch_event(session, context, envelope)
                        else:
                            result = sync_service.receive_list_item(
                                session, context, ref, envelope, page_index, collection_rv
                            )
                    # Only successful transaction exit acknowledges custody to
                    # the caller. Never move page/stream state after rollback.
                    return result
                except HTTPException as error:
                    failure = classify_error(error)
                    if failure.code not in PLATFORM_RECEPTION_FAILURES | {
                        "subscription_capacity_exceeded"
                    }:
                        raise
                    with context.session_factory.begin() as session:
                        sync_service.mark_sync_backpressure(session, context)
                    context.stop_event.wait(0.25)
            raise SourceFailure("source_stopped", "backoff")
        finally:
            context.buffer.release(size)

    def _list(
        self, context: SourceRunContext, transport: KubernetesTransport, ref: sync_service.SyncRef
    ) -> None:
        token: str | None = None
        collection_rv: str | None = None
        for page_index in range(MAX_LIST_PAGES):
            if context.stop_event.is_set():
                raise SourceFailure("source_stopped", "backoff")
            with transport.list_page(token) as page:
                metadata = page["metadata"]
                rv = metadata["resourceVersion"]
                if collection_rv is not None and rv != collection_rv:
                    raise SourceFailure("source_list_snapshot_changed")
                collection_rv = rv
                context.connected()
                receipt_ids = []
                for obj in page["items"]:
                    if not isinstance(obj, dict):
                        raise SourceFailure("source_object_invalid")
                    envelope = build_envelope(context, obj, "LIST", sync=self._sync(ref, "list"))
                    accepted = self._receive(
                        context, envelope, ref, page_index=page_index, collection_rv=rv
                    )
                    receipt_ids.append(accepted.receipt_id)
                next_token = metadata.get("continue", "")
                if next_token and next_token == token:
                    raise SourceFailure("source_continue_invalid")
                with context.session_factory.begin() as session:
                    sync_service.finish_list_page(
                        session,
                        context,
                        ref,
                        page_index,
                        rv,
                        receipt_ids,
                        expected_item_count=len(page["items"]),
                        last_page=not next_token,
                    )
                if not next_token:
                    return
                token = next_token
        raise SourceFailure("source_list_page_limit")

    def _watch(
        self, context: SourceRunContext, transport: KubernetesTransport, resource_version: str
    ) -> None:
        events = transport.watch(resource_version)
        try:
            for document in events:
                if context.stop_event.is_set():
                    return
                event_type, obj = watch_event(document)
                plan = self._plan(context)
                if plan.action != "watch":
                    return
                if event_type == "BOOKMARK":
                    with context.session_factory.begin() as session:
                        sync_service.advance_bookmark(
                            session, context, obj["metadata"]["resourceVersion"], plan.safe_sequence
                        )
                else:
                    envelope = build_envelope(
                        context, obj, event_type, sync=self._sync(plan.batch, "watch")
                    )
                    self._receive(context, envelope)
        finally:
            events.close()
