"""Kubernetes identity and raw HTTP safety, without mutating a source cluster."""

import io
import json
import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import urllib3
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SyncBatch,
    Worker,
)
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_kubernetes as kube
from dlr.control.services import subscription_kubernetes_identity as identity
from dlr.control.services.subscription_source import (
    ApplicationBufferBudget,
    SourceFailure,
    SourceRunContext,
)


@pytest.fixture(scope="module")
def ca():
    def make(serial):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"test-{serial}")])
        return (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(serial)
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )

    return make(1), make(2)


@pytest.fixture
def config(ca):
    return {
        "source_type": "kubernetes",
        "cluster_id": "test-cluster",
        "server": "https://example.invalid",
        "group": "",
        "version": "v1",
        "resource": "pods",
        "namespace": "test",
        "label_selector": "app in (a,b)",
        "field_selector": "",
        "ca_pem": ca[0].decode(),
    }


def test_ca_set_and_origin_are_stable_but_scope_and_trust_changes_are_not(config, ca):
    original = identity.kubernetes_scope_identity(config)
    equivalent = {
        **config,
        "server": "https://EXAMPLE.invalid:443/",
        "ca_pem": (ca[0] + ca[0]).decode(),
        "credential_id": 99,
        "mode": "state_sync",
        "limits": {"pending_count": 1},
    }
    assert identity.kubernetes_scope_identity(equivalent) == original
    both = identity.kubernetes_scope_identity({**config, "ca_pem": (ca[0] + ca[1]).decode()})
    reverse = identity.kubernetes_scope_identity({**config, "ca_pem": (ca[1] + ca[0]).decode()})
    assert both == reverse and both != original
    for key, value in [
        ("namespace", None),
        ("label_selector", "app=b"),
        ("server", "https://different.invalid"),
        ("cluster_id", "another"),
    ]:
        assert identity.kubernetes_scope_identity({**config, key: value}) != original
    assert set(original) == {"version", "origin", "trust_sha256", "scope_fingerprint"}
    assert (
        identity.source_identity(original) == "kubernetes-scope-v1:" + original["scope_fingerprint"]
    )


@pytest.mark.parametrize(
    "server",
    [
        "http://example.invalid",
        "https://user:pass@example.invalid",
        "https://example.invalid/api",
        "https://example.invalid?q=secret",
        "https://example.invalid#x",
        "https://example.invalid:0",
        "https://example.invalid\n",
        "https://[fe80::1%en0]",
    ],
)
def test_origin_rejects_non_origin_and_credential_inputs(server):
    with pytest.raises(identity.KubernetesIdentityError, match="source_server_invalid"):
        identity.normalized_origin(server)


def test_default_ca_is_actual_certifi_bytes_and_not_constant(config, ca, tmp_path, monkeypatch):
    trust_file = tmp_path / "trust.pem"
    trust_file.write_bytes(ca[0])
    monkeypatch.setattr(identity.certifi, "where", lambda: str(trust_file))
    config["ca_pem"] = None
    first = identity.kubernetes_scope_identity(config)
    trust_file.write_bytes(ca[1])
    assert identity.kubernetes_scope_identity(config) != first
    trust_file.write_bytes(b"x" * (identity.CA_BUNDLE_MAX_BYTES + 1))
    with pytest.raises(identity.KubernetesIdentityError, match="source_trust_invalid"):
        identity.kubernetes_scope_identity(config)


def test_deployment_matrix_uses_apps_endpoint_and_matching_object_kind(config):
    config.update(group="apps", resource="deployments")
    transport = kube.KubernetesTransport(config, {})
    try:
        assert transport.path == "/apis/apps/v1/namespaces/test/deployments"
        deployment = obj()
        deployment.update(apiVersion="apps/v1", kind="Deployment")
        result = kube.build_envelope(context(config), deployment, "MODIFIED")
        assert result["source"]["attributes"]["resource"] == "deployments"
        with pytest.raises(SourceFailure, match="source_object_invalid"):
            kube.build_envelope(context(config), obj(), "MODIFIED")
    finally:
        transport.close()


