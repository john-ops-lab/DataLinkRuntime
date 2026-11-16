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
