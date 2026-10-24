"""Closed v1 event input and deterministic source identities (never normalize NUL)."""

import base64
import hashlib
import json
from typing import Any

import rfc8785

from dlr.common.config import settings
from dlr.control.services.adapter import domain_error
from dlr.control.services.subscription_registry import SOURCES, source_definition

FIELDS = {"schema", "subscription", "event", "source", "received_at", "data", "sync"}


def canonical(value: Any) -> bytes:
    try:

        def invalid(item: Any) -> bool:
            if isinstance(item, str):
                return "\x00" in item or any(0xD800 <= ord(char) <= 0xDFFF for char in item)
            if isinstance(item, dict):
                return any(invalid(key) or invalid(child) for key, child in item.items())
            if isinstance(item, list):
                return any(invalid(child) for child in item)
            return False

        if invalid(value):
            raise ValueError("unsupported JSON string")
        encoded = rfc8785.dumps(value)
        return encoded
    except (ValueError, TypeError, UnicodeError) as error:
        raise domain_error(
            422, "event_content_invalid", "Unsupported canonical event JSON"
        ) from error


def event_id(envelope: dict[str, Any]) -> str:
    source = envelope["source"]
    attrs, position = source["attributes"], source["position"]
    definition = source_definition(source["type"])
    if definition.identity_contract == "message-id-v1":
        identity = [
            source["type"],
            source["identity"],
            attrs["cluster_id"],
            *[attrs[key] for key in definition.scope_fields],
            position["message_id"],
        ]
        return hashlib.sha256(canonical(identity)).hexdigest()
    if source["type"] == "kafka":
        identity = [
            "kafka",
            source["identity"],
            attrs["cluster_id"],
            attrs["topic_incarnation"],
            position["topic"],
            position["partition"],
            position["offset"],
        ]
    elif envelope["event"]["kind"].startswith("sync."):
        identity = [
            "kubernetes-control",
            source["identity"],
            attrs["cluster_id"],
            envelope["sync"]["batch_id"],
            envelope["event"]["kind"],
        ]
    else:
        identity = [
            "kubernetes",
            source["identity"],
            attrs["cluster_id"],
            attrs["group"],
            attrs["version"],
            attrs["resource"],
            attrs["namespace"],
            attrs["uid"],
            attrs["watch_type"],
            position["resource_version"],
            envelope["event"]["kind"],
            (
                envelope["sync"]["batch_id"]
                if envelope["sync"] is not None
                and (attrs["watch_type"] == "LIST" or envelope["event"]["kind"].startswith("sync."))
                else None
            ),
        ]
    return hashlib.sha256(canonical(identity)).hexdigest()