class Response:
    def __init__(self, raw, status=200, encoding="identity"):
        self.raw = io.BytesIO(raw)
        self.headers = {"Content-Encoding": encoding}
        self.status = status
        self.reads = []
        self.closed = False
        self.released = False

    @property
    def data(self):
        raise AssertionError("Unbounded data access")

    def read1(self, *, amt, decode_content):
        assert decode_content is False and amt <= 4096
        self.reads.append(amt)
        return self.raw.read(amt)

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


@pytest.fixture
def transport(config):
    result = kube.KubernetesTransport(config, {})
    yield result
    result.close()
    assert result.budget.used_bytes == 0


def attach(transport, response, monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr(transport.api.rest_client.pool_manager, "request", request)
    return calls


def test_real_sdk_auth_is_used_but_never_its_unbounded_decoder(config, monkeypatch):
    config["credential_id"] = 42
    transport = kube.KubernetesTransport(config, {"token": "private-token"})
    raw = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "PodList",
            "metadata": {"resourceVersion": "opaque-rv"},
            "items": [],
        }
    ).encode()
    response = Response(raw)
    calls = attach(transport, response, monkeypatch)
    try:
        with transport.list_page() as page:
            assert page["metadata"]["resourceVersion"] == "opaque-rv"
            assert transport.budget.used_bytes == len(raw)
        options = calls[0][1]
        assert options["headers"]["authorization"] == "Bearer private-token"
        assert options["headers"]["Accept-Encoding"] == "identity"
        assert options["preload_content"] is options["retries"] is options["redirect"] is False
        assert options["timeout"].read_timeout == kube.READ_SECONDS
        assert response.closed and response.released and transport.budget.used_bytes == 0
        assert transport.api._pool is None
    finally:
        transport.close()


@pytest.mark.parametrize("status", [401, 403, 410, 500])
def test_non_2xx_body_is_closed_without_decode_or_unbounded_read(transport, monkeypatch, status):
    response = Response(b"private-raw-body" * 100000, status=status)
    attach(transport, response, monkeypatch)
    with pytest.raises(SourceFailure) as result, transport.list_page():
        pytest.fail("must not yield")
    assert (
        result.value.code
        == {
            401: "source_permission_denied",
            403: "source_permission_denied",
            410: "source_history_gap",
            500: "source_http_unavailable",
        }[status]
    )
    assert response.reads == [4096] and response.closed and response.released
    assert transport.budget.used_bytes == 0


@pytest.mark.parametrize("lines", [True, False])
def test_success_limit_precedes_json_decoding_and_releases_budget(transport, monkeypatch, lines):
    response = Response(b"x" * 20000)
    decoder_called = False

    def decode(raw):
        nonlocal decoder_called
        decoder_called = True
        raise AssertionError("decode before cap")

    monkeypatch.setattr(kube, "_json_document", decode)
    with pytest.raises(SourceFailure, match="source_response_too_large"):
        list(transport._documents(response, lines=lines, cap=8192, seconds=5))
    assert not decoder_called and transport.budget.used_bytes == 0
    assert len(response.reads) == 3


def test_watch_raw_lines_release_before_next_and_truncated_tail_is_not_an_event(transport):
    first = {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "a"}}}
    raw = json.dumps(first).encode() + b"\n" + b'{"type":"ADDED"'
    response = Response(raw)
    documents = transport._documents(response, lines=True, cap=8192, seconds=5)
    assert next(documents) == first
    assert transport.budget.used_bytes == len(raw)
    with pytest.raises(SourceFailure, match="source_watch_truncated"):
        next(documents)
    assert transport.budget.used_bytes == 0


