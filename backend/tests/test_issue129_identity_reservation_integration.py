"""The opt-in identity endpoint and legacy Worker boundary on PostgreSQL."""

import hashlib
import io
import ipaddress
import json
import os
import secrets
import socket
import struct
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from dlr.common.attempt_lease_proof_wire_v4 import verify_attempt_proof_signature
from dlr.common.config import settings
from dlr.common.lease_proof_wire_v4 import verify_worker_proof_signature
from dlr.control import db
from dlr.control.app import create_app
from dlr.control.control_identity_init_v4 import initialize
from dlr.control.models import Execution, ExecutionAttempt, ExecutionOutbox, Worker
from dlr.control.schemas.reliable_runtime import AttemptActionBody
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.services import worker_credentials
from dlr.control.services.attempt import V4AttemptAuthority
from dlr.control.services.control_declaration_v4 import read_declaration
from dlr.control.services.control_process_v4 import ControlProcess
from dlr.control.services.dispatch import deserialize_dispatch_message
from dlr.control.services.worker_claim_v4 import (
    ClaimProofContext,
    ClaimWireBudget,
    finalize_claim,
    prepare_claim,
)
from dlr.control.services.worker_lease_proof_v4 import public_key_bytes
from dlr.control.services.worker_token_fence_v4 import TokenFenceUnavailable
from dlr.worker import (
    attempt_session_v4,
    claim_http_v4,
    claim_preparation_v4,
    enrollment_v4,
    lease_session_v4,
    runtime_credential_v4,
)
from test_adapters import create_adapter, save_version
from test_issue127_c0 import _register_worker


@contextmanager
def _local_tls_control(app, directory, monkeypatch):
    """Serve the existing application over verified TLS for the Linux Claim path."""
    import uvicorn

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("control")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "test-tls-cert.pem", directory / "test-tls-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    remote = os.environ.get("DLR_TEST_FIXED_WORKER_E2E") == "1"
    listener.bind(("0.0.0.0" if remote else "127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_config=None,
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield f"https://{'control' if remote else '127.0.0.1'}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


@contextmanager
def _claim_task_process(lease, message, wire_budget):
    """Run the same bounded Claim receiver and creator lifecycle as the Worker."""
    parent, child = socket.socketpair()
    parent.setblocking(False)
    ticket = claim_http_v4._Spawn(child=child)
    preparer = claim_preparation_v4.ClaimPreparer()
    pidfd = None
    try:
        with lease._lock:
            deadline = lease._active_deadline()
            claims = lease._claims
            assert claims is not None and lease._proof is not None
            request = {
                "base_url": lease._base_url,
                "worker_id": lease._worker_id,
                "worker_identity_id": str(lease._identity_id),
                "instance_id": str(lease._instance_id),
                "generation": claims.generation,
                "credential": lease._credential,
                "instance_proof": lease._proof,
                "verification_key": lease._verification_key.hex(),
                "cluster_id": str(lease._cluster_id),
                "authority_epoch": str(lease._authority_epoch),
                "dispatch": message,
                "wire_budget": wire_budget,
                "timeout": 5.0,
            }
        io_deadline = min(deadline, claim_http_v4._clock() + 5.0)
        numerator, denominator = deadline.as_integer_ratio()
        deadline_ns = numerator * 1_000_000_000 // denominator
        command = [
            sys.executable,
            "-m",
            "dlr.worker.claim_task_child_v4",
            str(child.fileno()),
            str(os.getpid()),
            str(deadline_ns),
            str(512 * 1024 * 1024),
        ]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}
            or key.lower() in {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}
        }
        claim_http_v4._spawn_until(
            ticket,
            io_deadline,
            deadline_ns,
            512 * 1024 * 1024,
            _process_command=command,
            _process_env=environment,
        )
        assert ticket.process is not None
        pidfd = os.pidfd_open(ticket.process.pid, 0)
        assert claim_http_v4._receive(parent, 1, io_deadline) == b"R"
        raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
        claim_http_v4._send_all(parent, struct.pack("!I", len(raw)) + raw, io_deadline)
        size = struct.unpack("!I", claim_http_v4._receive(parent, 4, io_deadline))[0]
        assert 0 < size <= 65_536
        yield json.loads(claim_http_v4._receive(parent, size, io_deadline)), parent
    finally:
        assert preparer._stop_uncommitted(ticket, pidfd, parent)


