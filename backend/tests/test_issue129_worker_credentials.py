"""Private Worker credentials: one-time exchange, rotation, recovery and races."""

import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Worker, WorkerCredential
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import worker_credential_operations as operations
from dlr.control.services import worker_credentials as credentials
from dlr.control.services.worker_identity_reservation import reserve_identity
from test_issue129_control_single_process import declaration  # noqa: F401


def _worker(factory: sessionmaker[Session]) -> int:
    with factory.begin() as session:
        row = Worker(name=f"identity-{uuid4()}", status="offline", capabilities=[])
        session.add(row)
        session.flush()
        return row.id


def _enrolled(factory: sessionmaker[Session], worker_id: int) -> credentials.IssuedWorkerCredential:
    with factory.begin() as session:
        enrollment = credentials.issue_enrollment(session, worker_id)
    with factory.begin() as session:
        return credentials.exchange_enrollment(session, worker_id, enrollment.secret)


def _assert_invalid(factory: sessionmaker[Session], worker_id: int, token: str) -> None:
    with factory.begin() as session, pytest.raises(credentials.WorkerCredentialError) as error:
        credentials.authenticate_runtime_credential(session, worker_id, token)
    assert error.value.code == "worker_credential_invalid"


def test_enrollment_bound_to_worker_one_time_and_digest_only(
    session_factory: sessionmaker[Session],
) -> None:
    first, second = _worker(session_factory), _worker(session_factory)
    with session_factory.begin() as session:
        enrollment = credentials.issue_enrollment(session, first)
        assert enrollment.expires_at is not None
        now = session.scalar(select(func.clock_timestamp()))
        assert now is not None
        assert 0 < (enrollment.expires_at - now).total_seconds() <= 600
    with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
        credentials.exchange_enrollment(session, second, enrollment.secret)
    with session_factory.begin() as session:
        runtime = credentials.exchange_enrollment(session, first, enrollment.secret)
        assert enrollment.secret not in repr(enrollment)
        assert runtime.secret not in repr(runtime)
    with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
        credentials.exchange_enrollment(session, first, enrollment.secret)
    with session_factory.begin() as session:
        assert (
            credentials.authenticate_runtime_credential(session, first, runtime.secret).id == first
        )
        rows = list(
            session.scalars(select(WorkerCredential).where(WorkerCredential.worker_id == first))
        )
        assert len(rows) == 2
        assert all(len(row.secret_digest) == 32 for row in rows)
        assert all(
            row.secret_digest != bytes.fromhex(runtime.secret.removeprefix("dlrw4_"))
            for row in rows
        )
    _assert_invalid(session_factory, second, runtime.secret)


def test_enrollment_expiry_reissue_and_rollback(session_factory: sessionmaker[Session]) -> None:
    worker_id = _worker(session_factory)
    with session_factory.begin() as session:
        first = credentials.issue_enrollment(session, worker_id)
    with session_factory.begin() as session:
        second = credentials.issue_enrollment(session, worker_id)
    with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
        credentials.exchange_enrollment(session, worker_id, first.secret)
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE worker_credentials SET expires_at=clock_timestamp() WHERE id=:id"),
            {"id": second.credential_id},
        )
    with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
        credentials.exchange_enrollment(session, worker_id, second.secret)
    with session_factory.begin() as session:
        third = credentials.issue_enrollment(session, worker_id)
    with session_factory() as session:
        runtime = credentials.exchange_enrollment(session, worker_id, third.secret)
        session.rollback()
    _assert_invalid(session_factory, worker_id, runtime.secret)
    with session_factory.begin() as session:
        credentials.exchange_enrollment(session, worker_id, third.secret)
    with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
        credentials.issue_enrollment(session, worker_id)
    for ttl in (0, 601, True):
        with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
            credentials.issue_enrollment(session, worker_id, ttl_seconds=ttl)