def test_read1_api_on_real_urllib3_response_is_chunked_and_finite(transport):
    response = urllib3.HTTPResponse(body=io.BytesIO(b'{"ok":true}'), preload_content=False)
    assert list(transport._documents(response, lines=False, cap=8192, seconds=5)) == [{"ok": True}]
    response.close()
    assert transport.budget.used_bytes == 0


def test_slow_continuous_bytes_hit_total_deadline_not_only_socket_timeout(transport, monkeypatch):
    clock = iter([0, 1, 2, 3, 4, 5, 6])
    monkeypatch.setattr(kube.time, "monotonic", lambda: next(clock))
    response = Response(b"x" * 30000)
    with pytest.raises(SourceFailure, match="source_read_deadline"):
        list(transport._documents(response, lines=False, cap=65536, seconds=3))
    assert len(response.reads) == 2 and transport.budget.used_bytes == 0


def test_compressed_success_is_not_decoded(transport, monkeypatch):
    response = Response(b"compressed", encoding="gzip")
    attach(transport, response, monkeypatch)
    with pytest.raises(SourceFailure, match="source_encoding_unsupported"), transport.list_page():
        pass
    assert not response.reads and response.closed


@pytest.mark.parametrize("characters,accepted", [(1365, True), (1366, False)])
def test_cursor_limit_is_utf8_bytes_and_does_not_interpret_opaque_values(
    transport, monkeypatch, characters, accepted
):
    response = Response(b"")
    calls = attach(transport, response, monkeypatch)
    cursor = "界" * characters
    if accepted:
        assert list(transport.watch(cursor)) == []
        assert dict(calls[0][1]["fields"])["resourceVersion"] == cursor
    else:
        with pytest.raises(SourceFailure, match="source_cursor_invalid"):
            list(transport.watch(cursor))
        assert not calls


def test_close_always_clears_sdk_pool_and_only_confirms_real_close(transport, monkeypatch):
    called = []
    path = transport.ca_file.name
    monkeypatch.setattr(transport.api, "close", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(transport.api.rest_client.pool_manager, "clear", lambda: called.append(1))
    with pytest.raises(SourceFailure) as result:
        transport.close()
    assert called == [1] and result.value.closed is False and transport.ca_file.closed
    assert not transport.closed
    monkeypatch.setattr(transport.api, "close", lambda: None)
    transport.close()
    from pathlib import Path

    assert not Path(path).exists()


def test_constructor_and_direct_sdk_debug_handlers_do_not_leak(config, monkeypatch, caplog):
    logger = logging.getLogger("kubernetes.client.rest")
    logger.handlers = [caplog.handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    original = kube.client.ApiClient

    def constructor(native):
        logger.debug("private-token private-raw-body")
        return original(native)

    monkeypatch.setattr(kube.client, "ApiClient", constructor)
    transport = kube.KubernetesTransport(config, {"token": "private-token"})
    logger.handlers = [caplog.handler]
    logger.debug("private-token private-raw-body")
    transport.close()
    logging.getLogger("dlr.test").warning("source_permission_denied")
    assert "private-token" not in caplog.text and "private-raw-body" not in caplog.text
    assert "source_permission_denied" in caplog.text


def context(config):
    return SimpleNamespace(
        subscription_id=1,
        config_version=1,
        source_epoch="epoch",
        source_config=config,
        identity_snapshot=identity.kubernetes_scope_identity(config),
    )


def obj(uid="uid-one", rv="opaque-rv"):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "same", "namespace": "test", "uid": uid, "resourceVersion": rv},
    }


def sync(batch):
    return {
        "batch_id": str(batch),
        "scope_fingerprint": "scope",
        "phase": "watch",
        "read_complete": True,
        "item_count": 1,
        "failure_code": None,
    }