def _exercise_remote_fixed_worker(client, session_factory, shared, base_url, trust_file, cert_file):
    """One opt-in real Broker -> fixed Worker -> result -> cleanup path."""
    import base64
    import http.server
    import urllib.request

    import pika

    from dlr.common.rabbitmq_names import worker_queue_name
    from dlr.control.models import AdapterExecutionSlot
    from dlr.control.services.dispatch import (
        DISPATCH_EXCHANGE,
        serialize_dispatch_message,
        worker_routing_key,
    )

    reservation = client.post(
        "/api/workers/v4/identities", json={"name": "remote-e2e", "request_id": str(uuid4())}
    )
    assert reservation.status_code == 201
    worker_id = reservation.json()["worker_id"]
    identity = reservation.json()["worker_identity_id"]
    # The disposable Broker account has read permission only on this queue.
    assert worker_id == 2
    enrollment = client.post(f"/api/workers/{worker_id}/credentials/enrollment", json={})
    assert enrollment.status_code == 201
    credentials = shared / "credentials"
    credentials.mkdir(mode=0o700)
    secret_file = credentials / "enrollment.secret"
    secret_file.write_text(enrollment.json()["credential_id"])
    secret_file.chmod(0o600)
    (shared / "journal").mkdir(mode=0o700)
    (shared / "trust.json").write_bytes(trust_file.read_bytes())
    (shared / "trust.json").chmod(0o600)
    (shared / "control-ca.pem").write_bytes(cert_file.read_bytes())
    (shared / "worker-id").write_text(str(worker_id))
    (shared / "worker-identity").write_text(identity)
    (shared / "control-url").write_text(base_url)
    broker = {
        "schema_version": 1,
        "worker_id": worker_id,
        "worker_identity_id": identity,
        "host": "rabbit",
        "port": 5671,
        "vhost": "/",
        "username": os.environ["DLR_TEST_BROKER_WORKER_USER"],
        "password": os.environ["DLR_TEST_BROKER_WORKER_PASSWORD"],
        "ca_file": "/shared/rabbit/ca.pem",
    }
    (credentials / "broker.json").write_text(json.dumps(broker))
    (credentials / "broker.json").chmod(0o600)
    admin_user = os.environ["DLR_TEST_BROKER_ADMIN_USER"]
    admin_password = os.environ["DLR_TEST_BROKER_ADMIN_PASSWORD"]
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            host="rabbit", credentials=pika.PlainCredentials(admin_user, admin_password)
        )
    )
    try:
        channel = connection.channel()
        channel.exchange_declare(exchange=DISPATCH_EXCHANGE, exchange_type="direct", durable=True)
        channel.queue_declare(
            queue=worker_queue_name(worker_id), durable=True, arguments={"x-queue-type": "quorum"}
        )
        channel.queue_bind(
            queue=worker_queue_name(worker_id),
            exchange=DISPATCH_EXCHANGE,
            routing_key=worker_routing_key(worker_id),
        )
        channel.confirm_delivery()
        auth = base64.b64encode(f"{admin_user}:{admin_password}".encode()).decode()
        url = f"http://rabbit:15672/api/queues/%2F/{worker_queue_name(worker_id)}"

        def broker_state():
            request = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310
                return json.load(response)

        before_ack = broker_state().get("message_stats", {}).get("ack", 0)
        (shared / "worker-start").touch()
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            with session_factory() as session:
                worker = session.get(Worker, worker_id)
                if worker.capacity_state == "ready" and worker.status == "online":
                    break
            time.sleep(0.2)
        else:
            pytest.fail("remote Worker did not complete actual local preflight and registration")
        activated = client.post(
            f"/api/workers/{worker_id}/v4/activate",
            json={"request_id": str(uuid4()), "expected_version": 1},
        )
        assert activated.status_code == 200, activated.text
        adapter = create_adapter(client, name="remote-e2e-adapter")
        changed = client.patch(
            f"/api/adapters/{adapter['id']}", json={"runtime_worker_id": worker_id}
        )
        assert changed.status_code == 200, changed.text
        save_version(
            client,
            adapter["id"],
            code="def handle(context, input):\n    return {'seen': input}\n",
        )
        created = client.post(
            f"/api/adapters/{adapter['id']}/executions", json={"input": {"value": 7}}
        )
        assert created.status_code == 202, created.text
        execution_id = created.json()["id"]
        with session_factory() as session:
            row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
            )
            message = deserialize_dispatch_message(row.payload_json)
        channel.basic_publish(
            exchange=DISPATCH_EXCHANGE,
            routing_key=worker_routing_key(worker_id),
            body=serialize_dispatch_message(message),
            mandatory=True,
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            with session_factory() as session:
                execution = session.get(Execution, execution_id)
                attempt = session.scalar(
                    select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution_id)
                )
                if (
                    execution.status == "succeeded"
                    and execution.output == {"seen": {"value": 7}}
                    and execution.workspace_cleanup_status == "completed"
                    and attempt is not None
                    and attempt.cleanup_settled_at is not None
                ):
                    slot = session.get(AdapterExecutionSlot, (adapter["id"], 0))
                    assert slot.active_attempt_id is None
                    assert execution.attempt_count == 1
                    break
            time.sleep(0.2)
        else:
            pytest.fail("remote execution/result/cleanup did not settle")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = broker_state()
            if (
                state.get("message_stats", {}).get("ack", 0) > before_ack
                and state.get("messages_ready") == 0
                and state.get("messages_unacknowledged") == 0
            ):
                break
            time.sleep(0.2)
        else:
            pytest.fail("Broker delivery remained unacknowledged")
        assert not [
            p for p in (shared / "journal" / "attempts").iterdir() if not p.name.startswith(".dlr-")
        ]
        assert not list(Path("/worker-runtime/workspaces").iterdir())

        # Confirm that revoking this instance's credential stops real work
        # and leaves the unsettled delivery and cleanup responsibility intact.
        started = threading.Event()

        class StartedHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/started":
                    started.set()
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format, *_args):
                pass

        receiver = http.server.ThreadingHTTPServer(("0.0.0.0", 0), StartedHandler)  # noqa: S104
        receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        receiver_thread.start()
        try:
            save_version(
                client,
                adapter["id"],
                code=(
                    "import time, urllib.request\n"
                    "def handle(context, input):\n"
                    f"    urllib.request.urlopen('http://control:{receiver.server_port}/started', "
                    "timeout=3).read()\n"
                    "    time.sleep(60)\n"
                    "    return {'finished': True}\n"
                ),
            )
            created = client.post(f"/api/adapters/{adapter['id']}/executions", json={})
            assert created.status_code == 202, created.text
            second_id = created.json()["id"]
            with session_factory() as session:
                row = session.scalar(
                    select(ExecutionOutbox).where(ExecutionOutbox.execution_id == second_id)
                )
                message = deserialize_dispatch_message(row.payload_json)
            acknowledged_before_fault = state.get("message_stats", {}).get("ack", 0)
            channel.basic_publish(
                exchange=DISPATCH_EXCHANGE,
                routing_key=worker_routing_key(worker_id),
                body=serialize_dispatch_message(message),
                mandatory=True,
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )
            assert started.wait(20), "business process did not start"
            revoked = client.post(f"/api/workers/{worker_id}/credentials/recover")
            assert revoked.status_code == 201
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                state = broker_state()
                if (
                    state.get("messages_ready") == 1
                    and state.get("messages_unacknowledged") == 0
                    and state.get("consumers") == 0
                ):
                    break
                time.sleep(0.2)
            else:
                pytest.fail("revoked Worker did not relinquish its unacknowledged delivery")
            assert state.get("message_stats", {}).get("ack", 0) == acknowledged_before_fault
            with session_factory() as session:
                execution = session.get(Execution, second_id)
                attempt = session.scalar(
                    select(ExecutionAttempt).where(ExecutionAttempt.execution_id == second_id)
                )
                assert execution.status == "running" and execution.output is None
                assert attempt is not None and attempt.cleanup_settled_at is None
                slot = session.get(AdapterExecutionSlot, (adapter["id"], 0))
                assert slot.active_attempt_id == attempt.id
                assert (
                    shared / "journal" / "attempts" / f"attempt-{attempt.id}.attempt.json"
                ).exists()
        finally:
            receiver.shutdown()
            receiver.server_close()
            receiver_thread.join(timeout=5)
    finally:
        connection.close()


