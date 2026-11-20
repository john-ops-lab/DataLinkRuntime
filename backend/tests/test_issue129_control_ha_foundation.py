"""Dual-Control admission and durable intake limits on real PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from dlr.common.config import settings
from dlr.control import db
from dlr.control.app import create_app
from dlr.control.services import control_cluster_identity as identity
from dlr.control.services import control_ha_capacity as state
from dlr.control.services import ingress_capacity, rabbitmq
from dlr.control.services import postgres_capacity_provider as provider


@pytest.fixture
def ha_authority(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "postgres_capacity_url", "https://capacity.example.test/v1")
    monkeypatch.setattr(settings, "postgres_capacity_token", "test-capacity-token")
    authority = identity.ControlDeclaration(
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
    clean_authority = text(
        "TRUNCATE control_ha_capacity, control_process_instances, control_cluster_authority"
    )
    with session_factory.begin() as session:
        session.execute(clean_authority)
        identity.provision_authority(session, authority, authority_epoch=uuid4())
        session.execute(
            text("""
                INSERT INTO control_ha_capacity
                    (id, fingerprint, storage_domain_id, topology_epoch)
                VALUES (1, :fingerprint, 'data', 1)
            """),
            {"fingerprint": state.fingerprint(settings)},
        )
    try:
        yield authority
    finally:
        with session_factory.begin() as session:
            session.execute(clean_authority)


def test_two_slots_serialize_concurrent_joins_and_fence_expired_slot(
    session_factory, ha_authority
) -> None:
    barrier = Barrier(3)

    def join(slot):
        identifier, token = uuid4(), identity.new_instance_token()
        barrier.wait(timeout=5)
        try:
            with session_factory.begin() as session:
                identity.register_control(
                    session,
                    ha_authority,
                    instance_id=identifier,
                    instance_token=token,
                    ha_slot=slot,
                )
        except identity.ControlIdentityUnavailable:
            return None
        return slot, identifier, token

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = [item for item in executor.map(join, ("a", "b", "a")) if item is not None]
    assert sorted(item[0] for item in results) == ["a", "b"]
    _, identifier, token = next(item for item in results if item[0] == "a")
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE control_process_instances SET lease_until=clock_timestamp() "
                "WHERE ha_slot='a'"
            )
        )
    with pytest.raises(identity.ControlIdentityUnavailable), session_factory.begin() as session:
        identity.register_control(
            session,
            replace(ha_authority, master_key="x" * 64),
            instance_id=uuid4(),
            instance_token=identity.new_instance_token(),
            ha_slot="a",
        )
    with session_factory.begin() as session:
        identity.register_control(
            session,
            ha_authority,
            instance_id=uuid4(),
            instance_token=identity.new_instance_token(),
            ha_slot="a",
        )
    with pytest.raises(identity.ControlIdentityUnavailable), session_factory.begin() as session:
        identity.renew_control(
            session,
            ha_authority,
            instance_id=identifier,
            instance_token=token,
        )


def test_storage_observation_order_and_hysteresis_are_database_authoritative(
    session_factory, ha_authority, monkeypatch
) -> None:
    class Registered:
        def capacity_compatible(self) -> bool:
            return True

        def assert_live(self, session) -> None:
            pass

    monkeypatch.setattr(state, "_local_process", Registered())
    current = provider.VerifiedCapacityAuthority(str(ha_authority.cluster_id), "data", 1)
    now = datetime.now(UTC)
    first = now - timedelta(seconds=3)
    second = now - timedelta(seconds=2)
    third = now - timedelta(seconds=1)

    def observe(at, free, expected):
        sample = provider.parse_observation(
            json.dumps(
                {
                    "schema_version": 1,
                    "cluster_id": current.cluster_id,
                    "storage_domain_id": "data",
                    "topology_epoch": 1,
                    "observed_at": at.isoformat(),
                    "writable_free_bytes": free,
                }
            ).encode(),
            current,
        )
        with session_factory.begin() as reporter:
            state._record(reporter, state._start_probe(reporter), sample)
        with session_factory() as receiver:
            assert state.storage_state(receiver, for_accept=True) == expected

    observe(first, 7 * 1024**3, "available")
    observe(second, 5 * 1024**3, "low")
    observe(first, 7 * 1024**3, "low")
    observe(second, 7 * 1024**3, "conflict")
    observe(second, 5 * 1024**3, "conflict")
    observe(third, 6 * 1024**3, "available")
    first_failed_at = now - timedelta(seconds=4)
    with session_factory.begin() as session:
        state._record(session, state._Probe(current, first_failed_at), None)
        assert state.storage_state(session) == "available"
        state._record(session, state._start_probe(session), None)
        assert state.storage_state(session) == "unavailable"


def test_enrolled_database_rejects_noncluster_intake(session_factory, ha_authority, monkeypatch):
    def unexpected_disk_read():
        pytest.fail("cluster database must not inspect the Control's local disk")

    monkeypatch.setattr(ingress_capacity, "_storage_free_bytes", unexpected_disk_read)
    with session_factory.begin() as session:
        assert not ingress_capacity.activate_profile(session)
        with pytest.raises(HTTPException) as error:
            ingress_capacity.require_active_profile(session)
        assert error.value.detail["code"] == "ingress_capacity_unavailable"


def test_dual_ingress_rejects_known_broker_outage_despite_topology_cache(monkeypatch):
    monkeypatch.setattr(settings, "control_ha_slot", "a")
    monkeypatch.setattr(rabbitmq, "_verified_ingress_workers", lambda session: (True, None))
    monkeypatch.setattr(rabbitmq, "_runtime_status", dict(rabbitmq._runtime_status))
    rabbitmq.mark_runtime_ready()
    assert rabbitmq.ingress_configuration_ready()
    rabbitmq.mark_runtime_failure("topology_unavailable")
    assert not rabbitmq.ingress_configuration_ready()


def test_ready_checks_write_authority_without_using_broker(test_engine, monkeypatch) -> None:
    session_factory = sessionmaker(bind=test_engine)
    monkeypatch.setattr(db, "SessionLocal", session_factory)
    client = TestClient(create_app())
    assert client.get("/api/health/live").status_code == 200
    assert client.get("/api/health/ready").status_code == 200

    def read_only(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    event.listen(test_engine, "begin", read_only)
    try:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 503
    finally:
        event.remove(test_engine, "begin", read_only)