def test_uid_scope_and_watch_batch_identity_are_explicit_and_rv_is_opaque(config):
    ctx = context(config)
    first = kube.build_envelope(ctx, obj(), "MODIFIED")
    associated = kube.build_envelope(ctx, obj(), "MODIFIED", sync=sync(1))
    newer_batch = kube.build_envelope(ctx, obj(), "MODIFIED", sync=sync(2))
    assert first["event"]["id"] == associated["event"]["id"] == newer_batch["event"]["id"]
    assert first["sync"] is None
    assert (
        kube.build_envelope(ctx, obj(uid="new-uid"), "MODIFIED")["event"]["object_key"]
        != (first["event"]["object_key"])
    )
    assert kube.build_envelope(ctx, obj(), "DELETED")["event"]["kind"] == "object.removed"
    assert (
        kube.build_envelope(ctx, obj(), "LIST", sync=sync(1))["event"]["id"]
        != (kube.build_envelope(ctx, obj(), "LIST", sync=sync(2))["event"]["id"])
    )


def test_final_envelope_uses_existing_execution_limit(config, monkeypatch):
    monkeypatch.setattr(settings, "execution_input_max_bytes", 512)
    with pytest.raises(Exception) as result:
        kube.build_envelope(context(config), obj(), "ADDED")
    assert result.value.detail["code"] == "event_payload_too_large"


def test_changed_default_trust_is_rejected_before_native_client_creation(config, ca, monkeypatch):
    snapshot = identity.kubernetes_scope_identity(config)
    config["ca_pem"] = ca[1].decode()
    called = []
    monkeypatch.setattr(kube.client, "ApiClient", lambda native: called.append(True))
    with pytest.raises(SourceFailure, match="source_identity_changed"):
        kube.KubernetesTransport(config, {}, snapshot)
    assert not called