def _assert_result_transaction_fences(factory, process, worker_id, credential, lease, attempt):
    """DB protocol only: decoding a valid intent cannot replace either fence."""
    from dlr.common.result_receipt_proof_v4 import verify_result_receipt
    from dlr.control.schemas.reliable_runtime import AttemptResultBody
    from dlr.control.services import attempt_result_finalize_v4 as finalizer
    from dlr.control.services import attempt_result_intent_v4 as intents

    assert attempt._claims is not None
    action = AttemptResultBody(
        attempt_id=attempt._claims.attempt_id,
        fencing_token=attempt._claims.fencing_token,
        claim_token=attempt._claim_token,
        status="succeeded",
        output={"protocol_only": True},
    )
    fence = finalizer.ResultControlFence(
        process._deployment, process._instance, process._token, process.check_authority
    )
    with factory() as session:
        gate, authority = finalizer._control_authority(session, fence)
        context = intents.ResultIntentContext(
            worker_id,
            action.attempt_id,
            credential,
            lease._instance_id,
            lease._claims.generation,
            lease._proof,
            attempt._proof,
            action,
            authority,
        )
        ref = intents.stage_result_intent(session, context, max_body_bytes=100000)
        gate.check()
        session.commit()
    nonce = uuid4()
    with factory() as session:
        gate, authority = finalizer._control_authority(session, fence)
        context = replace(context, authority=authority)
        proof = intents.observe_result_in_session(
            session, context, ref, max_body_bytes=100000, observation_nonce=nonce
        )
        gate.check()
        session.rollback()
    assert proof.token is not None
    # Both paths receive the valid local signature and ownership metadata.
    # An invalid Control fence must still reject mutation and observation.
    invalid = replace(fence, instance_token="invalid")
    with pytest.raises(finalizer.ResultFinalizeUnknown):
        finalizer.finalize_unconfirmed_result_from_token(
            factory,
            context,
            ref,
            proof.token,
            observation_nonce=nonce,
            control=invalid,
            max_body_bytes=100000,
        )
    with factory() as session:
        assert session.get(ExecutionAttempt, action.attempt_id).status == "running"
    with pytest.raises(finalizer.ResultFinalizeUnknown):
        finalizer.finalize_unconfirmed_result_from_token(
            factory,
            context,
            ref,
            proof.token,
            observation_nonce=nonce,
            control=fence,
            max_body_bytes=100000,
        )
    with factory() as session:
        assert session.get(ExecutionAttempt, action.attempt_id).status == "succeeded"
        assert session.get(ExecutionAttempt, action.attempt_id).cleanup_settled_at is None
    receipt_nonce = uuid4()
    with pytest.raises(finalizer.ResultReceiptUnavailable):
        finalizer.observe_committed_result_from_token(
            factory,
            context,
            ref,
            proof.token,
            observation_nonce=nonce,
            receipt_nonce=receipt_nonce,
            control=invalid,
            max_body_bytes=100000,
        )
    receipt = finalizer.observe_committed_result_from_token(
        factory,
        context,
        ref,
        proof.token,
        observation_nonce=nonce,
        receipt_nonce=receipt_nonce,
        control=fence,
        max_body_bytes=100000,
    )
    assert verify_result_receipt(
        receipt.token,
        public_key=authority.public_key,
        intent_token=proof.token,
        observation_nonce=receipt_nonce,
    )


def _assert_cleanup_protocol(client, factory, worker_id, lease, claimed, public_key):
    """Protocol-only settlement: this synthetic receipt proves no physical cleanup."""
    from dlr.common.cleanup_receipt_proof_v4 import verify_cleanup_receipt_proof
    from dlr.control.models import AdapterExecutionSlot

    payload = claimed.payload
    assert payload is not None
    base = f"/api/workers/{worker_id}/v4/executions/{payload.execution_id}/cleanup"
    intent, nonce, receipt_nonce = uuid4(), uuid4(), uuid4()
    headers = {
        "Authorization": f"Bearer {lease._credential}",
        "X-DLR-Worker-Instance": str(lease._instance_id),
        "X-DLR-Worker-Generation": str(lease._claims.generation),
        "X-DLR-Worker-Lease-Proof": lease._proof,
        "X-DLR-Cleanup-Token": payload.cleanup_token,
        "X-DLR-Cleanup-Intent": str(intent),
        "X-DLR-Cleanup-Observation-Nonce": str(nonce),
    }
    body = {
        "status": "completed",
        "attempt_id": payload.attempt_id,
        "fencing_token": payload.fencing_token,
        "worker_identity_id": str(lease._claims.worker_identity_id),
        "original_instance_id": str(lease._instance_id),
        "original_instance_generation": lease._claims.generation,
    }
    assert (
        client.post(
            base + "/stage", headers={**headers, "X-DLR-Cleanup-Token": "invalid"}, json=body
        ).status_code
        == 503
    )
    staged = client.post(base + "/stage", headers=headers, json=body)
    assert staged.status_code == 200, staged.text
    proof = staged.json()["cleanup_proof"]
    final_headers = {
        **headers,
        "X-DLR-Cleanup-Proof": proof,
        "X-DLR-Cleanup-Receipt-Nonce": str(receipt_nonce),
    }
    assert (
        client.post(
            base + "/finalize",
            headers={**final_headers, "X-DLR-Cleanup-Proof": "invalid"},
            json=body,
        ).status_code
        == 503
    )
    with factory() as session:
        assert session.get(ExecutionAttempt, payload.attempt_id).cleanup_settled_at is None
        assert (
            session.get(AdapterExecutionSlot, (payload.adapter_id, 0)).active_attempt_id
            == payload.attempt_id
        )
    for _ in range(2):
        result = client.post(base + "/finalize", headers=final_headers, json=body)
        assert result.status_code == 200, result.text
        assert (
            verify_cleanup_receipt_proof(
                result.json()["cleanup_receipt"],
                public_key=public_key,
                instance_token=lease._proof,
                intent_token=proof,
                observation_nonce=receipt_nonce,
            )
            is not None
        )
        with factory() as session:
            attempt = session.get(ExecutionAttempt, payload.attempt_id)
            assert attempt.cleanup_settled_at is not None
            assert (
                session.get(AdapterExecutionSlot, (payload.adapter_id, 0)).active_attempt_id is None
            )
            assert session.get(Execution, payload.execution_id).status == "succeeded"
            settled = attempt.cleanup_settled_at
        if _ == 0:
            first_settlement = settled
        else:
            assert settled == first_settlement


