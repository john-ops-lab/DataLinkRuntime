"""Compatibility and fail-closed checks for the opted-in Broker HA contract."""

import base64

import pytest
from pydantic import SecretStr, ValidationError

from dlr.common.config import Settings, settings
from dlr.control.services import control_ha_capacity, rabbitmq

NODES = ["rabbit@a", "rabbit@b", "rabbit@c"]


@pytest.fixture
def ha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rabbitmq_ha_enabled", True)
    monkeypatch.setattr(rabbitmq, "_quorum_observations", {})


@pytest.mark.parametrize(
    ("members", "online", "leader", "state", "error"),
    [
        (NODES, NODES, NODES[0], "healthy", None),
        (NODES, NODES[1:], NODES[1], "degraded", None),
        (NODES, NODES[:1], NODES[0], "unavailable", "topology_unavailable"),
        (NODES, NODES[1:], NODES[0], "unavailable", "topology_unavailable"),
        (NODES[:1], NODES[:1], NODES[0], "incomplete", "topology_drift"),
        (NODES, [NODES[0]] * 3, NODES[0], "unknown", "topology_unavailable"),
        (NODES, [NODES[0], "rabbit@other"], NODES[0], "unknown", "topology_unavailable"),
    ],
)
def test_quorum_needs_the_online_leader_of_a_declared_majority(
    ha, members, online, leader, state, error
) -> None:
    payload = {"members": members, "online": online, "leader": leader}
    if error is None:
        rabbitmq._require_queue_quorum("1", payload)
    else:
        with pytest.raises(rabbitmq.RabbitMQTopologyError) as exc:
            rabbitmq._require_queue_quorum("1", payload)
        assert exc.value.code == error
    health = rabbitmq._broker_health_payload()
    assert health["quorum_queues"]["1"]["state"] == state
    assert not any(node in repr(health) for node in NODES)


def test_ha_requires_quorum_on_both_queue_families(ha, monkeypatch: pytest.MonkeyPatch) -> None:
    healthy = {"type": "quorum", "members": NODES, "online": NODES, "leader": NODES[0]}
    work = {**healthy, "arguments": rabbitmq.work_queue_arguments()}
    dlq = {**healthy, "arguments": rabbitmq.infrastructure_queue_arguments(), "online": NODES[:1]}
    queues = {rabbitmq.topology_names(1).queue: work, rabbitmq.INFRASTRUCTURE_DLQ: dlq}
    monkeypatch.setattr(rabbitmq, "_fetch_queue_details", queues.__getitem__)
    with pytest.raises(rabbitmq.RabbitMQTopologyError) as exc:
        rabbitmq.inspect_topology_policies(1)
    assert exc.value.code == "topology_unavailable"
    assert "broker_queue_quorum_unavailable" in rabbitmq._broker_health_payload()["alerts"]


def test_expired_quorum_evidence_turns_unknown(ha, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rabbitmq, "monotonic", lambda: 20.0)
    rabbitmq._require_queue_quorum("1", {"members": NODES, "online": NODES, "leader": NODES[0]})
    monkeypatch.setattr(rabbitmq, "monotonic", lambda: 30.1)
    health = rabbitmq._broker_health_payload()
    assert health["quorum_queues"]["1"] == {
        "state": "unknown",
        "members": None,
        "online": None,
        "leader_online": None,
    }
    assert "broker_queue_quorum_unknown" in health["alerts"]


def test_down_queue_cannot_reuse_earlier_membership_observation(ha) -> None:
    payload = {"members": NODES, "online": NODES, "leader": NODES[0], "state": "down"}
    with pytest.raises(rabbitmq.RabbitMQTopologyError) as exc:
        rabbitmq._require_queue_quorum("1", payload)
    assert exc.value.code == "topology_unavailable"


@pytest.mark.parametrize(
    "override",
    [
        {"DLR_RABBITMQ_MANAGEMENT_USER": "publisher"},
        {"DLR_RABBITMQ_MANAGEMENT_PASSWORD": ""},
        {"DLR_RABBITMQ_MANAGEMENT_URL": "http://management.example.invalid"},
        {"DLR_RABBITMQ_URL": "amqp://publisher:placeholder@broker.example.invalid/"},
        {"DLR_RABBITMQ_URL": "amqps://publisher:placeholder@broker.example.invalid/?heartbeat=5"},
    ],
)
def test_ha_settings_preserve_endpoint_trust(override) -> None:
    values = {
        "DLR_RABBITMQ_HA_ENABLED": True,
        "DLR_RABBITMQ_URL": "amqps://publisher:placeholder@broker.example.invalid/",
        "DLR_RABBITMQ_MANAGEMENT_URL": "https://management.example.invalid",
        "DLR_RABBITMQ_MANAGEMENT_USER": "observer",
        "DLR_RABBITMQ_MANAGEMENT_PASSWORD": "placeholder",
    }
    with pytest.raises(ValidationError) as exc:
        Settings(**(values | override))
    assert "placeholder" not in str(exc.value)


def test_ha_binds_management_identity_and_tls_name(ha, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "rabbitmq_url", "amqps://publisher:example@broker.example.invalid/"
    )
    monkeypatch.setattr(settings, "rabbitmq_management_user", "observer")
    monkeypatch.setattr(settings, "rabbitmq_management_password", SecretStr("first"))
    header = base64.b64decode(rabbitmq._management_auth_header().split()[1])
    assert header == b"observer:first"
    first = rabbitmq.configuration_fingerprint([1])
    monkeypatch.setattr(settings, "rabbitmq_management_password", SecretStr("second"))
    assert rabbitmq.configuration_fingerprint([1]) != first
    ssl_options = rabbitmq.connection_parameters().ssl_options
    assert ssl_options.server_hostname == "broker.example.invalid"
    assert ssl_options.context.check_hostname is True


def test_dual_control_capacity_fingerprint_binds_broker_ha() -> None:
    config = settings.model_copy(
        update={
            "postgres_capacity_url": "https://capacity.example.invalid",
            "postgres_capacity_token": "placeholder",
            "rabbitmq_ha_enabled": False,
        }
    )
    enabled = config.model_copy(update={"rabbitmq_ha_enabled": True})
    assert control_ha_capacity.fingerprint(config) != control_ha_capacity.fingerprint(enabled)