@pytest.mark.parametrize(
    "document,code",
    [
        (
            {"type": "ERROR", "object": {"code": 410, "message": "private-raw"}},
            "source_history_gap",
        ),
        ({"type": "ERROR", "object": {"code": 403}}, "source_permission_denied"),
        ({"type": "ERROR", "object": {"code": 500}}, "source_watch_error"),
        ({"type": "DELETED", "object": None}, "source_watch_invalid"),
        (
            {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": 123}}},
            "source_watch_invalid",
        ),
    ],
)
def test_watch_errors_and_bookmarks_are_not_business_delete(document, code):
    with pytest.raises(SourceFailure, match=code):
        kube.watch_event(document)


def test_connection_uses_real_sdk_bounded_list_watch_and_returns_only_checked_facts(
    config, monkeypatch
):
    responses = [
        Response(
            b'{"apiVersion":"v1","kind":"PodList","metadata":{"resourceVersion":"opaque-list-rv"},"items":[]}'
        ),
        Response(b""),
    ]
    created = []
    calls = []

    def factory(*args):
        transport = kube.KubernetesTransport(*args)
        created.append(transport)

        def request(*args, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

        monkeypatch.setattr(transport.api.rest_client.pool_manager, "request", request)
        return transport

    result = kube.KubernetesSourceRunner(factory).test_connection(config, {})
    assert result == {
        "source_type": "kubernetes",
        "cluster_id": "test-cluster",
        "identity_verified": True,
        "kubernetes": {
            "scope_fingerprint": created[0].identity["scope_fingerprint"],
            "trust_verified": True,
            "list_verified": True,
            "watch_verified": True,
            "group": "",
            "version": "v1",
            "resource": "pods",
            "namespace": "test",
        },
    }
    assert dict(calls[0]["fields"])["limit"] == 1
    assert dict(calls[1]["fields"])["resourceVersion"] == "opaque-list-rv"
    assert created[0].closed and created[0].budget.used_bytes == 0


def test_connection_watch_forbidden_never_claims_verified_and_closes(config, monkeypatch):
    created = []
    responses = [
        Response(
            b'{"apiVersion":"v1","kind":"PodList","metadata":{"resourceVersion":"rv"},"items":[]}'
        ),
        Response(b"private-error", status=403),
    ]

    def factory(*args):
        transport = kube.KubernetesTransport(*args)
        created.append(transport)
        monkeypatch.setattr(
            transport.api.rest_client.pool_manager, "request", lambda *a, **kw: responses.pop(0)
        )
        return transport

    with pytest.raises(SourceFailure, match="source_permission_denied"):
        kube.KubernetesSourceRunner(factory).test_connection(config, {})
    assert created[0].closed


class OwnerTransactions:
    def __init__(self):
        self.active = False
        self.commits = 0

    @contextmanager
    def begin(self):
        assert not self.active
        self.active = True
        try:
            yield self
            self.commits += 1
        finally:
            self.active = False


def owner_context(config):
    ctx = context(config)
    ctx.owner_id = "owner"
    ctx.generation = 1
    ctx.activation_id = 1
    ctx.credential_fields = {}
    ctx.session_factory = OwnerTransactions()
    ctx.buffer = ApplicationBufferBudget().scope()
    ctx.stop_event = threading.Event()
    ctx.connected = lambda: None
    return ctx


def set_sync(monkeypatch, name, function):
    monkeypatch.setattr(kube.sync_service, name, function, raising=False)


def test_list_items_commit_individually_before_page_advance_and_collection_rv_wins(
    config, monkeypatch
):
    ctx = owner_context(config)
    ref = kube.sync_service.SyncRef(1, 1, "epoch", 1, ctx.identity_snapshot["scope_fingerprint"])
    accepted = []
    pages = []
    finished = []

    class Transport:
        @contextmanager
        def list_page(self, token):
            assert not ctx.session_factory.active
            pages.append(token)
            yield {
                "metadata": {"resourceVersion": "opaque-collection", "continue": ""},
                "items": [obj(uid=f"uid-{i}", rv=f"individual-{i}") for i in range(16)],
            }

    def receive(session, context, current_ref, envelope, page_index, collection_rv):
        assert session.active and session.commits == len(accepted)
        assert page_index == 0 and current_ref == ref and collection_rv == "opaque-collection"
        assert envelope["source"]["position"]["resource_version"].startswith("individual-")
        assert envelope["sync"]["item_count"] == 0 and not envelope["sync"]["read_complete"]
        accepted.append(envelope)
        return kube.sync_service.SyncAcceptResult(len(accepted), False, len(accepted))

    def finish(session, context, current_ref, page_index, rv, receipt_ids, **kwargs):
        assert session.active and session.commits == 16
        finished.append((page_index, rv, receipt_ids, kwargs))

    set_sync(monkeypatch, "receive_list_item", receive)
    set_sync(monkeypatch, "finish_list_page", finish)
    kube.KubernetesSourceRunner()._list(ctx, Transport(), ref)
    assert len(accepted) == 16 and ctx.session_factory.commits == 17
    assert finished == [
        (0, "opaque-collection", list(range(1, 17)), {"expected_item_count": 16, "last_page": True})
    ]
    assert pages == [None] and ctx.buffer.used_bytes == 0


def test_partial_list_failure_never_finishes_page_or_erases_committed_members(config, monkeypatch):
    ctx = owner_context(config)
    ref = kube.sync_service.SyncRef(1, 1, "epoch", 1, ctx.identity_snapshot["scope_fingerprint"])
    accepted = []
    finished = []

    class Transport:
        @contextmanager
        def list_page(self, token):
            assert not ctx.session_factory.active
            yield {"metadata": {"resourceVersion": "snapshot"}, "items": [obj(), {"bad": True}]}

    def receive(session, context, ref, envelope, page_index, collection_rv):
        accepted.append(envelope)
        return kube.sync_service.SyncAcceptResult(1, False, 1)

    set_sync(monkeypatch, "receive_list_item", receive)
    set_sync(monkeypatch, "finish_list_page", lambda *a, **kw: finished.append(True))
    with pytest.raises(SourceFailure, match="source_object_invalid"):
        kube.KubernetesSourceRunner()._list(ctx, Transport(), ref)
    assert len(accepted) == 1 and ctx.session_factory.commits == 1
    assert not finished and ctx.buffer.used_bytes == 0


def test_watch_is_serial_and_bookmark_observes_preceding_committed_receipt(config, monkeypatch):
    ctx = owner_context(config)
    received, bookmarks = [], []

    class Transport:
        closed = False

        def watch(self, rv):
            assert rv == "collection-rv" and not ctx.session_factory.active
            try:
                yield {"type": "MODIFIED", "object": obj(rv="not-a-number")}
                assert ctx.session_factory.commits == 2
                yield {
                    "type": "BOOKMARK",
                    "object": {"metadata": {"resourceVersion": "after-item"}},
                }
            finally:
                self.closed = True

    def plan(session, context):
        return SimpleNamespace(action="watch", batch=None, safe_sequence=len(received))

    def receive(session, context, envelope):
        assert session.active
        received.append(envelope)
        return kube.sync_service.SyncAcceptResult(1, False, 1)

    def bookmark(session, context, rv, expected):
        assert session.active and session.commits == 3 and len(received) == 1
        bookmarks.append((rv, expected))
        return True

    set_sync(monkeypatch, "sync_source_plan", plan)
    set_sync(monkeypatch, "receive_watch_event", receive)
    set_sync(monkeypatch, "advance_bookmark", bookmark)
    transport = Transport()
    kube.KubernetesSourceRunner()._watch(ctx, transport, "collection-rv")
    assert bookmarks == [("after-item", 1)] and received[0]["sync"] is None
    assert received[0]["source"]["position"] == {"resource_version": "not-a-number"}
    assert transport.closed and ctx.buffer.used_bytes == 0


def test_page_snapshot_change_fails_before_receiving_new_page(config, monkeypatch):
    ctx = owner_context(config)
    ref = kube.sync_service.SyncRef(1, 1, "epoch", 1, ctx.identity_snapshot["scope_fingerprint"])
    accepted, pages = [], []

    class Transport:
        @contextmanager
        def list_page(self, token):
            pages.append(token)
            yield {
                "metadata": {
                    "resourceVersion": "first" if token is None else "changed",
                    "continue": "next" if token is None else "",
                },
                "items": [obj()],
            }

    set_sync(
        monkeypatch,
        "receive_list_item",
        lambda *a, **kw: accepted.append(True) or kube.sync_service.SyncAcceptResult(1, False, 1),
    )
    set_sync(monkeypatch, "finish_list_page", lambda *a, **kw: None)
    with pytest.raises(SourceFailure, match="source_list_snapshot_changed"):
        kube.KubernetesSourceRunner()._list(ctx, Transport(), ref)
    assert len(accepted) == 1 and pages == [None, "next"]


@pytest.fixture
def database_context(api_client, session_factory, config, monkeypatch):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    snapshot = identity.kubernetes_scope_identity(config)

    def connected():
        # The independent production maintenance tick owns ready controls.
        # Run that real short transaction here without a background Worker.
        with session_factory.begin() as session:
            tickets = kube.sync_service.select_ready_control_candidates(session)
        for ticket in tickets:
            with session_factory.begin() as session:
                kube.sync_service.materialize_ready_control(session, ticket)

    with session_factory.begin() as session:
        worker = Worker(name="kubernetes-owner-worker", status="online", capabilities=["python"])
        adapter = Adapter(
            name="kubernetes-owner", language="python", adapter_type="task", run_mode="manual"
        )
        session.add_all([worker, adapter])
        session.flush()
        revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(revision)
        session.flush()
        adapter.latest_version_id = revision.id
        adapter.runtime_worker_id = worker.id
        sub = EventSubscription(
            adapter_id=adapter.id,
            source_type="kubernetes",
            source_epoch="epoch-1",
            desired_state="enabled",
        )
        session.add(sub)
        session.flush()
        config_version = SubscriptionConfigVersion(
            subscription_id=sub.id,
            version=1,
            source_epoch=sub.source_epoch,
            config=config,
            source_identity=snapshot,
        )
        session.add(config_version)
        session.flush()
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=config_version.id,
            revision_id=revision.id,
            worker_id=worker.id,
        )
        session.add(activation)
        session.flush()
        sub.activation_id = activation.id
        lease = delivery.acquire_lease(session, sub.id, "kubernetes-owner")
        return SourceRunContext(
            sub.id,
            lease.owner_id,
            lease.generation,
            activation.id,
            1,
            sub.source_epoch,
            config,
            {},
            threading.Event(),
            session_factory,
            ApplicationBufferBudget().scope(),
            connected,
            snapshot,
        )


def test_runner_paged_list_then_watch_and_bookmark_use_real_postgresql_custody(database_context):
    ctx = database_context
    calls = []

    class Transport:
        closed = False

        @contextmanager
        def list_page(self, token):
            calls.append(("list", token))
            yield {
                "metadata": {
                    "resourceVersion": "collection-opaque",
                    "continue": "page-two" if token is None else "",
                },
                "items": [obj(uid="first" if token is None else "second", rv="item-only")],
            }

        def watch(self, rv):
            calls.append(("watch", rv))
            yield {"type": "MODIFIED", "object": obj(uid="first", rv="watch-event")}
            yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "bookmark-rv"}}}
            ctx.stop_event.set()

        def close(self):
            self.closed = True

    transport = Transport()
    kube.KubernetesSourceRunner(lambda *args: transport).run(ctx)
    with ctx.session_factory() as session:
        receipts = list(session.scalars(select(EventReceipt).order_by(EventReceipt.sequence)))
        cp = session.scalar(select(SourceCheckpoint))
        batch = session.scalar(select(SyncBatch))
        assert len(receipts) == 4 and [r.source_phase for r in receipts] == [
            "sync.started",
            "list",
            "list",
            "watch",
        ]
        assert receipts[1].envelope["source"]["position"] == {"resource_version": "item-only"}
        assert cp.position == {"resource_version": "bookmark-rv"} and cp.next_sequence == 5
        assert batch.read_complete and batch.resource_version == "collection-opaque"
        assert batch.item_count == 2 and batch.page_count == 2
        assert not batch.completion_committed
    assert calls == [("list", None), ("list", "page-two"), ("watch", "collection-opaque")]
    assert transport.closed and ctx.buffer.used_bytes == 0


def test_runner_partial_page_failure_keeps_real_custody_and_marks_gap(database_context):
    ctx = database_context

    class Transport:
        closed = False

        @contextmanager
        def list_page(self, token):
            yield {
                "metadata": {"resourceVersion": "collection-rv"},
                "items": [obj(), {"invalid": True}],
            }

        def watch(self, rv):
            pytest.fail("failed LIST may not WATCH from an incomplete collection")

        def close(self):
            self.closed = True

    transport = Transport()
    with pytest.raises(SourceFailure, match="source_object_invalid"):
        kube.KubernetesSourceRunner(lambda *args: transport).run(ctx)
    with ctx.session_factory() as session:
        receipts = list(session.scalars(select(EventReceipt)))
        cp = session.scalar(select(SourceCheckpoint))
        batch = session.scalar(select(SyncBatch))
        assert len(receipts) == 2
        assert {r.source_phase: r.state for r in receipts} == {
            "sync.started": "superseded",
            "list": "pending",
        }
        assert cp.position == {} and cp.next_sequence == 3
        assert batch.failure_code == "source_object_invalid" and batch.ready_kind == "sync.failed"
        assert not batch.read_complete and not batch.completion_committed
    assert transport.closed and ctx.buffer.used_bytes == 0
