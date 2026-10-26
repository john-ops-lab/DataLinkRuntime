"""Opt-in isolated-process fault cuts, never a product recovery authority.

Requires all three DLR_INGRESS_FAULT_{ENABLED,SCOPE,ROLE} settings and an
absolute private ARM path. Importing this module does not install hooks or
kill anything. A matching, fsynced, exclusive marker precedes os._exit(137).
"""

from __future__ import annotations

import base64
import contextvars
import functools
import hashlib
import importlib.abc
import json
import os
import re
import stat
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

POINTS = {
    "t1_before_commit": "control",
    "t1_after_commit": "control",
    "outbox_after_confirm": "control",
    "worker_after_claim_journal": "worker",
    "target_after_commit_before_result": "worker",
}
_TARGETS = {
    "sqlalchemy.orm.session",
    "dlr.control.services.subscription_delivery",
    "dlr.control.services.subscription_t1_unit",
    "dlr.control.services.outbox",
    "pika.adapters.blocking_connection",
    "dlr.worker.client",
    "dlr.worker.workspace",
}
_MATCH_KEYS = {
    "subscription_id",
    "run_id",
    "execution_id",
    "attempt_id",
    "receipt_id",
    "message_id",
}
_SAFE_TEXT = re.compile(r"[A-Za-z0-9_.:/-]{1,128}\Z")
_SAFE_ARM = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_PUBLISH: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "ingress_fault_publish", default=None
)
_ACTIVE: Probe | None = None


@dataclass(frozen=True)
class Arm:
    arm_id: str
    point: str
    match: dict[str, Any]
    result_proof_mode: str
    parent_identity: tuple[int, int]


def _private_directory(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise ValueError("private_directory_required")
    return fd


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_arm_field")
        result[key] = value
    return result


def _read_arm(path: Path, role: str) -> Arm:
    directory = _private_directory(path.parent)
    try:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 8192
            ):
                raise ValueError("private_arm_required")
            raw = os.read(fd, 8193)
        finally:
            os.close(fd)
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) - {
            "schema",
            "arm_id",
            "point",
            "role",
            "match",
            "result_proof_mode",
        }:
            raise ValueError("invalid_arm")
        if (
            value.get("schema") != "dlr.ingress-fault-arm/v1"
            or value.get("role") != role
        ):
            raise ValueError("invalid_arm")
        arm_id, point, match = (
            value.get("arm_id"),
            value.get("point"),
            value.get("match"),
        )
        if (
            not isinstance(arm_id, str)
            or not _SAFE_ARM.fullmatch(arm_id)
            or POINTS.get(point) != role
        ):
            raise ValueError("invalid_arm")
        if (
            not isinstance(match, dict)
            or set(match) - _MATCH_KEYS
            or "run_id" not in match
            or not ({"subscription_id", "execution_id"} & set(match))
        ):
            raise ValueError("explicit_identity_required")
        for key, item in match.items():
            if key in {"run_id", "message_id"}:
                if not isinstance(item, str) or not _SAFE_TEXT.fullmatch(item):
                    raise ValueError("invalid_identity")
            elif type(item) is not int or not 1 <= item < 2**63:
                raise ValueError("invalid_identity")
        mode = value.get("result_proof_mode", "reference-single-v1")
        if mode not in {"reference-single-v1", "reference-batch-v1", "external-oracle"}:
            raise ValueError("invalid_result_proof_mode")
        info = os.fstat(directory)
        return Arm(arm_id, point, match, mode, (info.st_dev, info.st_ino))
    finally:
        os.close(directory)


def _loaded(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = object.__getattribute__(value, "__dict__")
        return data if isinstance(data, dict) else {}
    except (AttributeError, TypeError):
        return {}


def _integer(value: Any) -> int | None:
    if type(value) is int and 1 <= value < 2**63:
        return value
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and 1 <= len(value) <= 19
    ):
        parsed = int(value)
        return parsed if 1 <= parsed < 2**63 else None
    return None