def test_identity_reservation_is_disabled_by_default(api_client):
    assert api_client.post("/api/workers/v4/identities", json={}).status_code == 404


def test_shared_tokens_cannot_use_the_v4_worker_credential_namespace(api_client, monkeypatch):
    credential = "dlrw4_" + "a" * 64
    monkeypatch.setattr(settings, "admin_token", credential)
    assert (
        api_client.get(
            "/api/workers", headers={"Authorization": f"Bearer {credential}"}
        ).status_code
        == 503
    )
    monkeypatch.setattr(settings, "worker_token", credential)
    assert (
        api_client.post(
            "/api/workers/register",
            json={"name": "reserved-token", "protocol_version": 3},
            headers={"Authorization": f"Bearer {credential}"},
        ).status_code
        == 503
    )


def test_remote_mode_startup_refuses_legacy_identity(api_client, session_factory, monkeypatch):
    from dlr.control.services.worker_remote_deployment_v4 import (
        RemoteDeploymentUnavailable,
        require_dedicated_deployment,
    )

    # An empty dedicated database may start, including subsequent restarts
    # once only administrator-reserved stage identities exist.
    with session_factory() as session:
        require_dedicated_deployment(session)
        session.rollback()
    monkeypatch.setattr(settings, "worker_identity_reservation_enabled", True)
    app = create_app()
    app.dependency_overrides.update(api_client.app.dependency_overrides)
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-token"})
    reserved = client.post(
        "/api/workers/v4/identities",
        json={"name": "stage-provenance", "request_id": str(uuid4())},
    )
    assert reserved.status_code == 201
    with session_factory() as session:
        require_dedicated_deployment(session)
        session.rollback()

    _register_worker(api_client, "legacy-before-remote")
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    monkeypatch.setattr(db, "SessionLocal", session_factory)
    with (
        pytest.raises(RemoteDeploymentUnavailable, match="worker_remote_deployment_incompatible"),
        TestClient(create_app()),
    ):
        pytest.fail("legacy deployment must not start remote mode")
    # Refusal leaves the original identity usable when the switch is off.
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", False)
    _register_worker(api_client, "legacy-before-remote")


def test_remote_v4_mode_closes_the_shared_worker_api(api_client, monkeypatch):
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    response = api_client.post(
        "/api/workers/register",
        json={"name": "legacy-disabled", "protocol_version": 3},
        headers={"Authorization": "Bearer test-worker-token"},
    )
    assert response.status_code == 401
    assert api_client.get("/api/workers").status_code == 200


def test_remote_v4_mode_does_not_queue_to_legacy_worker(api_client, monkeypatch):
    _register_worker(api_client, "legacy-target")
    adapter = create_adapter(api_client, name="legacy-target-adapter")
    save_version(api_client, adapter["id"])
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runtime_worker_invalid"


def test_reserved_identity_cannot_be_taken_over_by_legacy_worker(
    api_client, session_factory, monkeypatch
):
    monkeypatch.setattr(settings, "worker_identity_reservation_enabled", True)
    app = create_app()
    app.dependency_overrides.update(api_client.app.dependency_overrides)
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-token"})
    payload = {"name": f"reserved-{uuid4()}", "request_id": str(uuid4())}

    denied = client.post(
        "/api/workers/v4/identities",
        json=payload,
        headers={"Authorization": "Bearer test-worker-token"},
    )
    assert denied.status_code == 401
    first = client.post("/api/workers/v4/identities", json=payload)
    again = client.post("/api/workers/v4/identities", json=payload)
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    worker_id = first.json()["worker_id"]

    legacy = {"Authorization": "Bearer test-worker-token"}
    registration = client.post(
        "/api/workers/register",
        json={"name": payload["name"], "protocol_version": 3},
        headers=legacy,
    )
    assert registration.status_code == 409
    assert client.post(f"/api/workers/{worker_id}/heartbeat", headers=legacy).status_code == 401
    with session_factory() as session:
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text("UPDATE workers SET protocol_version = 3 WHERE id = :worker_id"),
                {"worker_id": worker_id},
            )
        worker = session.get(Worker, worker_id)
        assert worker.identity_lifecycle == "reserved" and worker.protocol_version == 4
        assert worker.status == "offline" and worker.scheduling_state == "disabled"