def test_rotation_is_bounded_and_recovery_preserves_instance(
    session_factory: sessionmaker[Session],
) -> None:
    first, second = _worker(session_factory), _worker(session_factory)
    first_token = _enrolled(session_factory, first)
    second_token = _enrolled(session_factory, second)
    with session_factory.begin() as session:
        next_token = credentials.rotate_runtime_credential(session, first, overlap_seconds=120)
        previous = session.get(WorkerCredential, first_token.credential_id)
        assert previous is not None and previous.expires_at is not None
        first_deadline = previous.expires_at
    with session_factory.begin() as session:
        credentials.authenticate_runtime_credential(session, first, first_token.secret)
        credentials.authenticate_runtime_credential(session, first, next_token.secret)
        newest = credentials.rotate_runtime_credential(session, first, overlap_seconds=600)
        previous = session.get(WorkerCredential, first_token.credential_id)
        assert previous is not None and previous.expires_at == first_deadline
        assert previous.revoked_at is not None
    _assert_invalid(session_factory, first, first_token.secret)
    with session_factory.begin() as session:
        credentials.authenticate_runtime_credential(session, first, next_token.secret)
        credentials.authenticate_runtime_credential(session, second, second_token.secret)
        worker = session.get(Worker, first)
        assert worker is not None
        identity = worker.identity_id
        enrolled_at = worker.identity_enrolled_at
        instance = worker.instance_id
        generation = worker.instance_generation
        recovered = credentials.recover_runtime_credential(session, first)
    _assert_invalid(session_factory, first, next_token.secret)
    _assert_invalid(session_factory, first, newest.secret)
    with session_factory.begin() as session:
        worker = credentials.authenticate_runtime_credential(session, first, recovered.secret)
        assert (
            worker.identity_id,
            worker.identity_enrolled_at,
            worker.instance_id,
            worker.instance_generation,
        ) == (identity, enrolled_at, instance, generation)
        credentials.authenticate_runtime_credential(session, second, second_token.secret)


def test_revocation_and_exact_expiry_fail_closed(session_factory: sessionmaker[Session]) -> None:
    worker_id = _worker(session_factory)
    runtime = _enrolled(session_factory, worker_id)
    with session_factory.begin() as session:
        credentials.revoke_runtime_credential(session, worker_id, runtime.credential_id)
        credentials.revoke_runtime_credential(session, worker_id, runtime.credential_id)
    _assert_invalid(session_factory, worker_id, runtime.secret)
    with (
        session_factory.begin() as session,
        pytest.raises(credentials.WorkerCredentialError) as error,
    ):
        credentials.rotate_runtime_credential(session, worker_id)
    assert error.value.code == "worker_runtime_recovery_required"
    with session_factory.begin() as session:
        recovered = credentials.recover_runtime_credential(session, worker_id)
        credentials.rotate_runtime_credential(session, worker_id, overlap_seconds=0)
    _assert_invalid(session_factory, worker_id, recovered.secret)
    for overlap in (-1, 601, True):
        with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
            credentials.rotate_runtime_credential(session, worker_id, overlap_seconds=overlap)


def test_concurrent_exchange_only_one_succeeds(session_factory: sessionmaker[Session]) -> None:
    worker_id = _worker(session_factory)
    with session_factory.begin() as session:
        enrollment = credentials.issue_enrollment(session, worker_id)
    barrier = Barrier(2)

    def exchange() -> str:
        barrier.wait(timeout=10)
        try:
            with session_factory.begin() as session:
                return credentials.exchange_enrollment(session, worker_id, enrollment.secret).secret
        except credentials.WorkerCredentialError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: exchange(), range(2)))
    assert results.count("rejected") == 1
    winner = next(token for token in results if token != "rejected")
    with session_factory.begin() as session:
        credentials.authenticate_runtime_credential(session, worker_id, winner)
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkerCredential)
                .where(WorkerCredential.worker_id == worker_id, WorkerCredential.kind == "runtime")
            )
            == 1
        )


def test_revoke_serializes_with_authentication(session_factory: sessionmaker[Session]) -> None:
    worker_id = _worker(session_factory)
    runtime = _enrolled(session_factory, worker_id)
    revoked = Event()
    release = Event()
    attempting = Event()
    finished = Event()

    def revoke() -> None:
        with session_factory.begin() as session:
            credentials.revoke_runtime_credential(session, worker_id, runtime.credential_id)
            revoked.set()
            assert release.wait(timeout=10)

    def authenticate() -> str:
        assert revoked.wait(timeout=10)
        attempting.set()
        try:
            with session_factory.begin() as session:
                credentials.authenticate_runtime_credential(session, worker_id, runtime.secret)
        except credentials.WorkerCredentialError as error:
            return error.code
        finally:
            finished.set()
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=2) as pool:
        revoke_future = pool.submit(revoke)
        auth_future = pool.submit(authenticate)
        try:
            assert attempting.wait(timeout=10)
            assert not finished.wait(timeout=0.2)
        finally:
            release.set()
        revoke_future.result(timeout=10)
        assert auth_future.result(timeout=10) == "worker_credential_invalid"


