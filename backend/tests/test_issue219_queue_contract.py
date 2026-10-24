"""Queue extensions preserve closed configuration and durable shared responsibilities."""

import base64
import hashlib
from copy import deepcopy

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from dlr.common.config import settings
from dlr.common.jcs import canonicalize
from dlr.control.models import EventReceipt, EventSubscription
from dlr.control.schemas.subscription import SourceConfig
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.event_envelope import event_id, validate_envelope
from dlr.control.services.subscription_registry import (
    SOURCES,
    configuration_identity,
    source_definition,
    source_group_key,
)
from test_issue219_delivery import envelope as kafka_envelope
from test_issue219_delivery import world as delivery_world

world = delivery_world


def queue_config(name="rabbitmq"):
    value = {
        "source_type": name,
        "cluster_id": "fixture",
        "credential_id": 1,
        "server": "amqp://broker.invalid:5672",
        "vhost": "/",
        "queue": "events",
    }
    if name == "rocketmq":
        value.update(server="grpc://proxy.invalid:8081", topic="events", group_id="dlr-events")
        del value["vhost"], value["queue"]
    return value


def queue_envelope(world, name="rabbitmq", message_id="message-1", body=b"\x00\xff"):
    attrs = {"cluster_id": "fixture", "properties": {}}
    if name == "rabbitmq":
        attrs.update(vhost="/", queue="events", exchange="", routing_key="events")
    else:
        attrs.update(topic="events", group_id="dlr-events", tag="", keys=[])
    result = {
        "schema": "dlr.event.v1",
        "subscription": {"id": str(world["sub"]), "config_version": 1, "source_epoch": "epoch-1"},
        "received_at": "2026-10-24T00:00:00Z",
        "source": {
            "type": name,
            "identity": "fixture",
            "position": {"message_id": message_id},
            "attributes": attrs,
        },
        "event": {"id": "", "kind": "message", "object_key": None},
        "data": {
            "encoding": "base64",
            "content_type": "application/octet-stream",
            "value": base64.b64encode(body).decode(),
        },
        "sync": None,
    }
    result["event"]["id"] = event_id(result)
    return result


@pytest.mark.parametrize("name", ["rabbitmq", "rocketmq"])
def test_closed_configuration_and_explicit_capabilities(name):
    parsed = TypeAdapter(SourceConfig).validate_python(queue_config(name))
    definition = source_definition(name)
    assert isinstance(parsed, definition.config_model)
    assert definition.capabilities()["recovery"] == "redelivery"
    assert not definition.skip_unaccepted and not definition.batches
    assert definition.max_shards == 1
    with pytest.raises(ValidationError):
        TypeAdapter(SourceConfig).validate_python({**queue_config(name), "auto_ack": True})


@pytest.mark.parametrize(
    "server",
    [
        "amqp://user:secret@broker:5672",
        "amqp://broker:0",
        "amqp://broker:5672?token=secret",
        "amqp://broker:5672/path",
        "http://broker:5672",
        "amqp://broker:5672\n",
    ],
)
def test_connection_addresses_reject_secrets_and_ambiguous_scope(server):
    with pytest.raises(ValidationError):
        TypeAdapter(SourceConfig).validate_python({**queue_config(), "server": server})


def test_unknown_source_has_no_fallback():
    with pytest.raises(ValidationError):
        TypeAdapter(SourceConfig).validate_python({**queue_config(), "source_type": "mqtt"})
    with pytest.raises(HTTPException) as error:
        source_definition("mqtt")
    assert error.value.detail["code"] == "source_not_available"


def test_legacy_kafka_identity_and_group_key_are_unchanged():
    config = {
        "source_type": "kafka",
        "cluster_id": "cluster",
        "topics": ["events"],
        "group_id": "group",
        "initial_position": "earliest",
    }
    assert configuration_identity(config) == config
    assert (
        source_group_key(config) == hashlib.sha256(canonicalize(["cluster", "group"])).hexdigest()
    )
    e = kafka_envelope({"sub": 1})
    attrs, pos = e["source"]["attributes"], e["source"]["position"]
    expected = [
        "kafka",
        e["source"]["identity"],
        attrs["cluster_id"],
        attrs["topic_incarnation"],
        pos["topic"],
        pos["partition"],
        pos["offset"],
    ]
    assert event_id(e) == hashlib.sha256(canonicalize(expected)).hexdigest()
    validate_envelope(e)


@pytest.mark.parametrize("name", ["rabbitmq", "rocketmq"])
def test_scope_changes_require_new_identity_but_credential_rotation_does_not(name):
    config = queue_config(name)
    original = configuration_identity(config)
    assert original == configuration_identity({**config, "credential_id": 2})
    assert original != configuration_identity({**config, "server": config["server"] + "0"})
    key = "queue" if name == "rabbitmq" else "group_id"
    assert source_group_key(config) != source_group_key({**config, key: "other"})


