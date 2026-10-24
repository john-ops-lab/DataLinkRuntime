"""Closed source contracts; importing the registry never creates SDK clients."""

from dataclasses import dataclass
from importlib import import_module
from typing import Any

from dlr.common.jcs import canonicalize
from dlr.control.schemas.subscription import (
    KafkaSourceConfig,
    KubernetesSourceConfig,
    RabbitMQSourceConfig,
    RocketMQSourceConfig,
    SourceModel,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.subscription_source import SourceRunner


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    config_model: type[SourceModel]
    runner_class: str
    max_shards: int
    recovery: str
    ordering: str
    batches: bool = False
    skip_unaccepted: bool = False
    implemented: bool = True
    identity_contract: str = "message-id-v1"
    scope_fields: tuple[str, ...] = ()
    attribute_fields: frozenset[str] = frozenset()

    def runner(self) -> SourceRunner:
        module, name = self.runner_class.rsplit(".", 1)
        result: SourceRunner = getattr(import_module(module), name)()
        return result

    def capabilities(self) -> dict[str, Any]:
        return {
            "type": self.name,
            "ordering": self.ordering,
            "recovery": self.recovery,
            "batches": self.batches,
            "skip_unaccepted": self.skip_unaccepted,
            "max_shards": self.max_shards,
        }


PREFIX = "dlr.control.services."
SOURCES = {
    "kafka": SourceDefinition(
        "kafka",
        KafkaSourceConfig,
        PREFIX + "subscription_kafka.KafkaSourceRunner",
        16,
        "checkpoint",
        "partition_serial",
        skip_unaccepted=True,
        identity_contract="kafka-v1",
    ),
    "kubernetes": SourceDefinition(
        "kubernetes",
        KubernetesSourceConfig,
        PREFIX + "subscription_kubernetes.KubernetesSourceRunner",
        1,
        "relist",
        "scope_serial",
        batches=True,
        identity_contract="kubernetes-v1",
    ),
    "rabbitmq": SourceDefinition(
        "rabbitmq",
        RabbitMQSourceConfig,
        PREFIX + "subscription_rabbitmq.RabbitMQSourceRunner",
        1,
        "redelivery",
        "receipt_serial",
        scope_fields=("vhost", "queue"),
        attribute_fields=frozenset(
            {"cluster_id", "vhost", "queue", "exchange", "routing_key", "properties"}
        ),
    ),
    "rocketmq": SourceDefinition(
        "rocketmq",
        RocketMQSourceConfig,
        PREFIX + "subscription_rocketmq.RocketMQSourceRunner",
        1,
        "redelivery",
        "receipt_serial",
        scope_fields=("topic", "group_id"),
        attribute_fields=frozenset(
            {"cluster_id", "topic", "group_id", "tag", "keys", "properties"}
        ),
    ),
}


def source_definition(name: str) -> SourceDefinition:
    definition = SOURCES.get(name)
    if definition is None:
        raise domain_error(409, "source_not_available", "Source is not registered")
    return definition


def configuration_identity(
    value: dict[str, Any], verified: dict[str, Any] | None = None
) -> dict[str, Any]:
    definition = source_definition(value["source_type"])
    if definition.identity_contract == "kafka-v1":
        # Keep the deployed Kafka v1 identity byte-compatible.
        return {
            key: value.get(key)
            for key in ("source_type", "cluster_id", "topics", "group_id", "initial_position")
        }
    if definition.batches:
        return {
            "source_type": value["source_type"],
            "mode": value.get("mode"),
            "source_identity": verified,
        }
    return {
        key: value.get(key)
        for key in ("source_type", "cluster_id", "server", "ca_pem", *definition.scope_fields)
    }


def source_group_key(value: dict[str, Any]) -> str | None:
    import hashlib

    name = value["source_type"]
    if name == "kafka":
        key = [value["cluster_id"], value["group_id"]]
    elif name == "rabbitmq":
        key = [name, value["cluster_id"], value["vhost"], value["queue"]]
    elif name == "rocketmq":
        key = [name, value["cluster_id"], value["group_id"]]
    else:
        return None
    return hashlib.sha256(canonicalize(key)).hexdigest()