def _envelope_ids(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        return {}
    result: dict[str, Any] = {}
    sub_id = _integer(_loaded(envelope.get("subscription")).get("id"))
    if sub_id is not None:
        result["subscription_id"] = sub_id
    data = _loaded(envelope.get("data"))
    item = data.get("payload")
    if item is None and data.get("encoding") == "base64":
        encoded = data.get("value")
        if isinstance(encoded, str) and len(encoded) <= 262144:
            try:
                item = json.loads(base64.b64decode(encoded, validate=True))
            except (ValueError, TypeError, RecursionError):
                item = None
    if isinstance(item, dict):
        for key in ("run_id", "message_id"):
            value = item.get(key)
            if isinstance(value, str) and _SAFE_TEXT.fullmatch(value):
                result[key] = value
    source_type = _loaded(envelope.get("source")).get("type")
    if source_type in {"http", "rabbitmq", "kafka", "kubernetes", "rocketmq"}:
        result["source_type"] = source_type
    return result


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def _input_context(value: Any, arm: Arm | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Bind identities to Control's frozen input, never a result's run/source claim."""
    if not isinstance(value, dict) or value.get("schema") != "dlr.event.batch.v1":
        return _envelope_ids(value), None
    try:
        if set(value) != {"schema", "batch_id", "manifest_hash", "subscription", "members"}:
            return {}, None
        raw = _encoded(value)
        members, subscription = value["members"], value["subscription"]
        if (
            len(raw) > 262144
            or not isinstance(members, list)
            or not 1 <= len(members) <= 100
            or not isinstance(subscription, dict)
            or set(subscription) != {"id", "config_version", "source_epoch"}
            or _integer(subscription["id"]) is None
            or type(subscription["config_version"]) is not int
            or _integer(subscription["config_version"]) is None
            or not isinstance(subscription["source_epoch"], str)
            or not 1 <= len(subscription["source_epoch"]) <= 64
            or not isinstance(value["batch_id"], str)
            or re.fullmatch(r"[a-f0-9]{32}", value["batch_id"]) is None
            or not isinstance(value["manifest_hash"], str)
            or _HASH.fullmatch(value["manifest_hash"]) is None
        ):
            return {}, None
        identity_keys = ("receipt_id", "delivery_id", "delivery_ordinal", "member_index")
        manifest, ids = [], []
        receipts, deliveries, messages = set(), set(), set()
        for index, member in enumerate(members):
            if (
                not isinstance(member, dict)
                or set(member) != {*identity_keys, "input"}
                or any(type(member[key]) is not int or _integer(member[key]) is None for key in identity_keys[:3])
                or type(member["member_index"]) is not int
                or member["member_index"] != index
                or member["receipt_id"] in receipts
                or member["delivery_id"] in deliveries
            ):
                return {}, None
            known = _envelope_ids(member["input"])
            if (
                known.get("subscription_id") != _integer(subscription["id"])
                or not {"run_id", "message_id", "source_type"}.issubset(known)
                or known["source_type"] not in {"http", "rabbitmq"}
                or known["message_id"] in messages
                or ids and any(known[key] != ids[0][key] for key in ("run_id", "source_type"))
            ):
                return {}, None
            receipts.add(member["receipt_id"])
            deliveries.add(member["delivery_id"])
            messages.add(known["message_id"])
            manifest.append({key: member[key] for key in identity_keys})
            ids.append(known)
        requested_message = arm.match.get("message_id") if arm else None
        chosen = next((item for item in ids if item["message_id"] == requested_message), ids[0])
        bound = chosen | {"batch_id": value["batch_id"], "manifest_hash": value["manifest_hash"], "batch_member_count": len(members)}
        contract = {"batch_id": value["batch_id"], "manifest_hash": value["manifest_hash"], "manifest": manifest, "input_sha256": hashlib.sha256(raw).hexdigest()}
        return bound, contract
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError, UnicodeError):
        return {}, None


def _payload_context(payload: Any, arm: Arm) -> tuple[dict[str, Any], dict[str, Any] | None]:
    data = _loaded(payload)
    result, contract = _input_context(data.get("input"), arm)
    for key in ("execution_id", "attempt_id", "attempt_no", "fencing_token"):
        item = _integer(data.get(key))
        if item is not None:
            result[key] = item
    return result, contract


def _reference_batch_result(output: Any, contract: dict[str, Any] | None) -> bool:
    """All-success strict result is still evidence, not an external commit oracle."""
    if contract is None or not isinstance(output, dict):
        return False
    try:
        manifest = contract["manifest"]
        if (
            set(output) != {"schema", "batch_id", "manifest_hash", "members"}
            or output["schema"] != "dlr.event.batch.result.v1"
            or output["batch_id"] != contract["batch_id"]
            or output["manifest_hash"] != contract["manifest_hash"]
            or not isinstance(output["members"], list)
            or len(output["members"]) != len(manifest)
            or len(_encoded(output)) > min(65536, 512 + len(manifest) * 512)
        ):
            return False
        for expected, actual in zip(manifest, output["members"], strict=True):
            if (
                not isinstance(actual, dict)
                or set(actual) != {*expected, "status", "error_code"}
                or any(type(actual[key]) is not int or actual[key] != item for key, item in expected.items())
                or actual["status"] != "succeeded"
                or actual["error_code"] is not None
                or len(_encoded(actual)) > 512
            ):
                return False
        return True
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError, UnicodeError):
        return False


def _matches(arm: Arm, ids: dict[str, Any]) -> bool:
    return all(ids.get(key) == value for key, value in arm.match.items())


class Probe:
    def __init__(self, arm_path: Path, role: str) -> None:
        self.arm_path, self.role = arm_path, role
        self._lock = threading.RLock()
        self._pending: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
            weakref.WeakKeyDictionary()
        )
        self._outboxes: dict[int, dict[str, Any]] = {}
        self._claims: dict[int, dict[str, Any]] = {}
        self._journals: dict[int, dict[str, Any]] = {}
        self.hooks: set[str] = set()

    def arm(self, point: str | None = None) -> Arm | None:
        try:
            arm = _read_arm(self.arm_path, self.role)
            if point is not None and arm.point != point:
                return None
            directory = _private_directory(self.arm_path.parent)
            try:
                try:
                    os.stat(
                        f"fault-{arm.arm_id}.json",
                        dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return arm
                return None  # Never repeat the same armed fault after restart.
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError, RecursionError):
            return None

    def _remember(
        self, table: dict[int, dict[str, Any]], key: int, value: dict[str, Any]
    ) -> None:
        with self._lock:
            if len(table) >= 128 and key not in table:
                table.pop(next(iter(table)))
            table[key] = value

    def _known_ids(self, arm: Arm, ids: dict[str, Any]) -> dict[str, Any]:
        # Explicit opaque/batch mode is deliberately an operator-scoped
        # execution cut, not a claim that an arbitrary output proves a commit.
        if arm.result_proof_mode == "external-oracle" and "execution_id" in arm.match and ids.get(
            "execution_id"
        ) == arm.match.get("execution_id"):
            ids = {**arm.match, **ids}
        return ids

    def fire(
        self,
        arm: Arm,
        ids: dict[str, Any],
        proof: dict[str, Any],
        *,
        diagnostic_receipt: bool = False,
    ) -> bool:
        if (
            not _matches(arm, ids)
            or os.environ.get("DLR_INGRESS_FAULT_ENABLED") != "1"
            or os.environ.get("DLR_INGRESS_FAULT_SCOPE") != "issue222-isolated"
            or os.environ.get("DLR_INGRESS_FAULT_ROLE") != self.role
            or os.environ.get("DLR_INGRESS_FAULT_ARM") != str(self.arm_path)
            or self.arm(arm.point) != arm
        ):
            return False
        directory = None
        try:
            directory = _private_directory(self.arm_path.parent)
            info = os.fstat(directory)
            if (info.st_dev, info.st_ino) != arm.parent_identity:
                return False
            record = {
                "schema": "dlr.ingress-fault-proof/v1",
                "arm_id": arm.arm_id,
                "point": arm.point,
                "role": self.role,
                "pid": os.getpid(),
                "monotonic_ns": time.monotonic_ns(),
                "ids": ids,
                "action": "os._exit(137)",
                "installed_hooks": sorted(self.hooks),
                "proof": proof,
                "instrumentation_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
            }
            if diagnostic_receipt:
                receipt = {
                    "schema": "dlr.ingress-fault-result-evidence/v1",
                    "arm_id": arm.arm_id,
                    "ids": ids,
                    "proof": proof,
                    "monotonic_ns": time.monotonic_ns(),
                    "authority": "evidence_only_not_product_recovery",
                }
                encoded = json.dumps(
                    receipt, sort_keys=True, separators=(",", ":")
                ).encode()
                self._exclusive_write(
                    directory, f"fault-{arm.arm_id}.result-evidence.json", encoded
                )
                record["diagnostic_receipt_sha256"] = hashlib.sha256(
                    encoded + b"\n"  # _exclusive_write persists this final newline too.
                ).hexdigest()
                record["diagnostic_receipt_fsynced"] = True
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            self._exclusive_write(directory, f"fault-{arm.arm_id}.json", encoded)
        except (OSError, ValueError, TypeError):
            return (
                False  # No marker means no counted fault; never break the product call.
            )
        finally:
            if directory is not None:
                os.close(directory)
        os._exit(
            137
        )  # Only this explicitly opted-in process; no external PID is accepted.

    @staticmethod
    def _exclusive_write(directory: int, name: str, encoded: bytes) -> None:
        if len(encoded) > 8192:
            raise ValueError("proof_too_large")
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.write(b"\n")
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory)

    def _wrap(self, owner: Any, name: str, wrapper: Any) -> None:
        original = getattr(owner, name, None)
        if original is None or getattr(original, "_dlr_ingress_fault_wrapped", False):
            return
        wrapped = functools.wraps(original)(wrapper(original))
        wrapped._dlr_ingress_fault_wrapped = True
        setattr(owner, name, wrapped)
        self.hooks.add(f"{getattr(owner, '__name__', type(owner).__name__)}.{name}")

    def patch(self, module: ModuleType) -> None:
        name = module.__name__
        if name == "dlr.control.services.subscription_t1_unit":

            def receive_unit(original: Any) -> Any:
                def call(unit: Any, inputs: Any, *args: Any, **kwargs: Any) -> Any:
                    result = original(unit, inputs, *args, **kwargs)
                    try:
                        arm = self.arm()
                        if arm is None or arm.point not in {"t1_before_commit", "t1_after_commit"}:
                            return result
                        fields = _loaded(unit)
                        source_type = fields.get("source_type")
                        if not isinstance(source_type, str):
                            return result
                        maximum = {"http": 4, "rabbitmq": 16}.get(source_type)
                        if maximum is None:
                            return result
                        outcomes = _loaded(result).get("outcomes")
                        if (
                            not isinstance(inputs, (list, tuple))
                            or not 1 <= len(inputs) <= maximum
                            or not isinstance(outcomes, (list, tuple))
                            or not 1 <= len(outcomes) <= len(inputs)
                        ):
                            return result
                        input_indexes = [_loaded(item).get("index") for item in inputs]
                        outcome_indexes = [_loaded(item).get("index") for item in outcomes]
                        if (
                            any(type(index) is not int for index in input_indexes + outcome_indexes)
                            or len(set(input_indexes)) != len(input_indexes)
                            or len(set(outcome_indexes)) != len(outcome_indexes)
                            or not set(outcome_indexes).issubset(input_indexes)
                        ):
                            return result
                        by_index = {_loaded(item).get("index"): item for item in inputs}
                        new_outcomes = [
                            item for item in outcomes
                            if _loaded(item).get("error") is None
                            and _loaded(item).get("duplicate") is False
                            and _loaded(item).get("status_code") == 202
                            and _integer(_loaded(item).get("receipt_id")) is not None
                        ]
                        for outcome in new_outcomes:
                            item = by_index.get(_loaded(outcome).get("index"))
                            ids = {
                                **_envelope_ids(_loaded(item).get("envelope")),
                                "subscription_id": _loaded(fields.get("subscription")).get("id"),
                                "receipt_id": _integer(_loaded(outcome).get("receipt_id")),
                            }
                            if _matches(arm, ids):
                                with self._lock:
                                    self._pending[fields["session"]] = {
                                        "arm": arm,
                                        "ids": ids,
                                        "reception_path": "T1Unit.receive",
                                        "t1_unit_new_items": len(new_outcomes),
                                        "t1_unit_outcomes": len(outcomes),
                                    }
                                break
                    except Exception:  # noqa: BLE001, S110 - missing instrumentation is not a counted fault
                        pass
                    return result

                return call

            self._wrap(module.T1Unit, "receive", receive_unit)
        elif name == "dlr.control.services.subscription_delivery":

            def receive(original: Any) -> Any:
                def call(
                    session: Any, subscription_id: int, *args: Any, **kwargs: Any
                ) -> Any:
                    result = original(session, subscription_id, *args, **kwargs)
                    try:
                        arm = self.arm()
                        if (
                            arm is not None
                            and arm.point in {"t1_before_commit", "t1_after_commit"}
                            and result[0] is not None
                            and not result[1]
                        ):
                            envelope = (
                                args[2] if len(args) > 2 else kwargs.get("envelope")
                            )
                            ids = {
                                **_envelope_ids(envelope),
                                "subscription_id": subscription_id,
                            }
                            receipt_id = _integer(_loaded(result[0]).get("id"))
                            if receipt_id is not None:
                                ids["receipt_id"] = receipt_id
                            if _matches(arm, ids):
                                with self._lock:
                                    self._pending[session] = {"arm": arm, "ids": ids}
                    except Exception:  # noqa: BLE001, S110 - an absent proof must not change product behavior
                        pass
                    return result

                return call

            self._wrap(module, "receive_event", receive)
        elif name == "sqlalchemy.orm.session":

            def commit(original: Any) -> Any:
                def call(session: Any, *args: Any, **kwargs: Any) -> Any:
                    with self._lock:
                        pending = self._pending.pop(session, None)
                    if pending is not None:
                        arm, ids = pending["arm"], pending["ids"]
                        if (
                            self.arm(arm.point) == arm
                            and arm.point == "t1_before_commit"
                            and session.in_transaction()
                            and not session.in_nested_transaction()
                        ):
                            self.fire(
                                arm,
                                ids,
                                {
                                    "reception_path": pending.get("reception_path", "receive_event"),
                                    "t1_unit_new_items": pending.get("t1_unit_new_items"),
                                    "t1_unit_outcomes": pending.get("t1_unit_outcomes"),
                                    "transaction_committed": False,
                                    "authority": "same_session_before_commit",
                                    "source_ack_or_http_response": False,
                                },
                            )
                    result = original(session, *args, **kwargs)
                    if pending is not None:
                        arm, ids = pending["arm"], pending["ids"]
                        if (
                            self.arm(arm.point) == arm
                            and arm.point == "t1_after_commit"
                        ):
                            self.fire(
                                arm,
                                ids,
                                {
                                    "reception_path": pending.get("reception_path", "receive_event"),
                                    "t1_unit_new_items": pending.get("t1_unit_new_items"),
                                    "t1_unit_outcomes": pending.get("t1_unit_outcomes"),
                                    "transaction_committed": True,
                                    "authority": "same_session_commit_returned_successfully",
                                    "source_ack_or_http_response": False,
                                },
                            )
                    return result

                return call

            def clear(original: Any) -> Any:
                def call(session: Any, *args: Any, **kwargs: Any) -> Any:
                    with self._lock:
                        self._pending.pop(session, None)
                    return original(session, *args, **kwargs)

                return call

            self._wrap(module.Session, "commit", commit)
            self._wrap(module.Session, "rollback", clear)
            self._wrap(module.Session, "close", clear)
        elif name == "dlr.control.services.outbox":

            def create(original: Any) -> Any:
                def call(
                    session: Any, execution: Any, *args: Any, **kwargs: Any
                ) -> Any:
                    row = original(session, execution, *args, **kwargs)
                    try:
                        arm = self.arm("outbox_after_confirm")
                        if arm is None:
                            return row
                        data = _loaded(execution)
                        input_ids, _ = _input_context(data.get("input"), arm)
                        ids = {
                            **input_ids,
                            "execution_id": data.get("id"),
                        }
                        if arm is not None and _matches(arm, ids):
                            self._remember(
                                self._outboxes,
                                ids["execution_id"],
                                {"arm_id": arm.arm_id, "ids": ids},
                            )
                    except Exception:  # noqa: BLE001, S110 - diagnosis only
                        pass
                    return row

                return call

            def publish(original: Any) -> Any:
                def call(connection: Any, row: Any, *args: Any, **kwargs: Any) -> Any:
                    info = self._outboxes.get(_loaded(row).get("execution_id"))
                    arm = self.arm("outbox_after_confirm")
                    state = None
                    if (
                        arm is not None
                        and info is not None
                        and info["arm_id"] == arm.arm_id
                    ):
                        state = {
                            "arm": arm,
                            "ids": info["ids"],
                            "ack_seen": False,
                            "nack_seen": False,
                        }
                    token = _PUBLISH.set(state)
                    try:
                        return original(connection, row, *args, **kwargs)
                    finally:
                        _PUBLISH.reset(token)

                return call

            self._wrap(module, "create_dispatch_outbox", create)
            self._wrap(module, "_publish_row", publish)
        elif name == "pika.adapters.blocking_connection":
            import pika

            if pika.__version__ != "1.3.2":
                return  # The inspected callback/confirm contract is version-specific.

            def callback(original: Any) -> Any:
                def call(callback_result: Any, *args: Any, **kwargs: Any) -> Any:
                    result = original(callback_result, *args, **kwargs)
                    state = _PUBLISH.get()
                    frame = args[0] if args else kwargs.get("method_frame")
                    method = getattr(frame, "method", None)
                    if state is not None and isinstance(method, pika.spec.Basic.Ack):
                        state["ack_seen"] = True
                        state["broker_delivery_tag"] = method.delivery_tag
                    elif state is not None and isinstance(method, pika.spec.Basic.Nack):
                        state["nack_seen"] = True
                    return result

                return call

            def basic_publish(original: Any) -> Any:
                def call(
                    channel: Any,
                    exchange: Any,
                    routing_key: Any,
                    body: Any,
                    properties: Any = None,
                    mandatory: bool = False,
                ) -> Any:
                    result = original(
                        channel,
                        exchange,
                        routing_key,
                        body,
                        properties=properties,
                        mandatory=mandatory,
                    )
                    state = _PUBLISH.get()
                    if (
                        state is not None
                        and state["ack_seen"]
                        and not state["nack_seen"]
                        and getattr(channel, "_delivery_confirmation", False)
                        and mandatory is True
                        and getattr(properties, "delivery_mode", None) == 2
                    ):
                        self.fire(
                            state["arm"],
                            state["ids"],
                            {
                                "authority": "pika_1_3_2_basic_ack_callback_and_basic_publish_return",
                                "broker_basic_ack_observed": True,
                                "broker_delivery_tag": state.get("broker_delivery_tag"),
                                "confirm_mode": True,
                                "persistent": True,
                                "mandatory": True,
                                "outbox_mark_published_called": False,
                                "transaction_committed": False,
                            },
                        )
                    return result

                return call

            self._wrap(module._CallbackResult, "set_value_once", callback)
            self._wrap(module.BlockingChannel, "basic_publish", basic_publish)
        elif name == "dlr.worker.client":

            def claim(original: Any) -> Any:
                def call(client: Any, *args: Any, **kwargs: Any) -> Any:
                    result = original(client, *args, **kwargs)
                    try:
                        arm = self.arm()
                        if arm is not None and result.get("decision") == "EXECUTE":
                            raw_ids, contract = _payload_context(result.get("payload"), arm)
                            ids = self._known_ids(arm, raw_ids)
                            binding = (
                                "explicit_execution_scope_requires_ledger_oracle"
                                if set(arm.match) - set(raw_ids)
                                else "claim_input_batch" if contract is not None else "claim_input"
                            )
                            if _matches(arm, ids) and "attempt_id" in ids:
                                self._remember(
                                    self._claims,
                                    ids["attempt_id"],
                                    {
                                        "arm_id": arm.arm_id,
                                        "ids": ids,
                                        "identity_binding": binding,
                                        "batch_contract": contract,
                                    },
                                )
                    except Exception:  # noqa: BLE001, S110 - diagnosis only
                        pass
                    return result

                return call

            def result_attempt(original: Any) -> Any:
                def call(
                    client: Any,
                    worker_id: int,
                    attempt_id: int,
                    payload: Any,
                    *args: Any,
                    **kwargs: Any,
                ) -> Any:
                    arm = self.arm("target_after_commit_before_result")
                    info = self._claims.get(attempt_id)
                    journal = self._journals.get(attempt_id)
                    if (
                        arm is not None
                        and info is not None
                        and journal is not None
                        and info["arm_id"] == journal["arm_id"] == arm.arm_id
                        and isinstance(payload, dict)
                        and payload.get("status") == "succeeded"
                    ):
                        output = _loaded(payload.get("output"))
                        timing = _loaded(output.get("timing"))
                        digest = output.get("payload_sha256")
                        reference = (
                            arm.result_proof_mode == "reference-single-v1"
                            and payload.get("output_truncated") is not True
                            and info.get("batch_contract") is None
                            and output.get("target_outcome") in {"inserted", "duplicate"}
                            and
                            output.get("run_id") == info["ids"].get("run_id")
                            and output.get("message_id")
                            == info["ids"].get("message_id")
                            and isinstance(digest, str)
                            and _HASH.fullmatch(digest) is not None
                            and type(timing.get("target_commit_returned_monotonic_ns"))
                            is int
                            and timing["target_commit_returned_monotonic_ns"] > 0
                        )
                        batch_reference = arm.result_proof_mode == "reference-batch-v1" and payload.get("output_truncated") is not True and _reference_batch_result(output, info.get("batch_contract"))
                        if reference or batch_reference or arm.result_proof_mode == "external-oracle":
                            self.fire(
                                arm,
                                info["ids"],
                                {
                                    "authority": "evidence_only_not_product_recovery",
                                    "worker_result_authority": "memory_report",
                                    "control_result_rpc_sent": False,
                                    "product_result_journal": False,
                                    "identity_binding": info.get(
                                        "identity_binding", "claim_input"
                                    ),
                                    "target_commit_verified": False,
                                    "independent_target_oracle_required": True,
                                    "adapter_commit_marker_present": bool(reference),
                                    "batch_result_validated": bool(batch_reference),
                                    "batch_input_sha256": info["batch_contract"]["input_sha256"] if info.get("batch_contract") else None,
                                    "batch_result_sha256": hashlib.sha256(_encoded(output)).hexdigest() if batch_reference else None,
                                    "payload_sha256": digest if reference else None,
                                    "journal_file_and_directory_fsynced": True,
                                    "transaction_committed": None,
                                    "result_proof_mode": arm.result_proof_mode,
                                },
                                diagnostic_receipt=True,
                            )
                    return original(
                        client, worker_id, attempt_id, payload, *args, **kwargs
                    )

                return call

            self._wrap(module.ControlClient, "claim_v3", claim)
            self._wrap(module.ControlClient, "result_attempt", result_attempt)
        elif name == "dlr.worker.workspace":

            def journal(original: Any) -> Any:
                def call(root: Any, *args: Any, **kwargs: Any) -> Any:
                    path = original(root, *args, **kwargs)
                    try:
                        arm = self.arm()
                        attempt_id = kwargs.get("attempt_id")
                        info = self._claims.get(attempt_id)
                        if (
                            arm is not None
                            and arm.point
                            in {
                                "worker_after_claim_journal",
                                "target_after_commit_before_result",
                            }
                            and info is not None
                            and info["arm_id"] == arm.arm_id
                            and kwargs.get("execution_id")
                            == info["ids"].get("execution_id")
                        ):
                            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                            try:
                                metadata = os.fstat(fd)
                                if (
                                    not stat.S_ISREG(metadata.st_mode)
                                    or metadata.st_uid != os.geteuid()
                                    or metadata.st_size > 8192
                                ):
                                    raise ValueError("invalid_journal")
                                value = json.loads(os.read(fd, 8193))
                                if any(
                                    value.get(key) != info["ids"][key]
                                    for key in (
                                        "execution_id",
                                        "attempt_id",
                                        "attempt_no",
                                        "fencing_token",
                                    )
                                ):
                                    raise ValueError("journal_identity_conflict")
                                os.fsync(fd)
                            finally:
                                os.close(fd)
                            directory = os.open(
                                Path(path).parent,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            )
                            try:
                                os.fsync(directory)
                            finally:
                                os.close(directory)
                            self._remember(self._journals, attempt_id, info)
                            if arm.point == "worker_after_claim_journal":
                                self.fire(
                                    arm,
                                    info["ids"],
                                    {
                                        "authority": "control_execute_response_and_actual_attempt_journal",
                                        "claim_execute_response": True,
                                        "journal_file_fsynced": True,
                                        "journal_directory_fsynced": True,
                                        "journal_identity_checked": True,
                                        "execution_started": False,
                                        "identity_binding": info.get(
                                            "identity_binding", "claim_input"
                                        ),
                                        "transaction_committed": None,
                                    },
                                )
                    except Exception:  # noqa: BLE001, S110 - missing proof never becomes a claimed cut
                        pass
                    return path

                return call

            self._wrap(module, "write_attempt_journal", journal)


class _Loader(importlib.abc.Loader):
    def __init__(self, original: Any, probe: Probe) -> None:
        self.original, self.probe = original, probe

    def create_module(self, spec: Any) -> Any:
        method = getattr(self.original, "create_module", None)
        return method(spec) if method else None

    def exec_module(self, module: ModuleType) -> None:
        self.original.exec_module(module)
        try:
            self.probe.patch(module)
        except Exception:  # noqa: BLE001, S110 - missing instrumentation is not a product failure
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, probe: Probe) -> None:
        self.probe = probe
        self._finding = threading.local()

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _TARGETS:
            return None
        active = getattr(self._finding, "active", set())
        if fullname in active:
            return None  # Compose with another explicit observer's finder without recursion.
        self._finding.active = active | {fullname}
        try:
            for finder in tuple(sys.meta_path):
                method = getattr(finder, "find_spec", None)
                if finder is self or method is None:
                    continue
                spec = method(fullname, path, target)
                if spec is not None:
                    if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                        spec.loader = _Loader(spec.loader, self.probe)
                    return spec
            return None
        finally:
            self._finding.active = active


def install() -> Probe | None:
    global _ACTIVE
    if (
        os.environ.get("DLR_INGRESS_FAULT_ENABLED") != "1"
        or os.environ.get("DLR_INGRESS_FAULT_SCOPE") != "issue222-isolated"
        or os.environ.get("DLR_INGRESS_FAULT_ROLE") not in {"control", "worker"}
    ):
        return None
    path = Path(os.environ.get("DLR_INGRESS_FAULT_ARM", ""))
    if not path.is_absolute():
        return None
    if _ACTIVE is not None:
        return _ACTIVE
    probe = _ACTIVE = Probe(path, os.environ["DLR_INGRESS_FAULT_ROLE"])
    sys.meta_path.insert(0, _Finder(probe))
    for name in _TARGETS:
        module = sys.modules.get(name)
        if module is not None:
            try:
                probe.patch(module)
            except Exception:  # noqa: BLE001, S110 - count only a proved runtime cut
                pass
    return probe