def validate_envelope(envelope: dict[str, Any]) -> tuple[str, int]:
    """Return stable content hash and complete inline size, independent of receipt metadata."""
    try:
        if set(envelope) != FIELDS or envelope["schema"] != "dlr.event.v1":
            raise ValueError("schema")
        if set(envelope["subscription"]) != {"id", "config_version", "source_epoch"}:
            raise ValueError("subscription")
        if set(envelope["event"]) != {"id", "kind", "object_key"}:
            raise ValueError("event")
        source = envelope["source"]
        if set(source) != {"type", "identity", "position", "attributes"}:
            raise ValueError("source")
        if source["type"] not in SOURCES or not source["identity"]:
            raise ValueError("source type")
        if set(envelope["data"]) != {"encoding", "content_type", "value"}:
            raise ValueError("data")
        if source_definition(source["type"]).identity_contract == "message-id-v1":
            validate_queue_content(envelope)
        elif source["type"] == "kafka":
            attrs = source["attributes"]
            if set(source["position"]) != {"topic", "partition", "offset"}:
                raise ValueError("position")
            for key in ("partition", "offset"):
                value = source["position"][key]
                if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                    raise ValueError("integer position must be decimal string")
            if set(attrs) != {
                "cluster_id",
                "topic_incarnation",
                "timestamp_ms",
                "timestamp_type",
                "key_base64",
                "headers",
            }:
                raise ValueError("attributes")
            if not attrs["cluster_id"] or not attrs["topic_incarnation"]:
                raise ValueError("identity")
            if envelope["data"]["encoding"] != "base64" or envelope["sync"] is not None:
                raise ValueError("encoding")
            if (
                envelope["event"]["kind"] != "message"
                or envelope["event"]["object_key"] is not None
            ):
                raise ValueError("kind")
            values = [attrs["key_base64"], envelope["data"]["value"]]
            header_bytes = 0
            for header in attrs["headers"]:
                if set(header) != {"name", "value_base64"}:
                    raise ValueError("header")
                header_bytes += len(header["name"].encode("utf-8"))
                values.append(header["value_base64"])
            raw_size = header_bytes
            for value in values:
                if value is not None:
                    raw_size += len(base64.b64decode(value, validate=True))
            if raw_size > 512 * 1024:
                raise domain_error(422, "event_payload_too_large", "Event exceeds raw byte limit")
        else:
            if envelope["data"]["encoding"] != "json":
                raise ValueError("encoding")
            attrs = source["attributes"]
            if set(attrs) != {
                "cluster_id",
                "group",
                "version",
                "resource",
                "namespace",
                "name",
                "uid",
                "watch_type",
            }:
                raise ValueError("attributes")
            if set(source["position"]) != {"resource_version"}:
                raise ValueError("position")
            kind = envelope["event"]["kind"]
            if kind not in {
                "object.present",
                "object.removed",
                "sync.started",
                "sync.completed",
                "sync.failed",
            }:
                raise ValueError("kind")
            sync = envelope["sync"]
            if sync is not None and set(sync) != {
                "batch_id",
                "scope_fingerprint",
                "phase",
                "read_complete",
                "item_count",
                "failure_code",
            }:
                raise ValueError("sync")
            if sync is not None and (
                not isinstance(sync["read_complete"], bool)
                or isinstance(sync["item_count"], bool)
                or not isinstance(sync["item_count"], int)
                or sync["item_count"] < 0
            ):
                raise ValueError("sync values")
            if kind.startswith("sync."):
                if (
                    sync is None
                    or envelope["event"]["object_key"] is not None
                    or envelope["data"]["value"] is not None
                    or attrs["name"] is not None
                    or attrs["uid"] is not None
                    or attrs["watch_type"] is not None
                ):
                    raise ValueError("control fields")
            elif (
                not attrs["uid"]
                or not attrs["name"]
                or not envelope["event"]["object_key"]
                or attrs["watch_type"] not in {"ADDED", "MODIFIED", "DELETED", "LIST"}
                or not isinstance(source["position"]["resource_version"], str)
            ):
                raise ValueError("object fields")
            if attrs["watch_type"] == "LIST" and sync is None:
                raise ValueError("LIST requires a batch")
        if envelope["event"]["id"] != event_id(envelope):
            raise ValueError("event identity")
        size = len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())
        if size > min(1024 * 1024, settings.execution_input_max_bytes):
            raise domain_error(422, "event_payload_too_large", "Event exceeds inline byte limit")
        stable = {"event": envelope["event"], "source": source, "data": envelope["data"]}
        if source["type"] == "kubernetes" and (
            source["attributes"]["watch_type"] == "LIST"
            or envelope["event"]["kind"].startswith("sync.")
        ):
            stable["sync"] = envelope["sync"]
        return hashlib.sha256(canonical(stable)).hexdigest(), size
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise domain_error(422, "event_content_invalid", "Invalid closed event envelope") from error


def validate_queue_content(envelope: dict[str, Any]) -> None:
    source = envelope["source"]
    definition = source_definition(source["type"])
    attrs = source["attributes"]
    position = source["position"]
    if (
        set(position) != {"message_id"}
        or not isinstance(position["message_id"], str)
        or not 0 < len(position["message_id"].encode()) <= 256
        or any(ord(c) < 32 for c in position["message_id"])
        or set(attrs) != definition.attribute_fields
        or not isinstance(attrs["cluster_id"], str)
        or not attrs["cluster_id"]
        or any(not isinstance(attrs[k], str) or not attrs[k] for k in definition.scope_fields)
        or envelope["event"]
        != {"id": envelope["event"]["id"], "kind": "message", "object_key": None}
        or envelope["sync"] is not None
        or envelope["data"]["encoding"] != "base64"
        or not isinstance(attrs["properties"], dict)
    ):
        raise ValueError("message source contract")
    raw = base64.b64decode(envelope["data"]["value"], validate=True)
    metadata = canonical(attrs)
    if len(raw) + len(metadata) > 512 * 1024 or len(metadata) > 64 * 1024:
        raise domain_error(422, "event_payload_too_large", "Message exceeds raw byte limit")