def test_credential_lookup_and_revoke_target_one_row_with_large_history(
    session_factory: sessionmaker[Session],
) -> None:
    worker_id = _worker(session_factory)
    runtime = _enrolled(session_factory, worker_id)
    with session_factory.begin() as session:
        now = session.scalar(select(func.clock_timestamp()))
        assert now is not None
        session.add_all(
            WorkerCredential(
                worker_id=worker_id,
                kind="runtime",
                secret_digest=hashlib.sha256(f"historical-{worker_id}-{n}".encode()).digest(),
                expires_at=now - timedelta(seconds=1),
                revoked_at=now if n % 2 else None,
            )
            for n in range(1200)
        )
    with session_factory.begin() as session:
        session.execute(text("ANALYZE worker_credentials"))
        digest = hashlib.sha256(bytes.fromhex(runtime.secret.removeprefix("dlrw4_"))).digest()
        plan = (
            session.execute(
                text(
                    "EXPLAIN SELECT id FROM worker_credentials "
                    "WHERE secret_digest = :digest AND worker_id = :worker_id AND kind = 'runtime'"
                ),
                {"digest": digest, "worker_id": worker_id},
            )
            .scalars()
            .all()
        )
        assert any("uq_worker_credentials_secret_digest" in row for row in plan)
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def record(_conn, _cursor, statement, *_args):
        if "from worker_credentials" in statement.lower():
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record)
    try:
        with session_factory.begin() as session:
            credentials.prescreen_runtime_credential(session, worker_id, runtime.secret)
            credentials.authenticate_runtime_credential(session, worker_id, runtime.secret)
            credentials.revoke_runtime_credential(session, worker_id, runtime.credential_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 3
    assert all("secret_digest =" in sql for sql in statements[:2])
    assert "worker_credentials.id =" in statements[2]
    assert "for update" not in statements[0]
    assert "for update" in statements[1] and "for update" in statements[2]
    assert all(runtime.secret not in sql for sql in statements)
    _assert_invalid(session_factory, worker_id, runtime.secret)


def test_revoke_cannot_cross_worker_identity(session_factory: sessionmaker[Session]) -> None:
    first, second = _worker(session_factory), _worker(session_factory)
    runtime = _enrolled(session_factory, first)
    with session_factory.begin() as session, pytest.raises(credentials.WorkerCredentialError):
        credentials.revoke_runtime_credential(session, second, runtime.credential_id)
    with session_factory.begin() as session:
        credentials.authenticate_runtime_credential(session, first, runtime.secret)


def test_recovery_revokes_expired_and_active_credentials_with_large_history(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_id = _worker(session_factory)
    original = _enrolled(session_factory, worker_id)
    expired_secret = "dlrw4_" + secrets.token_hex(32)
    with session_factory.begin() as session:
        previous = credentials.rotate_runtime_credential(session, worker_id, overlap_seconds=300)
        now = session.scalar(select(func.clock_timestamp()))
        assert now is not None
        expired = WorkerCredential(
            worker_id=worker_id,
            kind="runtime",
            secret_digest=hashlib.sha256(
                bytes.fromhex(expired_secret.removeprefix("dlrw4_"))
            ).digest(),
            expires_at=now - timedelta(seconds=1),
        )
        session.add(expired)
        session.flush()
        expired_id = expired.id
        session.add_all(
            WorkerCredential(
                worker_id=worker_id,
                kind="runtime",
                secret_digest=hashlib.sha256(f"expired-{worker_id}-{n}".encode()).digest(),
                expires_at=now - timedelta(seconds=1),
            )
            for n in range(1000)
        )
    with session_factory.begin() as session:
        recovered = credentials.recover_runtime_credential(session, worker_id)
        expired = session.get(WorkerCredential, expired_id)
        assert expired is not None and expired.revoked_at is not None
    _assert_invalid(session_factory, worker_id, original.secret)
    _assert_invalid(session_factory, worker_id, previous.secret)
    with session_factory.begin() as session:
        credentials.authenticate_runtime_credential(session, worker_id, recovered.secret)
    # Re-evaluating at a time before expiry must not resurrect the old token.
    monkeypatch.setattr(credentials, "_now", lambda _session: now - timedelta(seconds=30))
    _assert_invalid(session_factory, worker_id, expired_secret)


def test_recovery_serializes_with_old_credential_authentication(
    session_factory: sessionmaker[Session],
) -> None:
    worker_id = _worker(session_factory)
    runtime = _enrolled(session_factory, worker_id)
    recovered, release, attempting, finished = Event(), Event(), Event(), Event()

    def recover() -> None:
        with session_factory.begin() as session:
            credentials.recover_runtime_credential(session, worker_id)
            recovered.set()
            assert release.wait(timeout=10)

    def authenticate() -> str:
        assert recovered.wait(timeout=10)
        attempting.set()
        try:
            with session_factory.begin() as session:
                credentials.authenticate_runtime_credential(session, worker_id, runtime.secret)
        except credentials.WorkerCredentialError as error:
            return error.code
        finally:
            finished.set()
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery_future = pool.submit(recover)
        auth_future = pool.submit(authenticate)
        try:
            assert attempting.wait(timeout=10)
            assert not finished.wait(timeout=0.2)
        finally:
            release.set()
        recovery_future.result(timeout=10)
        assert auth_future.result(timeout=10) == "worker_credential_invalid"


# J uses the existing real-PG credential suite, not an additional runner.


@pytest.fixture
def credential_operation(session_factory, declaration):  # noqa: F811
    with session_factory.begin() as session:
        reserved = reserve_identity(
            session,
            name="credential-j-" + uuid4().hex,
            request_id=uuid4(),
            principal=SUPERADMIN_PRINCIPAL,
        )
        worker_id = reserved.worker_id
        identity = session.get(Worker, worker_id).identity_id
        version = session.get(Worker, worker_id).maintenance_version
    issued = _enrolled(session_factory, worker_id)
    payload = operations.RotateRequest(
        request_id=uuid4(),
        worker_identity_id=identity,
        expected_latest_credential_record_id=issued.credential_id,
        expected_maintenance_version=version,
        overlap_seconds=30,
    )
    return worker_id, issued, payload


def test_operation_committed_nonce_is_nonsecret_and_cannot_change_parameters(
    session_factory, credential_operation
):
    worker_id, old, payload = credential_operation
    with session_factory.begin() as session:
        receipt, issued = operations.execute(
            session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate"
        )
        assert issued is not None
        metadata = dict(receipt.result_metadata)
        assert old.secret not in json.dumps(metadata) and issued.secret not in json.dumps(metadata)
        assert len(json.dumps(metadata).encode()) < 2048
    with session_factory.begin() as session:
        again, secret = operations.execute(
            session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate"
        )
        assert secret is None and again.result_metadata == metadata
        assert (
            session.get(Worker, worker_id).maintenance_version
            == payload.expected_maintenance_version
        )
    with session_factory.begin() as session, pytest.raises(operations.OperationConflict):
        operations.execute(
            session,
            worker_id,
            payload.model_copy(update={"overlap_seconds": 31}),
            SUPERADMIN_PRINCIPAL,
            action="rotate",
        )
    with session_factory() as session:
        result = operations.read_receipt(
            session, worker_id, payload.request_id, SUPERADMIN_PRINCIPAL
        )
        assert result["status"] == "committed" and result["currently_usable"] is True
        assert (
            session.scalar(
                text("SELECT count(*) FROM worker_credential_audit WHERE request_id=:id"),
                {"id": payload.request_id},
            )
            == 1
        )


def test_operation_rollback_has_no_nonce_and_explicit_same_nonce_can_first_commit(
    session_factory, credential_operation
):
    worker_id, old, payload = credential_operation
    with session_factory() as session:
        operations.execute(session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate")
        session.rollback()  # The fixture knows this rolled back, unlike a lost reply.
    with session_factory() as session:
        assert (
            operations.read_receipt(session, worker_id, payload.request_id, SUPERADMIN_PRINCIPAL)[
                "status"
            ]
            == "not_observed"
        )
        assert operations.latest(session, worker_id).id == old.credential_id
    with session_factory.begin() as session:
        _, issued = operations.execute(
            session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate"
        )
        assert issued is not None


def test_operation_same_nonce_concurrency_only_one_durable_issue(
    session_factory, credential_operation
):
    worker_id, old, payload = credential_operation
    start = Barrier(2)

    def rotate():
        start.wait(timeout=5)
        with session_factory.begin() as session:
            _, issued = operations.execute(
                session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate"
            )
            return issued is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: rotate(), range(2))) == [False, True]
    with session_factory() as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM worker_credential_audit WHERE request_id=:id"),
                {"id": payload.request_id},
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkerCredential)
                .where(WorkerCredential.worker_id == worker_id, WorkerCredential.kind == "runtime")
            )
            == 2
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"overlap_seconds": True},
        {"overlap_seconds": 1.2},
        {"overlap_seconds": -1},
        {"overlap_seconds": 601},
        {"expected_latest_credential_record_id": True},
        {"expected_maintenance_version": False},
        {"worker_identity_id": str(UUID(int=0))},
        {"request_id": str(UUID(int=0))},
        {"unexpected": "forbidden"},
    ],
)
def test_operation_strict_public_payload(credential_operation, changes):
    from pydantic import ValidationError

    _, _, payload = credential_operation
    with pytest.raises(ValidationError):
        operations.RotateRequest.model_validate(payload.model_dump(mode="json") | changes)


