"""Dedicated remote-mode admission uses real PostgreSQL transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import Settings
from dlr.control.services import control_cluster_identity as control
from dlr.control.services import control_process_v4 as lifecycle
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration
from dlr.control.services.worker_token_fence_v4 import TokenFenceUnavailable


@pytest.fixture
def declaration(session_factory):
    value = control.ControlDeclaration(
        cluster_id=uuid4(),
        image_digest="sha256:" + "1" * 64,
        managed_fingerprint="2" * 64,
        builtin_fingerprint="3" * 64,
        attachment_fingerprint="4" * 64,
        capacity_fingerprint="5" * 64,
        master_key="m" * 64,
        admin_token="a" * 64,
        worker_token="w" * 64,
    )
    with session_factory.begin() as session:
        session.execute(
            text("TRUNCATE control_process_instances, control_cluster_authority CASCADE")
        )
        control.provision_authority(session, value, authority_epoch=uuid4())
    return value


def test_single_control_concurrent_start_and_normal_restart(session_factory, declaration):
    barrier = Barrier(2)

    def start():
        instance, token = uuid4(), control.new_instance_token()
        barrier.wait(timeout=5)
        try:
            with session_factory.begin() as session:
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                control.register_control(
                    session,
                    declaration,
                    instance_id=instance,
                    instance_token=token,
                    single_process=True,
                )
            return instance, token
        except control.ControlIdentityUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: start(), range(2)))
    winners = [value for value in outcomes if value is not None]
    assert len(winners) == 1
    instance, token = winners[0]
    with session_factory.begin() as session:
        assert session.scalar(text("SELECT count(*) FROM control_process_instances")) == 1
        first = control.register_control(
            session, declaration, instance_id=instance, instance_token=token, single_process=True
        )
        renewed = control.renew_control(
            session, declaration, instance_id=instance, instance_token=token
        )
        assert renewed.lease_until >= first.lease_until
        control.fence_control(session, declaration, instance_id=instance, instance_token=token)
    with session_factory.begin() as session:
        replacement = control.register_control(
            session,
            declaration,
            instance_id=uuid4(),
            instance_token=control.new_instance_token(),
            single_process=True,
        )
        assert isinstance(replacement, control.DeclaredCompatibility)


def test_single_control_expired_identity_cannot_resume_after_restart(session_factory, declaration):
    instance, token = uuid4(), control.new_instance_token()
    with session_factory.begin() as session:
        control.register_control(
            session, declaration, instance_id=instance, instance_token=token, single_process=True
        )
        session.execute(
            text(
                "UPDATE control_process_instances "
                "SET lease_until = clock_timestamp() - interval '1s'"
            )
        )
    with session_factory.begin() as session:
        control.register_control(
            session,
            declaration,
            instance_id=uuid4(),
            instance_token=control.new_instance_token(),
            single_process=True,
        )
    with session_factory.begin() as session:
        assert isinstance(
            control.renew_control(session, declaration, instance_id=instance, instance_token=token),
            control.ExpiredControl,
        )


def _process(factory, declaration, monkeypatch):
    with factory() as session:
        epoch = session.scalar(text("SELECT authority_epoch FROM control_cluster_authority"))
    deployment = DeploymentDeclaration(authority=declaration, authority_epoch=epoch)
    monkeypatch.setattr(lifecycle, "read_declaration", lambda _: deployment)
    return lifecycle.ControlProcess(
        Settings(DLR_WORKER_REMOTE_V4_ENABLED=True), factory, deployment
    )


def test_control_renewal_in_flight_keeps_only_the_confirmed_deadline(
    session_factory, test_engine, declaration, monkeypatch
):
    process = _process(session_factory, declaration, monkeypatch)
    assert process._cycle()
    previous = process._confirmed_until
    entered, release = Event(), Event()

    def before_commit(_connection):
        entered.set()
        assert release.wait(5)

    event.listen(test_engine, "commit", before_commit)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            renewal = executor.submit(process._cycle)
            try:
                assert entered.wait(5)
                process.check_authority()
                assert process._confirmed_until == previous
            finally:
                release.set()
            assert renewal.result(timeout=5)
    finally:
        release.set()
        event.remove(test_engine, "commit", before_commit)
        process._finish()


def test_control_renewal_unknown_commit_cannot_use_the_old_confirmation(
    test_engine, session_factory, declaration, monkeypatch
):
    class UnknownCommit(Session):
        fail = False

        def commit(self):
            super().commit()
            if type(self).fail:
                raise OSError("synthetic unknown commit")

    factory = sessionmaker(bind=test_engine, class_=UnknownCommit, expire_on_commit=False)
    process = _process(factory, declaration, monkeypatch)
    assert process._cycle()
    UnknownCommit.fail = True
    try:
        with pytest.raises(OSError):
            process._cycle()
        with pytest.raises(TokenFenceUnavailable):
            process.check_authority()
    finally:
        UnknownCommit.fail = False
        process._finish()


@pytest.mark.parametrize(
    "outcome",
    [
        "released",
        "held",
        "revoked",
        "slot_changed",
        "expired_before_retry",
        "already_unknown",
        "unknown_commit",
        "unknown_rollback",
    ],
)
def test_control_renewal_row_contention(
    test_engine, session_factory, declaration, monkeypatch, outcome
):
    renewals = []
    renew_control = control.renew_control

    def observe_renewal(session, *args, **kwargs):
        if renewals:
            assert session is not renewals[0]
            with pytest.raises(TokenFenceUnavailable):
                process.check_authority()
        renewals.append(session)
        return renew_control(session, *args, **kwargs)

    class ContendedRenewal(Session):
        def rollback(self):
            with pytest.raises(TokenFenceUnavailable):
                process.check_authority()
            if len(renewals) == 1:
                assert process._renewal_retry is None
                with pytest.raises(TokenFenceUnavailable):
                    process.check_authority(wait_for_renewal_retry=True)
            super().rollback()
            if outcome == "unknown_rollback":
                raise OSError("rollback acknowledgement lost")
            if outcome == "expired_before_retry":
                monkeypatch.setattr(lifecycle, "_cache_clock", lambda: previous)
            if outcome == "slot_changed":
                reader.execute(
                    text(
                        "UPDATE control_process_instances SET ha_slot = 'b' WHERE instance_id = :id"
                    ),
                    {"id": process.instance_id},
                )
            if outcome == "revoked":
                control.fence_control(
                    reader,
                    declaration,
                    instance_id=process.instance_id,
                    instance_token=process._token,
                )
            if outcome != "held":
                reader.commit()

        def commit(self):
            if self in renewals:
                with pytest.raises(TokenFenceUnavailable):
                    process.check_authority()
            super().commit()
            if self in renewals and outcome == "unknown_commit":
                raise OSError("commit acknowledgement lost")

    factory = sessionmaker(bind=test_engine, class_=ContendedRenewal, expire_on_commit=False)
    process = _process(factory, declaration, monkeypatch)
    assert process._cycle()
    previous = process._confirmed_until
    if outcome == "already_unknown":
        with session_factory.begin() as holder:
            holder.execute(text("LOCK TABLE control_process_instances IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(control.ControlIdentityUnavailable):
                process._cycle()
        with pytest.raises(TokenFenceUnavailable):
            process.check_authority()
    monkeypatch.setattr(control, "renew_control", observe_renewal)
    with session_factory() as reader:
        control.assert_live_control(
            reader, declaration, instance_id=process.instance_id, instance_token=process._token
        )
        if outcome == "released":
            assert process._cycle()
            process.check_authority()
        elif outcome == "expired_before_retry":
            assert process._cycle() is False
            with pytest.raises(TokenFenceUnavailable):
                process.check_authority()
        else:
            error = (
                OSError
                if outcome in {"unknown_commit", "unknown_rollback"}
                else control.ControlIdentityUnavailable
            )
            with pytest.raises(error):
                process._cycle()
            assert process._confirmed_until == previous
            with pytest.raises(TokenFenceUnavailable):
                process.check_authority()
        single_attempt = outcome in {"already_unknown", "expired_before_retry", "unknown_rollback"}
        assert len(renewals) == (1 if single_attempt else 2)
        if outcome == "held":
            monkeypatch.setattr(lifecycle, "_cache_clock", lambda: previous)
            assert process._cycle() is False
            assert len(renewals) == 2
    process._finish()


@pytest.mark.parametrize("outcome", ["confirmed", "commit_unknown", "wait_timeout", "old_deadline"])
def test_worker_first_fence_waits_only_for_the_confirmed_control_retry(
    session_factory, test_engine, declaration, monkeypatch, outcome
):
    retry_started, allow_retry, worker_waiting = Event(), Event(), Event()
    renewals = []
    original_renew = control.renew_control
    fence = object()
    fence_obtained = Event()

    class RenewalSession(Session):
        def commit(self):
            super().commit()
            if outcome == "commit_unknown" and retry_started.is_set():
                raise OSError("renewal commit acknowledgement unavailable")

    def renew(session, *args, **kwargs):
        renewals.append(session)
        if len(renewals) == 2:
            retry_started.set()
            assert allow_retry.wait(5)
        return original_renew(session, *args, **kwargs)

    def acquire(*args, **kwargs):
        fence_obtained.set()
        return fence

    factory = sessionmaker(bind=test_engine, class_=RenewalSession, expire_on_commit=False)
    process = _process(factory, declaration, monkeypatch)
    assert process._cycle()
    previous = process._confirmed_until
    monkeypatch.setattr(control, "renew_control", renew)
    monkeypatch.setattr(lifecycle, "acquire_worker_fence", acquire)
    if outcome == "wait_timeout":
        monkeypatch.setattr(lifecycle, "_WORKER_RENEWAL_RETRY_WAIT_SECONDS", 0.1)
    now = 0.0
    if outcome == "old_deadline":
        assert previous is not None
        now = previous - 0.25
        monkeypatch.setattr(lifecycle, "_cache_clock", lambda: now)
    try:
        with session_factory() as holder, ThreadPoolExecutor(max_workers=2) as executor:
            control.assert_live_control(
                holder, declaration, instance_id=process.instance_id, instance_token=process._token
            )
            renewal = executor.submit(process._cycle)
            try:
                assert retry_started.wait(4)
                with pytest.raises(TokenFenceUnavailable):
                    process.check_authority()
                retry = process._renewal_retry
                assert retry is not None
                original_wait = retry.finished.wait

                def wait(seconds):
                    nonlocal now
                    worker_waiting.set()
                    if outcome == "old_deadline":
                        assert 0 < seconds <= 0.25
                        assert original_wait(4)
                        now = previous + 0.05
                        return True
                    return original_wait(seconds)

                monkeypatch.setattr(retry.finished, "wait", wait)

                def enter_worker():
                    with session_factory() as session:
                        return process.worker_fence(session, wait_for_renewal_retry=True)

                worker = executor.submit(enter_worker)
                assert worker_waiting.wait(1)
                assert not fence_obtained.is_set()
                if outcome == "wait_timeout":
                    with pytest.raises(TokenFenceUnavailable):
                        worker.result(timeout=2)
            finally:
                holder.rollback()
                allow_retry.set()
            if outcome == "commit_unknown":
                with pytest.raises(OSError):
                    renewal.result(timeout=4)
                with pytest.raises(TokenFenceUnavailable):
                    worker.result(timeout=2)
            else:
                assert renewal.result(timeout=4)
                if outcome == "confirmed":
                    assert worker.result(timeout=2) is fence
                elif outcome == "old_deadline":
                    with pytest.raises(TokenFenceUnavailable):
                        worker.result(timeout=2)
                    process.check_authority()  # The successor is live for a fresh caller.
        assert len(renewals) == 2
        assert fence_obtained.is_set() == (outcome == "confirmed")
        assert process._renewal_retry is None
    finally:
        process._finish()


@pytest.mark.parametrize(
    ("route", "initial_failure"), [("heartbeat", True), ("ready", True), ("ready", False)]
)
def test_worker_proof_handler_opts_in_only_at_initial_fence(monkeypatch, route, initial_failure):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock, call

    from fastapi import HTTPException

    from dlr.control.api import worker_instance_v4
    from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
    from dlr.control.services import worker_capacity

    instance = uuid4()
    process = Mock()
    monkeypatch.setattr(worker_instance_v4, "_process", lambda _: process)
    monkeypatch.setattr(
        worker_instance_v4, "_proved_identity", lambda *_: ("fixture", instance, 1, "proof")
    )

    async def stream():
        flags = {name: True for name in REQUIRED_ISOLATION_CAPABILITIES}
        yield json.dumps({"isolation_capabilities": flags}).encode()

    request = SimpleNamespace(url=SimpleNamespace(query=""), stream=stream)
    session = Mock()
    if initial_failure:
        process.worker_fence.side_effect = TokenFenceUnavailable("unconfirmed")
        with pytest.raises(HTTPException) as failure:
            if route == "heartbeat":
                worker_instance_v4.heartbeat(request, 1, session)
            else:
                asyncio.run(worker_instance_v4.ready(request, 1, session))
        assert failure.value.status_code == 503
        process.worker_fence.assert_called_once_with(session, wait_for_renewal_retry=True)
        session.commit.assert_not_called()
    else:
        session.get.return_value = SimpleNamespace(
            id=1,
            identity_lifecycle="reserved",
            instance_id=instance,
            instance_generation=1,
            v4_storage_profile=None,
            capacity_state="unready",
        )
        monkeypatch.setattr(worker_instance_v4.candidates, "validate_current_proof", Mock())
        monkeypatch.setattr(worker_instance_v4.worker_availability, "current_time", Mock())
        monkeypatch.setattr(
            worker_instance_v4.worker_availability,
            "has_unreconciled_v4_attempt",
            Mock(return_value=False),
        )
        monkeypatch.setattr(worker_capacity, "lock_capacity", Mock(return_value=None))

        response = asyncio.run(worker_instance_v4.ready(request, 1, session))
        assert response.status_code == 200
        assert process.worker_fence.call_args_list == [
            call(session, wait_for_renewal_retry=True),
            call(session),
        ]
        session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()


@pytest.mark.parametrize("ddl", ["table", "index"])
def test_control_renewal_ddl_timeout_is_not_retried(
    test_engine, session_factory, declaration, monkeypatch, ddl
):
    started = []
    renew_control = control.renew_control

    class ObserveClose(Session):
        def close(self):
            if self in started:
                with pytest.raises(TokenFenceUnavailable):
                    process.check_authority()
            super().close()

    factory = sessionmaker(bind=test_engine, class_=ObserveClose, expire_on_commit=False)
    process = _process(factory, declaration, monkeypatch)
    assert process._cycle()

    def observe_renewal(session, *args, **kwargs):
        started.append(session)
        session.execute(text("SET LOCAL enable_seqscan = off"))
        return renew_control(session, *args, **kwargs)

    monkeypatch.setattr(control, "renew_control", observe_renewal)
    with session_factory.begin() as holder:
        if ddl == "table":
            holder.execute(text("LOCK TABLE control_process_instances IN ACCESS EXCLUSIVE MODE"))
        else:
            index = holder.scalar(
                text(
                    "SELECT indexrelid::regclass::text FROM pg_index "
                    "WHERE indrelid = 'control_process_instances'::regclass AND indisprimary"
                )
            )
            holder.execute(
                text("REINDEX INDEX " + test_engine.dialect.identifier_preparer.quote(index))
            )
        with pytest.raises(control.ControlIdentityUnavailable):
            process._cycle()
        assert len(started) == 1
        with pytest.raises(TokenFenceUnavailable):
            process.check_authority()
    process._finish()


def test_control_process_does_not_restore_local_state_after_late_renew_commit(
    session_factory, declaration, monkeypatch
):
    process = _process(session_factory, declaration, monkeypatch)
    assert process._cycle()
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE control_process_instances "
                "SET lease_until = pg_catalog.clock_timestamp() + interval '1.5 seconds' "
                "WHERE instance_id = :id"
            ),
            {"id": process._instance},
        )
        session.execute(
            text(
                "CREATE FUNCTION dlr129_delay_local_control_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.8); RETURN NULL; END $$"
            )
        )
        session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER dlr129_delay_local_control_commit "
                "AFTER UPDATE ON control_process_instances DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION dlr129_delay_local_control_commit()"
            )
        )
    with process._lock:
        process._confirmed_until = lifecycle._cache_clock() + 1.5
    try:
        assert process._cycle() is False
        assert process.status().state == "unknown"
        assert process._cycle() is False
        with session_factory() as session:
            assert session.scalar(
                text(
                    "SELECT lease_until > pg_catalog.clock_timestamp() "
                    "FROM control_process_instances WHERE instance_id = :id"
                ),
                {"id": process._instance},
            )
    finally:
        with session_factory.begin() as session:
            session.execute(
                text("DROP TRIGGER dlr129_delay_local_control_commit ON control_process_instances")
            )
            session.execute(text("DROP FUNCTION dlr129_delay_local_control_commit()"))
        process._finish()