def test_enrollment_requires_isolated_database_role_and_exchanges_once(
    api_client, test_engine, session_factory, tmp_path, monkeypatch
):
    """Exercise the actual API against a restricted PostgreSQL login."""
    monkeypatch.setattr(settings, "worker_identity_reservation_enabled", True)
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    runtime_role = "dlr_w1_" + uuid4().hex[:12]
    old_role = "dlr_old_" + uuid4().hex[:12]
    database = test_engine.url.database
    assert database and database.isidentifier()
    monkeypatch.setattr(settings, "worker_v4_database_role", runtime_role)
    monkeypatch.setattr(settings, "worker_v4_database_admin_roles", ["postgres"])
    signing_key = secrets.token_bytes(32)
    key_file = tmp_path / "proof-key"
    key_file.write_text(signing_key.hex())
    key_file.chmod(0o600)
    monkeypatch.setattr(settings, "worker_proof_signing_key_file", str(key_file))
    declaration_file = tmp_path / "control.json"
    declaration_file.write_text(
        json.dumps(
            {
                "cluster_id": str(uuid4()),
                "authority_epoch": str(uuid4()),
                "image_digest": "sha256:" + "1" * 64,
                "managed_fingerprint": "2" * 64,
                "builtin_fingerprint": "3" * 64,
                "attachment_fingerprint": "4" * 64,
                "capacity_fingerprint": "5" * 64,
                "worker_proof_public_key": public_key_bytes(signing_key).hex(),
            }
        )
    )
    declaration_file.chmod(0o600)
    monkeypatch.setattr(settings, "control_identity_file", str(declaration_file))
    deployment = read_declaration(settings)
    with session_factory() as session:
        initialize(session, deployment)
        session.commit()

    app = create_app()
    app.dependency_overrides.update(api_client.app.dependency_overrides)
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-token"})
    reserved = client.post(
        "/api/workers/v4/identities", json={"name": "remote-one", "request_id": str(uuid4())}
    )
    assert reserved.status_code == 201
    worker_id = reserved.json()["worker_id"]
    endpoint = f"/api/workers/{worker_id}/credentials/enrollment"
    assert client.post(endpoint, json={}).status_code == 503

    runtime_engine = None
    old_engine = None
    process = None
    tls_stack = ExitStack()
    roles_created = False
    try:
        with test_engine.begin() as conn:
            conn.execute(text(f'CREATE ROLE "{runtime_role}" LOGIN'))
            conn.execute(text(f'CREATE ROLE "{old_role}" LOGIN'))
            conn.execute(text(f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC'))
            conn.execute(text(f'REVOKE TEMPORARY ON DATABASE "{database}" FROM PUBLIC'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO "{runtime_role}"'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO "{old_role}"'))
            conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{runtime_role}"'))
            conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    f'TO "{runtime_role}"'
                )
            )
            for table in (
                "worker_lease_candidates_v4",
                "attempt_lease_candidates_v4",
                "attempt_result_intents_v4",
                "worker_cleanup_intents_v4",
            ):
                conn.execute(text(f'REVOKE DELETE ON "{table}" FROM "{runtime_role}"'))
            conn.execute(
                text(f'REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM "{runtime_role}"')
            )
            conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{runtime_role}"'))
        roles_created = True
        test_engine.dispose()  # The admin pool must not retain old database sessions.
        runtime_engine = create_engine(test_engine.url.set(username=runtime_role), pool_size=4)
        runtime_sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
        if os.environ.get("DLR_TEST_FIXED_WORKER_E2E") == "1":
            # Child processes use the same restricted runtime role as Control.
            monkeypatch.setattr(
                settings,
                "database_url",
                test_engine.url.set(username=runtime_role).render_as_string(hide_password=False),
            )
            monkeypatch.setattr(db, "SessionLocal", runtime_sessions)

        def runtime_session():
            with runtime_sessions() as session:
                yield session

        app.dependency_overrides[db.get_session] = runtime_session
        process = ControlProcess(settings, runtime_sessions, deployment)
        process.start()
        app.state.control_process_v4 = process
        deadline = time.monotonic() + 4
        while process._state != "registered" and time.monotonic() < deadline:
            time.sleep(0.02)
        process.check_authority()

        # A role capable of opening old connections blocks issuance even when
        # no such connection is active.
        assert client.post(endpoint, json={}).status_code == 503
        old_engine = create_engine(test_engine.url.set(username=old_role))
        with old_engine.connect() as old_connection:
            old_connection.execute(text("SELECT 1"))
            with test_engine.begin() as conn:
                old_oid = conn.scalar(
                    text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:name"),
                    {"name": old_role},
                )
                conn.execute(text(f'REVOKE CONNECT ON DATABASE "{database}" FROM "{old_role}"'))
                conn.execute(text(f'DROP ROLE "{old_role}"'))
            test_engine.dispose()
            with runtime_engine.connect() as observer:
                old_session = observer.execute(
                    text(
                        "SELECT usesysid, usename FROM pg_catalog.pg_stat_activity "
                        "WHERE usesysid=:oid"
                    ),
                    {"oid": old_oid},
                ).one()
                assert old_session.usesysid == old_oid and old_session.usename is None
            assert client.post(endpoint, json={}).status_code == 503
        old_engine.dispose()
        # Revoking CONNECT does not terminate an existing old connection.
        with test_engine.connect() as old_connection:
            old_connection.execute(text("SELECT 1"))
            with runtime_engine.connect() as observer:
                visible = observer.execute(
                    text(
                        "SELECT usename, datname, backend_type FROM pg_catalog.pg_stat_activity "
                        "WHERE datname = current_database() AND usename = 'postgres'"
                    )
                ).all()
                assert visible
            assert client.post(endpoint, json={}).status_code == 503
        test_engine.dispose()

        enrolled = client.post(endpoint, json={})
        assert enrolled.status_code == 201
        assert enrolled.headers["cache-control"] == "no-store"
        secret = enrolled.json()["credential_id"]
        assert secret.startswith("dlrw4_")
        exchange_endpoint = f"/api/workers/{worker_id}/credentials/exchange"
        credential_dir = tmp_path / "worker-credential"
        credential_dir.mkdir(mode=0o700)
        enrollment_file = credential_dir / "enrollment.secret"
        enrollment_file.write_text(secret)
        enrollment_file.chmod(0o600)

        class ExchangeResponse(io.BytesIO):
            def __init__(self, result):
                super().__init__(result.content)
                self.status = result.status_code

        def send_via_api(request, *, timeout):
            assert timeout > 0
            result = client.post(
                urlsplit(request.full_url).path,
                headers=dict(request.header_items()),
                content=request.data,
            )
            return ExchangeResponse(result)

        monkeypatch.setattr(enrollment_v4, "_open_v4", send_via_api)
        identity_id = UUID(reserved.json()["worker_identity_id"])
        enrollment_v4.enroll(
            base_url="https://control.example",
            worker_id=worker_id,
            worker_identity_id=identity_id,
            credential_directory=credential_dir,
        )
        credential = runtime_credential_v4.load_runtime_credential(
            credential_dir, worker_id=worker_id, worker_identity_id=identity_id
        )
        assert credential.secret != secret
        assert (
            client.post(
                exchange_endpoint, headers={"Authorization": f"Bearer {secret}"}
            ).status_code
            == 401
        )

        instance = uuid4()
        runtime_secret = credential.secret
        declaration = json.loads(declaration_file.read_text())
        trust_file = tmp_path / "worker-proof-trust.json"
        trust_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cluster_id": declaration["cluster_id"],
                    "authority_epoch": declaration["authority_epoch"],
                    "worker_proof_public_key": declaration["worker_proof_public_key"],
                }
            )
        )
        trust_file.chmod(0o600)
        monkeypatch.setattr(lease_session_v4, "_open_v4", send_via_api)
        base_url = "https://control.example"
        if os.environ.get("DLR_TEST_CLAIM_PROCESS") == "1":
            base_url = tls_stack.enter_context(_local_tls_control(app, tmp_path, monkeypatch))
        lease = lease_session_v4.WorkerLeaseSession.from_trusted_file(
            trust_file=trust_file,
            base_url=base_url,
            worker_id=worker_id,
            worker_identity_id=identity_id,
            credential=runtime_secret,
            credential_record_id=credential.credential_record_id,
            instance_id=instance,
        )
        worker_headers = {
            "Authorization": f"Bearer {runtime_secret}",
            "X-DLR-Worker-Instance": str(instance),
        }
        # A runtime role that can add candidate triggers must not sign leases.
        with test_engine.begin() as conn:
            conn.execute(text(f'GRANT TRIGGER ON worker_lease_candidates_v4 TO "{runtime_role}"'))
        test_engine.dispose()
        with pytest.raises(lease_session_v4.WorkerLeaseError):
            lease.register()
        with test_engine.begin() as conn:
            conn.execute(
                text(f'REVOKE TRIGGER ON worker_lease_candidates_v4 FROM "{runtime_role}"')
            )
        test_engine.dispose()
        lease.register()
        original_proof = lease._proof
        claims = verify_worker_proof_signature(original_proof, public_key_bytes(signing_key))
        assert claims is not None and claims.revision == 1
        assert claims.instance_id == instance and claims.worker_id == worker_id
        assert str(claims.worker_identity_id) == reserved.json()["worker_identity_id"]
        heartbeat_headers = {
            **worker_headers,
            "X-DLR-Worker-Generation": str(claims.generation),
            "X-DLR-Worker-Lease-Proof": original_proof,
        }
        heartbeat_endpoint = f"/api/workers/{worker_id}/v4/proof-heartbeat"
        lease.heartbeat()
        renewed_proof = lease._proof
        renewed = verify_worker_proof_signature(renewed_proof, public_key_bytes(signing_key))
        assert renewed is not None and renewed.revision == 2
        assert renewed.guard_us == claims.nominal_us
        assert client.post(heartbeat_endpoint, headers=heartbeat_headers).status_code == 409
        current_headers = {
            **heartbeat_headers,
            "X-DLR-Worker-Lease-Proof": renewed_proof,
        }
        # Readiness is test setup only; this does not assert local preflight.
        with runtime_sessions.begin() as session:
            worker = session.get(Worker, worker_id)
            worker.scheduling_state = "disabled"
            worker.capacity_state = "ready"
            worker.capabilities = ["python"]
            worker.isolation_preflight_status = "passed"
            worker.isolation_capabilities = {key: True for key in REQUIRED_ISOLATION_CAPABILITIES}
        activation_body = {"request_id": str(uuid4()), "expected_version": 1}
        with runtime_sessions() as concurrent_operation:
            ordinary_fence = process.worker_fence(concurrent_operation)
            activated = client.post(f"/api/workers/{worker_id}/v4/activate", json=activation_body)
            assert activated.status_code == 200, activated.text
            assert client.post(f"/api/workers/{worker_id}/credentials/recover").status_code == 503
            with pytest.raises(TokenFenceUnavailable):
                ordinary_fence.check_candidate("dlrw4_" + "e" * 64)
        with runtime_sessions() as exclusive_issuance:
            process.worker_fence(exclusive_issuance, issue_credentials=True)
            assert client.post(heartbeat_endpoint, headers=current_headers).status_code == 503
        assert (
            client.post(f"/api/workers/{worker_id}/v4/activate", json=activation_body).status_code
            == 200
        )
        empty_drain = {"request_id": str(uuid4()), "expected_version": 2}
        drained = client.post(f"/api/workers/{worker_id}/v4/drain", json=empty_drain)
        assert drained.status_code == 200 and drained.json()["scheduling_state"] == "disabled"
        assert drained.json()["maintenance_version"] == 3
        assert (
            client.post(f"/api/workers/{worker_id}/v4/drain", json=empty_drain).json()
            == drained.json()
        )
        assert (
            client.post(
                f"/api/workers/{worker_id}/v4/activate",
                json={"request_id": str(uuid4()), "expected_version": 2},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/workers/{worker_id}/v4/activate",
                json={"request_id": str(uuid4()), "expected_version": 3},
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/workers/{worker_id}/v4/status",
                headers={"Authorization": "Bearer test-worker-token"},
            ).status_code
            == 401
        )
        adapter = create_adapter(client, name="remote-claim")
        save_version(
            client,
            adapter["id"],
            code="def handle(context, input):\n    return input\n#" + "x" * 5000,
        )
        created = client.post(f"/api/adapters/{adapter['id']}/executions", json={})
        assert created.status_code == 202, created.text
        execution_id = created.json()["id"]
        with runtime_sessions() as session:
            row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
            )
            message = dict(row.payload_json)
        dispatch = deserialize_dispatch_message(message)
        worker_authority = V4AttemptAuthority(runtime_secret, instance, renewed.generation)

        def check_claim_authority() -> None:
            fence.check()
            process.check_authority()

        with runtime_sessions() as session:
            fence = process.worker_fence(session)
            authority = process.worker_proof_authority(session)
            waiting, proposal = prepare_claim(
                session,
                worker_id,
                dispatch,
                worker_authority,
                wire_budget=ClaimWireBudget(4096),
                proof_context=ClaimProofContext(authority, renewed_proof),
                check_authority=check_claim_authority,
            )
            assert waiting.decision == "PAUSE_CONSUMER"
            assert waiting.reason == "claim_response_budget_exceeded"
            assert proposal is None
        with runtime_sessions() as session:
            assert session.get(Execution, execution_id).attempt_count == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ExecutionAttempt)
                    .where(ExecutionAttempt.execution_id == execution_id)
                )
                == 0
            )
        # Prepare under real locks, then roll back before the Worker could
        # acknowledge its bounded parse. The final transaction must recreate
        # the same bytes and commit only that matching tentative Attempt.
        with runtime_sessions() as session:
            fence = process.worker_fence(session)
            authority = process.worker_proof_authority(session)
            tentative, proposal = prepare_claim(
                session,
                worker_id,
                dispatch,
                worker_authority,
                wire_budget=ClaimWireBudget(100000),
                proof_context=ClaimProofContext(authority, renewed_proof),
                check_authority=check_claim_authority,
            )
            assert tentative.decision == "EXECUTE"
            assert proposal is not None
        with runtime_sessions() as session:
            assert session.get(Execution, execution_id).attempt_count == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ExecutionAttempt)
                    .where(ExecutionAttempt.execution_id == execution_id)
                )
                == 0
            )
        # A real newer Adapter revision changes the prepared metadata. The
        # stale proposal must not create an Attempt, even though its original
        # immutable revision is still available.
        save_version(client, adapter["id"], code="def handle(context, input):\n    return input\n")
        with runtime_sessions() as session:
            fence = process.worker_fence(session)
            authority = process.worker_proof_authority(session)
            changed = finalize_claim(
                session,
                worker_id,
                dispatch,
                worker_authority,
                wire_budget=ClaimWireBudget(100000),
                proof_context=ClaimProofContext(authority, renewed_proof),
                proposal=proposal,
            )
            assert changed.decision == "PAUSE_CONSUMER"
            assert changed.reason == "claim_snapshot_changed"
        with runtime_sessions() as session:
            assert session.get(Execution, execution_id).attempt_count == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ExecutionAttempt)
                    .where(ExecutionAttempt.execution_id == execution_id)
                )
                == 0
            )
        with runtime_sessions() as session:
            fence = process.worker_fence(session)
            authority = process.worker_proof_authority(session)
            tentative, proposal = prepare_claim(
                session,
                worker_id,
                dispatch,
                worker_authority,
                wire_budget=ClaimWireBudget(100000),
                proof_context=ClaimProofContext(authority, renewed_proof),
                check_authority=check_claim_authority,
            )
            assert tentative.decision == "EXECUTE" and proposal is not None
        if os.environ.get("DLR_TEST_CLAIM_PROCESS") == "1":
            with _claim_task_process(lease, message, 4096) as (waiting, _channel):
                assert waiting == {
                    "state": "decision",
                    "decision": "PAUSE_CONSUMER",
                    "reason": "claim_response_budget_exceeded",
                    "retry_after_seconds": 30,
                }
            with runtime_sessions() as session:
                assert session.get(Execution, execution_id).attempt_count == 0
            with _claim_task_process(lease, message, 100000) as (prepared, channel):
                assert prepared["state"] == "prepared"
                with runtime_sessions() as session:
                    assert session.get(Execution, execution_id).attempt_count == 0
                action = AttemptActionBody(
                    attempt_id=prepared["attempt_id"],
                    fencing_token=prepared["fencing_token"],
                    claim_token=prepared["claim_token"],
                )
                claimed = SimpleNamespace(attempt_id=action.attempt_id)
                attempt_lease = attempt_session_v4.AttemptLeaseSession.finalize_prepared(
                    lease,
                    proposal=prepared["proposal"],
                    attempt_id=action.attempt_id,
                    fencing_token=action.fencing_token,
                    claim_token=action.claim_token,
                )
                signed_attempt = attempt_lease._proof
                assert signed_attempt is not None
                attempt_lease.start()
                workdir = (
                    tmp_path
                    / "workspaces"
                    / f"attempt-{action.attempt_id}"
                    / f"dlr-exec-{execution_id}"
                )
                workdir.mkdir(mode=0o700, parents=True)
                directory = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(directory)
                    stage = {
                        "op": "stage",
                        "workspace": str(workdir),
                        "workspace_device": info.st_dev,
                        "workspace_inode": info.st_ino,
                        "staging_bytes": prepared["resource_profile"]["tmp_bytes"],
                        "launch_bytes": 65536,
                        "fingerprint": prepared["task_fingerprint"],
                    }
                    raw = json.dumps(stage, separators=(",", ":")).encode("utf-8")
                    deadline = attempt_lease.local_deadline()
                    claim_http_v4._send_all(channel, struct.pack("!I", len(raw)) + raw, deadline)
                    claim_http_v4._send_fd(channel, directory, deadline)
                    size = struct.unpack("!I", claim_http_v4._receive(channel, 4, deadline))[0]
                    answer = json.loads(claim_http_v4._receive(channel, size, deadline))
                    assert answer == {
                        "state": "staged",
                        "fingerprint": prepared["task_fingerprint"],
                        "launch_bytes": 65536,
                    }
                    launch_fd = claim_preparation_v4.ClaimPreparer._receive_sealed_launch(
                        channel, deadline, 65536
                    )
                    os.close(launch_fd)
                    assert (workdir / "adapter.py").read_text().endswith("x" * 5000)
                    assert json.loads((workdir / "input_manifest.json").read_text()) == {
                        "execution_id": execution_id,
                        "files": [],
                    }
                finally:
                    os.close(directory)
        else:
            with runtime_sessions() as session:
                fence = process.worker_fence(session)
                authority = process.worker_proof_authority(session)
                claimed = finalize_claim(
                    session,
                    worker_id,
                    dispatch,
                    worker_authority,
                    wire_budget=ClaimWireBudget(100000),
                    proof_context=ClaimProofContext(authority, renewed_proof),
                    proposal=proposal,
                )
                assert claimed.decision == "EXECUTE" and claimed._wire_bytes is not None
                assert hashlib.sha256(claimed._wire_bytes).digest() == proposal.wire_digest
                fence.check()
                process.check_authority()
                session.commit()
            assert claimed._attempt_candidate is not None and claimed.payload is not None
            action = AttemptActionBody(
                attempt_id=claimed.payload.attempt_id,
                fencing_token=claimed.payload.fencing_token,
                claim_token=claimed.payload.claim_token,
            )
            proof_started = lease._now()
            signed_attempt = process.observe_attempt_candidate(
                claimed._attempt_candidate, runtime_secret, renewed_proof, action
            )
            proof_finished = lease._now()
            attempt_lease = attempt_session_v4.AttemptLeaseSession(
                lease,
                attempt_id=action.attempt_id,
                fencing_token=action.fencing_token,
                claim_token=action.claim_token,
                initial_proof=signed_attempt,
                request_started=proof_started,
                request_finished=proof_finished,
            )
        attempt_proof = verify_attempt_proof_signature(
            signed_attempt, public_key_bytes(signing_key)
        )
        assert attempt_proof is not None and attempt_proof.worker_id == worker_id
        assert attempt_proof.attempt_id == claimed.attempt_id
        attempt_headers = {
            **current_headers,
            "X-DLR-Worker-Attempt-Proof": signed_attempt,
        }
        action_json = action.model_dump(mode="json")
        start_endpoint = f"/api/workers/{worker_id}/v4/attempts/{claimed.attempt_id}/start"
        assert (
            client.post(
                start_endpoint,
                headers={**attempt_headers, "X-DLR-Worker-Attempt-Proof": "invalid"},
                json=action_json,
            ).status_code
            == 503
        )
        if os.environ.get("DLR_TEST_CLAIM_PROCESS") != "1":
            attempt_lease.start()
        attempt_lease.renew()
        assert attempt_lease.is_usable()
        successor = verify_attempt_proof_signature(
            attempt_lease._proof, public_key_bytes(signing_key)
        )
        assert successor is not None and successor.revision == attempt_proof.revision + 1
        assert (
            client.post(start_endpoint, headers=attempt_headers, json=action_json).status_code
            == 503
        )
        with runtime_sessions() as session:
            fence = process.worker_fence(session)
            authority = process.worker_proof_authority(session)
            repeated, proposal = prepare_claim(
                session,
                worker_id,
                dispatch,
                worker_authority,
                wire_budget=ClaimWireBudget(100000),
                proof_context=ClaimProofContext(authority, renewed_proof),
                check_authority=check_claim_authority,
            )
            assert repeated.decision == "ACK_NOOP"
            assert proposal is None
        with runtime_sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ExecutionAttempt)
                    .where(ExecutionAttempt.execution_id == execution_id)
                )
                == 1
            )
        _assert_result_transaction_fences(
            runtime_sessions, process, worker_id, runtime_secret, lease, attempt_lease
        )
        attempt_lease.close()
        active_drain = {"request_id": str(uuid4()), "expected_version": 4}
        draining = client.post(f"/api/workers/{worker_id}/v4/drain", json=active_drain)
        assert draining.status_code == 200
        assert draining.json()["scheduling_state"] == "draining"
        assert draining.json()["unsettled_attempts"] == 1
        assert (
            client.post(f"/api/workers/{worker_id}/v4/drain", json=active_drain).json()
            == draining.json()
        )
        observed = client.get(f"/api/workers/{worker_id}/v4/status")
        assert observed.status_code == 200
        assert observed.json()["blocked_reason"] == "worker_cleanup_unsettled"
        with runtime_sessions() as session:
            fence = process.worker_fence(session)
            authority = process.worker_proof_authority(session)
            paused, proposal = prepare_claim(
                session,
                worker_id,
                dispatch,
                worker_authority,
                wire_budget=ClaimWireBudget(100000),
                proof_context=ClaimProofContext(authority, renewed_proof),
                check_authority=check_claim_authority,
            )
            assert paused.decision == "PAUSE_CONSUMER" and paused.reason == "worker_not_ready"
            assert proposal is None
        if os.environ.get("DLR_TEST_CLAIM_PROCESS") != "1":
            # The production cleanup endpoint uses its own transaction factory,
            # rather than the request-session dependency overridden above.
            with monkeypatch.context() as cleanup_scope:
                cleanup_scope.setattr(db, "SessionLocal", runtime_sessions)
                _assert_cleanup_protocol(
                    client,
                    runtime_sessions,
                    worker_id,
                    lease,
                    claimed,
                    public_key_bytes(signing_key),
                )
            finished_drain = client.post(f"/api/workers/{worker_id}/v4/drain", json=active_drain)
            assert finished_drain.json()["scheduling_state"] == "disabled"
            assert finished_drain.json()["unsettled_attempts"] == 0
        lease.retire()
        assert client.post(heartbeat_endpoint, headers=current_headers).status_code == 409
        if os.environ.get("DLR_TEST_FIXED_WORKER_E2E") == "1":
            _exercise_remote_fixed_worker(
                client,
                runtime_sessions,
                Path(os.environ["DLR_TEST_SHARED_DIR"]),
                base_url,
                trust_file,
                tmp_path / "test-tls-cert.pem",
            )
        recovered = client.post(f"/api/workers/{worker_id}/credentials/recover")
        assert recovered.status_code == 201
        assert recovered.headers["cache-control"] == "no-store"
        recovered_secret = recovered.json()["credential_id"]
        staged = credential_dir / "runtime-credential.recovery.json"
        staged.write_bytes(recovered.content)
        staged.chmod(0o600)
        runtime_credential_v4.install_recovered_credential(
            credential_dir, worker_id=worker_id, worker_identity_id=identity_id
        )
        installed = runtime_credential_v4.load_runtime_credential(
            credential_dir, worker_id=worker_id, worker_identity_id=identity_id
        )
        assert installed.secret == recovered_secret
        with runtime_sessions() as session:
            with pytest.raises(worker_credentials.WorkerCredentialError):
                worker_credentials.authenticate_runtime_credential(
                    session, worker_id, runtime_secret
                )
            session.rollback()
            assert (
                worker_credentials.authenticate_runtime_credential(
                    session, worker_id, recovered_secret
                ).id
                == worker_id
            )
    finally:
        tls_stack.close()
        if process is not None:
            process.stop()
        if runtime_engine is not None:
            runtime_engine.dispose()
        if old_engine is not None:
            old_engine.dispose()
        if roles_created:
            with test_engine.begin() as conn:
                conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO PUBLIC'))
                conn.execute(text(f'GRANT TEMPORARY ON DATABASE "{database}" TO PUBLIC'))
                conn.execute(text(f'DROP OWNED BY "{runtime_role}"'))
                conn.execute(text(f'DROP ROLE IF EXISTS "{runtime_role}"'))
                conn.execute(text(f'DROP ROLE IF EXISTS "{old_role}"'))
