"""Private Worker credentials: one-time exchange, rotation, recovery and races."""

import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Worker, WorkerCredential
from dlr.control.services import worker_credentials as credentials


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
