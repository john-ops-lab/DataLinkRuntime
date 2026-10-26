"""Closed opt-in modes and safe per-member DTO declarations; no live runtime."""

import pytest
from pydantic import TypeAdapter, ValidationError

from dlr.control.schemas.subscription import (
    EventResponse,
    HttpSourceConfig,
    RabbitMQSourceConfig,
    SourceConfig,
)
from dlr.control.services.subscription_registry import SOURCES


def rabbit(**changes):
    return {
        "source_type": "rabbitmq",
        "cluster_id": "fixture",
        "server": "amqp://broker.invalid:5672",
        "vhost": "/",
        "queue": "events",
        "credential_id": 1,
        **changes,
    }


def test_only_http_and_rabbitmq_expose_explicit_business_batch_mode():
    assert HttpSourceConfig(credential_id=1).mode == "per_event"
    assert RabbitMQSourceConfig(**rabbit()).mode == "per_event"
    for value in ({"source_type": "http", "credential_id": 1}, rabbit()):
        assert (
            TypeAdapter(SourceConfig).validate_python({**value, "mode": "business_batch"}).mode
            == "business_batch"
        )
    assert (
        SOURCES["http"].processing_modes
        == SOURCES["rabbitmq"].processing_modes
        == ("per_event", "business_batch")
    )
    assert SOURCES["kubernetes"].processing_modes == ("per_event", "state_sync")
    for name in ("kafka", "rocketmq"):
        assert SOURCES[name].processing_modes == ("per_event",)
    # This older flag refers to Kubernetes's full-sync lifecycle protocol.
    assert SOURCES["kubernetes"].batches is True
    assert SOURCES["http"].batches is SOURCES["rabbitmq"].batches is False


@pytest.mark.parametrize(
    "config",
    [
        {
            "source_type": "kafka",
            "cluster_id": "fixture",
            "bootstrap_servers": ["broker:9092"],
            "topics": ["events"],
            "initial_position": "earliest",
        },
        {
            "source_type": "kubernetes",
            "cluster_id": "fixture",
            "server": "https://cluster.invalid",
            "group": "",
            "version": "v1",
            "resource": "pods",
        },
        {
            "source_type": "rocketmq",
            "cluster_id": "fixture",
            "server": "grpc://proxy.invalid:8081",
            "topic": "events",
            "group_id": "consumer",
        },
    ],
)
def test_business_batch_is_rejected_for_other_sources(config):
    with pytest.raises(ValidationError):
        TypeAdapter(SourceConfig).validate_python({**config, "mode": "business_batch"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_member_index", 100),
        ("batch_member_index", -1),
        ("batch_member_count", 0),
        ("batch_member_count", 101),
        ("batch_member_outcome", "all_done"),
        ("batch_member_code", "x" * 65),
    ],
)
def test_member_dto_bounds_reject_ambiguous_states(field, value):
    base = {
        "id": 1,
        "source_event_id": "fixed",
        "source_epoch": "epoch",
        "activation_id": 1,
        "state": "failed",
        "shard": "http",
        "sequence": 1,
        "envelope": None,
        "payload_expired": True,
        "first_received_at": "2026-10-26T00:00:00Z",
        "duplicate_count": 0,
        "deliveries": [],
        "processing_mode": "business_batch",
    }
    with pytest.raises(ValidationError):
        EventResponse.model_validate({**base, field: value})