@pytest.mark.parametrize("overlap", [0, 600])
def test_operation_exact_overlap_and_no_deadline_extension(
    session_factory, credential_operation, overlap
):
    worker_id, old, payload = credential_operation
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE worker_credentials SET expires_at=clock_timestamp()+interval '2s' "
                "WHERE id=:id"
            ),
            {"id": old.credential_id},
        )
        original = session.get(WorkerCredential, old.credential_id).expires_at
        receipt, _ = operations.execute(
            session,
            worker_id,
            payload.model_copy(update={"overlap_seconds": overlap}),
            SUPERADMIN_PRINCIPAL,
            action="rotate",
        )
        from datetime import datetime

        deadline = datetime.fromisoformat(receipt.result_metadata["previous_expires_at"])
        assert deadline <= original
        if overlap == 0:
            assert deadline <= credentials._now(session)


def test_operation_latest_revoked_record_never_falls_back_to_overlap(
    session_factory, credential_operation
):
    worker_id, old, payload = credential_operation
    with session_factory.begin() as session:
        _, issued = operations.execute(
            session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate"
        )
        credentials.revoke_runtime_credential(session, worker_id, issued.credential_id)
    with session_factory.begin() as session, pytest.raises(operations.OperationConflict):
        operations.execute(
            session,
            worker_id,
            payload.model_copy(
                update={
                    "request_id": uuid4(),
                    "expected_latest_credential_record_id": issued.credential_id,
                }
            ),
            SUPERADMIN_PRINCIPAL,
            action="rotate",
        )
    with session_factory.begin() as session:
        credentials.authenticate_runtime_credential(session, worker_id, old.secret)