@pytest.mark.parametrize("name", ["rabbitmq", "rocketmq"])
def test_queue_binary_input_and_metadata_are_closed(name):
    e = queue_envelope({"sub": 1}, name)
    original_hash, _ = validate_envelope(e)
    assert base64.b64decode(e["data"]["value"]) == b"\x00\xff"
    retry = deepcopy(e)
    retry["received_at"] = "2026-10-25T00:00:00Z"
    retry["subscription"]["config_version"] = 10
    assert validate_envelope(retry)[0] == original_hash
    retry["source"]["attributes"]["receipt_handle"] = "temporary-handle"
    with pytest.raises(HTTPException):
        validate_envelope(retry)
    huge = queue_envelope({"sub": 1}, name, body=b"x" * (512 * 1024))
    with pytest.raises(HTTPException) as error:
        validate_envelope(huge)
    assert error.value.detail["code"] == "event_payload_too_large"


@pytest.mark.parametrize("name", ["rabbitmq", "rocketmq"])
def test_queue_t1_rollback_redelivery_and_content_conflict(world, session_factory, name):
    with session_factory.begin() as session:
        session.get(EventSubscription, world["sub"]).source_type = name
    e = queue_envelope(world, name)

    def receive(session, value):
        return delivery.receive_event(
            session,
            world["sub"],
            "owner-a",
            world["generation"],
            value,
            "messages",
            {"message_id": "message-1"},
        )

    with session_factory() as session:
        receive(session, e)
        session.rollback()
        assert session.scalar(select(EventReceipt.id)) is None
        receipt, duplicate = receive(session, e)
        assert not duplicate
        original_id = receipt.id
        session.commit()
        retry = deepcopy(e)
        retry["received_at"] = "2026-10-25T00:00:00Z"
        receipt, duplicate = receive(session, retry)
        assert duplicate and receipt.id == original_id
        assert receipt.envelope["received_at"] == e["received_at"]
        session.commit()
        changed = queue_envelope(world, name, body=b"changed")
        with pytest.raises(HTTPException) as error:
            receive(session, changed)
        assert error.value.detail["code"] == "event_identity_conflict"


def test_capabilities_only_describe_available_sources(api_client, monkeypatch):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    result = api_client.get("/api/event-subscriptions/capabilities").json()
    assert {x["type"] for x in result["sources"]} == set(result["available_sources"])
    assert all(x["type"] in SOURCES for x in result["sources"])


@pytest.mark.parametrize("name", ["rabbitmq", "rocketmq"])
def test_queue_management_keeps_epoch_and_clone_scope_explicit(
    api_client, session_factory, monkeypatch, name
):
    from dlr.control.models import Credential
    from dlr.control.services.secrets import encrypt_fields
    from test_adapters import create_adapter

    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    with session_factory.begin() as session:
        credential = Credential(
            name="source-test",
            type="password",
            ciphertext=encrypt_fields({"username": "fixture", "password": "unit-only"}),
        )
        session.add(credential)
        session.flush()
        credential_id = credential.id
    adapter = create_adapter(api_client, name="queue-management")
    config = queue_config(name)
    config["credential_id"] = credential_id if name == "rabbitmq" else None
    response = api_client.post(
        "/api/event-subscriptions", json={"adapter_id": adapter["id"], "config": config}
    )
    assert response.status_code == 201
    sub = response.json()
    assert sub["available"] is True and sub["desired_state"] == "disabled"
    assert sub["activation_id"] is None
    endpoint = f"/api/event-subscriptions/{sub['id']}"
    changed = {
        **config,
        "server": config["server"]
        .replace("broker.invalid", "new.invalid")
        .replace("proxy.invalid", "new.invalid"),
    }
    update = api_client.patch(endpoint, json={"config": changed, "expected_config_version": 1})
    assert update.status_code == 409 and update.json()["detail"]["code"] == "source_epoch_required"
    updated = api_client.patch(
        endpoint, json={"config": changed, "expected_config_version": 1, "new_epoch": True}
    )
    assert updated.status_code == 200 and updated.json()["source_epoch"] != sub["source_epoch"]
    target = create_adapter(api_client, name="queue-clone-target")
    clone = api_client.post(
        endpoint + "/clone", json={"target_adapter_id": target["id"], "expected_config_version": 2}
    )
    assert clone.status_code == 201
    copy = clone.json()
    key = "queue" if name == "rabbitmq" else "group_id"
    assert copy["config"][key] != config[key]
    assert copy["desired_state"] == "disabled" and copy["activation_id"] is None