@pytest.mark.parametrize("first", ["activate", "recover"])
def test_operation_activate_or_recover_before_rotate_rejects_old_cas(
    session_factory, credential_operation, first
):
    worker_id, _, payload = credential_operation
    with session_factory.begin() as session:
        if first == "activate":
            session.get(Worker, worker_id).scheduling_state = "active"
        else:
            credentials.recover_runtime_credential(session, worker_id)
    with session_factory.begin() as session, pytest.raises(operations.OperationConflict):
        operations.execute(session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate")


def test_operation_revoke_all_including_expired_without_changing_other_worker(
    session_factory, credential_operation
):
    worker_id, old, payload = credential_operation
    other = _worker(session_factory)
    other_secret = _enrolled(session_factory, other)
    with session_factory.begin() as session:
        _, issued = operations.execute(
            session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate"
        )
        session.execute(
            text(
                "UPDATE worker_credentials SET expires_at=clock_timestamp()-interval '1s' "
                "WHERE id=:id"
            ),
            {"id": old.credential_id},
        )
        row = session.get(Worker, worker_id)
        row.scheduling_state = "active"
        version = row.maintenance_version
    with session_factory.begin() as session:
        revoke = operations.RevokeRequest(
            request_id=uuid4(),
            worker_identity_id=payload.worker_identity_id,
            expected_latest_credential_record_id=issued.credential_id,
        )
        operations.execute(session, worker_id, revoke, SUPERADMIN_PRINCIPAL, action="revoke")
        assert session.get(Worker, worker_id).maintenance_version == version
        assert session.get(Worker, worker_id).scheduling_state == "active"
    with session_factory.begin() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkerCredential)
                .where(
                    WorkerCredential.worker_id == worker_id,
                    WorkerCredential.kind == "runtime",
                    WorkerCredential.revoked_at.is_(None),
                )
            )
            == 0
        )
        credentials.authenticate_runtime_credential(session, other, other_secret.secret)


def test_operation_receipt_is_immutable_in_database(session_factory, credential_operation):
    from sqlalchemy.exc import DBAPIError

    worker_id, _, payload = credential_operation
    with session_factory.begin() as session:
        operations.execute(session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate")
    for statement in [
        "UPDATE worker_credential_audit SET request_id=NULL WHERE request_id=:id",
        "DELETE FROM worker_credential_audit WHERE request_id=:id",
    ]:
        with session_factory.begin() as session, pytest.raises(DBAPIError):
            session.execute(text(statement), {"id": payload.request_id})
        with session_factory.begin() as session, pytest.raises(DBAPIError):
            session.execute(text("SET LOCAL session_replication_role = replica"))
            session.execute(text(statement), {"id": payload.request_id})


def test_operation_nonce_and_receipt_cannot_cross_actor_or_worker(
    session_factory, credential_operation
):
    from dlr.control.security import Principal

    worker_id, _, payload = credential_operation
    with session_factory.begin() as session:
        operations.execute(session, worker_id, payload, SUPERADMIN_PRINCIPAL, action="rotate")
        reserved = reserve_identity(
            session,
            name="other-j-" + uuid4().hex,
            request_id=uuid4(),
            principal=SUPERADMIN_PRINCIPAL,
        )
        second = reserved.worker_id
    _enrolled(session_factory, second)
    actor = Principal(kind="account", role="admin", user_id=123)
    for target, principal in [(worker_id, actor), (second, SUPERADMIN_PRINCIPAL)]:
        with session_factory.begin() as session, pytest.raises(operations.OperationConflict):
            operations.execute(session, target, payload, principal, action="rotate")
        with session_factory() as session, pytest.raises(operations.OperationConflict):
            operations.read_receipt(session, target, payload.request_id, principal)


def test_operation_regular_account_is_rejected_by_actual_admin_dependency(api_client, monkeypatch):
    from fastapi.testclient import TestClient

    from dlr.common.config import settings
    from dlr.control.app import create_app
    from dlr.control.security import Principal, require_principal

    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    app = create_app()
    app.dependency_overrides.update(api_client.app.dependency_overrides)
    client = TestClient(app)

    monkeypatch.setitem(
        app.dependency_overrides,
        require_principal,
        lambda: Principal(kind="account", role="user", user_id=123),
    )
    endpoint = "/api/workers/1/credentials/runtime"
    for path in (endpoint, endpoint + "/operations/" + str(uuid4())):
        assert client.get(path).status_code == 403
    for action in ("rotate", "revoke"):
        response = client.post(
            endpoint + "/" + action,
            json={
                "request_id": str(uuid4()),
                "worker_identity_id": str(uuid4()),
                "expected_latest_credential_record_id": 1,
                **({"expected_maintenance_version": 1} if action == "rotate" else {}),
            },
        )
        assert response.status_code == 403


def test_operation_validation_never_echoes_rejected_values(api_client, monkeypatch):
    from fastapi.testclient import TestClient

    from dlr.common.config import settings
    from dlr.control.app import create_app

    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    app = create_app()
    app.dependency_overrides.update(api_client.app.dependency_overrides)
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-token"})
    marker = "rejected-value-must-not-be-reflected"
    for path in [
        "/api/workers/1/credentials/runtime/rotate",
        "/api/workers/1/credentials/runtime/revoke",
    ]:
        result = client.post(path, json={"unexpected": marker})
        assert result.status_code == 422 and marker not in result.text
        assert result.headers["cache-control"] == "no-store"
    result = client.get("/api/workers/" + marker + "/credentials/runtime")
    assert result.status_code == 422 and marker not in result.text


def test_operation_zero_observation_id_is_a_fixed_invalid_request(api_client, monkeypatch):
    from fastapi.testclient import TestClient

    from dlr.common.config import settings
    from dlr.control.app import create_app

    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    app = create_app()
    app.dependency_overrides.update(api_client.app.dependency_overrides)
    client = TestClient(app, headers={"Authorization": "Bearer test-admin-token"})
    response = client.get("/api/workers/1/credentials/runtime/operations/" + str(UUID(int=0)))
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
