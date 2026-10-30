"""Single-message and opt-in business-batch PostgreSQL reference Adapter.

Save this file through the ordinary Python Adapter version API; configure
psycopg[binary]==3.3.4 and bind TARGET_DSN. The DSN must contain a numeric host
or hostaddr (a hostname may remain in host for TLS identity verification).
This keeps DNS work outside the invocation and lets cancellation bound every
actual connection. No connection or user process survives an invocation.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import random
import time
from datetime import UTC, datetime

MAX_ATTEMPTS = 20
RETRY_SECONDS = 120.0
ATTEMPT_SECONDS = 5.0
INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0
BATCH_SCHEMA = "dlr.event.batch.v1"
BATCH_RESULT_SCHEMA = "dlr.event.batch.result.v1"
MAX_BATCH_MEMBERS = 100
MAX_BATCH_INPUT_BYTES = 256 * 1024
BATCH_LOG_BYTES = 64 * 1024
RESULT_TABLE = "dlr_ingress_results"
ATTEMPT_TABLE = "dlr_ingress_attempts"
_SAFE_REASONS = frozenset(
    {
        "target_attempt_timeout",
        "target_authentication_unverified",
        "invalid_fixture_encoding",
        "invalid_fixture_body",
        "unsupported_fixture_source",
        "invalid_fixture_identity",
        "target_dsn_required",
        "target_numeric_address_required",
        "target_single_endpoint_required",
        "target_requires_synchronous_commit_on",
        "target_identity_content_conflict",
        "target_committed_attempt_budget_exhausted",
        "target_nonretryable_failure",
        "target_attempt_budget_exhausted",
        "target_retry_deadline_exhausted",
        "invalid_batch_input",
        "invalid_batch_result",
    }
)


class ReferenceFailure(Exception):
    """Safe local reason, never a driver message or DSN."""


class AuthenticationUnverified(ReferenceFailure):
    """Password exchange occurred, but libpq cannot prove connection success."""


class AttemptTimedOut(ReferenceFailure):
    def __init__(self, phase):
        super().__init__("target_attempt_timeout")
        self.phase = phase


def _payload(input):
    source_type = input["source"]["type"]
    if source_type == "http":
        item = input["data"]["payload"]
    elif source_type == "rabbitmq":
        if input["data"].get("encoding") != "base64":
            raise ReferenceFailure("invalid_fixture_encoding")
        raw = base64.b64decode(input["data"]["value"], validate=True)
        item = json.loads(raw)
    else:
        raise ReferenceFailure("unsupported_fixture_source")
    if not isinstance(item, dict):
        raise ReferenceFailure("invalid_fixture_body")
    run_id, message_id = item.get("run_id"), item.get("message_id")
    sequence, value = item.get("seq"), item.get("value")
    if (
        not isinstance(run_id, str)
        or not 1 <= len(run_id) <= 80
        or not run_id.isascii()
        or not run_id.isprintable()
        or not isinstance(message_id, str)
        or not 1 <= len(message_id) <= 128
        or not message_id.isascii()
        or not message_id.isprintable()
        or type(sequence) is not int
        or not 1 <= sequence <= 2**31 - 1
        or type(value) is not int
        or not -(2**62) <= value <= 2**62 - 1
    ):
        raise ReferenceFailure("invalid_fixture_identity")
    if (
        source_type == "rabbitmq"
        and input["source"]["position"].get("message_id") != message_id
    ):
        raise ReferenceFailure("invalid_fixture_identity")
    canonical = json.dumps(
        item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    # This is a business-payload hash. HTTP original bytes are verified
    # independently against EventReceipt.source_raw_body, never replaced by it.
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return run_id, message_id, sequence, payload_hash, 2 * value + 1


def _connection_options(dsn, driver):
    if not isinstance(dsn, str) or not dsn.strip():
        raise ReferenceFailure("target_dsn_required")
    options = driver.conninfo.conninfo_to_dict(dsn)
    address = options.get("hostaddr")
    if address is None:
        address = options.get("host")
    try:
        ipaddress.ip_address(address or "")
    except ValueError:
        raise ReferenceFailure("target_numeric_address_required") from None
    options["hostaddr"] = address
    # No multi-host expansion, SRV, service file or async resolver work hidden
    # behind the nominal deadline. TLS host verification is left untouched.
    if "," in options.get("host", "") or options.get("service"):
        raise ReferenceFailure("target_single_endpoint_required")
    options["connect_timeout"] = "5"
    return options


def _retryable(error, driver):
    if isinstance(error, AttemptTimedOut):
        # Connect failure is transient. A timed-out COMMIT is uncertain and
        # must return through the original stable business key.
        return error.phase in {"connect", "commit"}
    state = getattr(error, "sqlstate", None)
    if state:
        return state.startswith("08") or state in {"40001", "40P01"}
    return isinstance(error, driver.OperationalError)


def _safe_failure(error):
    state = getattr(error, "sqlstate", None)
    if (
        not isinstance(state, str)
        or len(state) != 5
        or not state.isascii()
        or not state.isalnum()
    ):
        state = None
    result = {"error_type": type(error).__name__, "sqlstate": state}
    reason = error.args[0] if error.args else None
    if (
        isinstance(error, ReferenceFailure)
        and isinstance(reason, str)
        and reason in _SAFE_REASONS
    ):
        result["reason"] = reason
    return result


async def _connect(driver, options, marks, state):
    marks["target_connect_started_monotonic_ns"] = time.monotonic_ns()
    try:
        connection = await driver.AsyncConnection.connect(**options)
    except driver.OperationalError as error:
        pgconn = getattr(error, "pgconn", None)
        if (
            not error.sqlstate
            and pgconn is not None
            and (pgconn.needs_password or pgconn.used_password)
        ):
            raise AuthenticationUnverified("target_authentication_unverified") from None
        raise
    marks["target_connected_monotonic_ns"] = time.monotonic_ns()
    state["connected_at"] = datetime.now(UTC).isoformat()
    state["connection"] = connection
    return connection


async def _transaction(connection, item, marks, deadline, state):
    run_id, message_id, sequence, payload_hash, normalized = item
    state["phase"] = "query"
    remaining_ms = max(1, int(min(ATTEMPT_SECONDS, deadline - time.monotonic()) * 1000))
    await connection.execute(
        "SELECT set_config('statement_timeout', %s, true)", (str(remaining_ms),)
    )
    cursor = await connection.execute("SHOW synchronous_commit")
    if (await cursor.fetchone())[0] != "on":
        raise ReferenceFailure("target_requires_synchronous_commit_on")
    cursor = await connection.execute(
        "INSERT INTO dlr_ingress_results "
        "(run_id,message_id,seq,payload_sha256,normalized_value) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (run_id,message_id) DO NOTHING RETURNING created_at",
        (run_id, message_id, sequence, payload_hash, normalized),
    )
    inserted = await cursor.fetchone()
    cursor = await connection.execute(
        "SELECT seq,payload_sha256,normalized_value,attempts,created_at "
        "FROM dlr_ingress_results WHERE run_id=%s AND message_id=%s FOR UPDATE",
        (run_id, message_id),
    )
    prior = await cursor.fetchone()
    if prior is None or tuple(prior[:3]) != (
        sequence,
        payload_hash,
        normalized,
    ):
        raise ReferenceFailure("target_identity_content_conflict")
    if prior[3] >= 64:
        raise ReferenceFailure("target_committed_attempt_budget_exhausted")
    cursor = await connection.execute(
        "UPDATE dlr_ingress_results SET attempts=attempts+1 "
        "WHERE run_id=%s AND message_id=%s RETURNING attempts",
        (run_id, message_id),
    )
    ordinal = (await cursor.fetchone())[0]
    outcome = "inserted" if inserted else "duplicate"
    cursor = await connection.execute(
        "INSERT INTO dlr_ingress_attempts "
        "(run_id,message_id,ordinal,outcome,payload_sha256,normalized_value,"
        "handle_started_at,connected_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING recorded_at",
        (
            run_id,
            message_id,
            ordinal,
            outcome,
            payload_hash,
            normalized,
            marks["handle_started_utc"],
            state["connected_at"],
        ),
    )
    target_at = (await cursor.fetchone())[0]
    state["phase"] = "commit"
    marks["target_commit_started_monotonic_ns"] = time.monotonic_ns()
    await connection.commit()
    marks["target_commit_returned_monotonic_ns"] = time.monotonic_ns()
    return {
        "run_id": run_id,
        "message_id": message_id,
        "payload_sha256": payload_hash,
        "normalized_value": normalized,
        "target_outcome": outcome,
        "target_attempt_ordinal": ordinal,
        "target_recorded_utc": target_at.isoformat(),
        "target_created_utc": prior[4].isoformat(),
    }


async def _close(state, marks):
    connection = state.pop("connection", None)
    if connection is not None:
        # libpq close only releases local socket state; no network rollback.
        await connection.close()
        marks["target_closed_monotonic_ns"] = time.monotonic_ns()


async def _batch_write_once(driver, options, item, marks, deadline, state):
    state["phase"] = "connect" if "connection" not in state else "query"
    try:
        if deadline <= time.monotonic():
            raise AttemptTimedOut(state["phase"])
        async with asyncio.timeout(
            min(ATTEMPT_SECONDS, max(0, deadline - time.monotonic()))
        ):
            connection = state.get("connection")
            if connection is None:
                connection = await _connect(driver, options, marks, state)
            return await _transaction(connection, item, marks, deadline, state)
    except TimeoutError:
        raise AttemptTimedOut(state["phase"]) from None
    except BaseException:
        # A failed transaction/connection never carries into another member.
        await _close(state, marks)
        raise


async def _write_once(driver, options, item, marks, deadline):
    state = {}
    try:
        return await _batch_write_once(driver, options, item, marks, deadline, state)
    finally:
        await _close(state, marks)


def _deadline(marks):
    started_ns = marks.get("handle_started_monotonic_ns")
    started = time.monotonic() if started_ns is None else started_ns / 1_000_000_000
    return started + RETRY_SECONDS


def _encode(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hex(value, length):
    return (
        isinstance(value, str)
        and len(value) == length
        and all(c in "0123456789abcdef" for c in value)
    )


def _batch_members(frame):
    try:
        if not isinstance(frame, dict) or set(frame) != {
            "schema",
            "batch_id",
            "manifest_hash",
            "subscription",
            "members",
        }:
            raise ValueError
        members = frame["members"]
        subscription = frame["subscription"]
        if (
            frame["schema"] != BATCH_SCHEMA
            or not _hex(frame["batch_id"], 32)
            or not _hex(frame["manifest_hash"], 64)
        ):
            raise ValueError
        if not isinstance(subscription, dict) or set(subscription) != {
            "id",
            "config_version",
            "source_epoch",
        }:
            raise ValueError
        if (
            not isinstance(subscription["id"], str)
            or not subscription["id"].isascii()
            or not subscription["id"].isdigit()
            or not 1 <= int(subscription["id"]) <= 2**63 - 1
        ):
            raise ValueError
        if (
            type(subscription["config_version"]) is not int
            or not 1 <= subscription["config_version"] <= 2**63 - 1
            or not isinstance(subscription["source_epoch"], str)
            or not 1 <= len(subscription["source_epoch"]) <= 64
        ):
            raise ValueError
        if (
            not isinstance(members, list)
            or not 1 <= len(members) <= MAX_BATCH_MEMBERS
            or len(_encode(frame)) > MAX_BATCH_INPUT_BYTES
        ):
            raise ValueError
        receipts, deliveries = set(), set()
        for index, member in enumerate(members):
            if not isinstance(member, dict) or set(member) != {
                "receipt_id",
                "delivery_id",
                "delivery_ordinal",
                "member_index",
                "input",
            }:
                raise ValueError
            if (
                any(
                    type(member[key]) is not int or not 1 <= member[key] <= 2**63 - 1
                    for key in ("receipt_id", "delivery_id", "delivery_ordinal")
                )
                or type(member["member_index"]) is not int
                or member["member_index"] != index
                or not isinstance(member["input"], dict)
            ):
                raise ValueError
            if member["receipt_id"] in receipts or member["delivery_id"] in deliveries:
                raise ValueError
            receipts.add(member["receipt_id"])
            deliveries.add(member["delivery_id"])
        return members
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
        raise ReferenceFailure("invalid_batch_input") from None


def _member_result(member, status, code=None):
    return {
        key: member[key]
        for key in ("receipt_id", "delivery_id", "delivery_ordinal", "member_index")
    } | {"status": status, "error_code": code}


def _batch_failure(error, uncertain):
    reason = error.args[0] if error.args else None
    state = getattr(error, "sqlstate", None)
    if uncertain:
        return "unknown", "target_commit_unknown"
    if state in {
        "42501",
        "28000",
        "28P01",
    }:
        return "failed", "target_permission_denied"
    if isinstance(
        error, (KeyError, TypeError, ValueError, OverflowError, UnicodeError)
    ):
        return "failed", "target_invalid_data"
    if reason == "target_identity_content_conflict":
        return "failed", "target_identity_conflict"
    if (
        isinstance(reason, str)
        and (
            reason.startswith("invalid_fixture_")
            or reason == "unsupported_fixture_source"
        )
        or isinstance(state, str)
        and state.startswith(("22", "23"))
    ):
        return "failed", "target_invalid_data"
    return "failed", "target_unavailable"


def _log_batch(context, budget, value):
    encoded = _encode(value)
    size = len(encoded) + 1  # Include the emitted line separator in the payload budget.
    if size <= budget[0]:
        context.logger.info(encoded.decode("utf-8"))
        budget[0] -= size


async def _run_batch(context, frame, driver, marks):
    members = _batch_members(frame)
    results, state = [], {}
    budget = [BATCH_LOG_BYTES]
    deadline = _deadline(marks)
    retries = 0
    current = members[0]
    uncertain = False
    options = None
    try:
        if deadline <= time.monotonic():
            raise TimeoutError
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            for current in members:
                attempt, uncertain = 0, False
                state.pop("phase", None)
                try:
                    item = _payload(current["input"])
                    if options is None:
                        options = _connection_options(
                            context.secrets.get("TARGET_DSN"), driver
                        )
                except Exception as error:  # noqa: BLE001 - classify without leaking driver or payload text
                    outcome = _batch_failure(error, False)
                    results.append(_member_result(current, *outcome))
                    _log_batch(
                        context,
                        budget,
                        {
                            "reference_batch_failure": True,
                            "member_index": current["member_index"],
                            **_safe_failure(error),
                        },
                    )
                    break
                while True:
                    if deadline <= time.monotonic():
                        raise TimeoutError
                    attempt += 1
                    try:
                        value = await _batch_write_once(
                            driver, options, item, marks, deadline, state
                        )
                        results.append(_member_result(current, "succeeded"))
                        _log_batch(
                            context,
                            budget,
                            {
                                "reference_batch_committed": True,
                                "receipt_id": current["receipt_id"],
                                "delivery_id": current["delivery_id"],
                                "member_index": current["member_index"],
                                "run_id": value["run_id"],
                                "message_id": value["message_id"],
                                "payload_sha256": value["payload_sha256"],
                                "normalized_value": value["normalized_value"],
                                "commit_returned_monotonic_ns": marks.get(
                                    "target_commit_returned_monotonic_ns"
                                ),
                                "member_attempts": attempt,
                                "extra_retries_used": retries,
                            },
                        )
                        break
                    except Exception as error:  # noqa: BLE001 - safe closed result codes at the target boundary
                        # Only a COMMIT network/timeout failure leaves its effect unknown;
                        # serialization/deadlock responses explicitly reject the transaction.
                        uncertain = uncertain or (
                            state.get("phase") == "commit"
                            and _retryable(error, driver)
                            and getattr(error, "sqlstate", None)
                            not in {"40001", "40P01"}
                        )
                        state.pop("phase", None)
                        await _close(state, marks)
                        _log_batch(
                            context,
                            budget,
                            {
                                "reference_batch_failure": True,
                                "member_index": current["member_index"],
                                "member_attempt": attempt,
                                "extra_retries_used": retries,
                                **_safe_failure(error),
                            },
                        )
                        if not _retryable(error, driver):
                            results.append(
                                _member_result(
                                    current, *_batch_failure(error, uncertain)
                                )
                            )
                            break
                        if retries >= MAX_ATTEMPTS - 1:
                            results.append(
                                _member_result(
                                    current,
                                    "unknown" if uncertain else "failed",
                                    "target_commit_unknown"
                                    if uncertain
                                    else "target_retry_exhausted",
                                )
                            )
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        base = min(
                            MAX_BACKOFF_SECONDS, INITIAL_BACKOFF_SECONDS * 2**retries
                        )
                        await asyncio.sleep(
                            min(remaining, base * random.uniform(0.8, 1.2))
                        )
                        retries += 1
                if results[-1]["status"] != "succeeded":
                    break
    except TimeoutError:
        # Cancellation from the outer invocation deadline is different from
        # external task cancellation, which propagates and produces no result.
        if len(results) < len(members):
            current = members[len(results)]
            results.append(
                _member_result(
                    current,
                    "unknown",
                    "target_commit_unknown"
                    if uncertain or state.get("phase") == "commit"
                    else "execution_timeout",
                )
            )
    finally:
        await _close(state, marks)
    results.extend(
        _member_result(member, "not_run", "predecessor_failed")
        for member in members[len(results) :]
    )
    result = {
        "schema": BATCH_RESULT_SCHEMA,
        "batch_id": frame["batch_id"],
        "manifest_hash": frame["manifest_hash"],
        "members": results,
    }
    if len(_encode(result)) > min(65536, 512 + 512 * len(members)) or any(
        len(_encode(member)) > 512 for member in results
    ):
        raise ReferenceFailure("invalid_batch_result")
    _log_batch(
        context,
        budget,
        {
            "reference_batch_finished": True,
            "batch_id": frame["batch_id"],
            "members": len(members),
            "extra_retries_used": retries,
            "log_bytes_remaining": budget[0],
        },
    )
    return result


async def _run(context, input, driver, marks):
    item = _payload(input)
    options = _connection_options(context.secrets.get("TARGET_DSN"), driver)
    deadline = _deadline(marks)
    failures = []
    async with asyncio.timeout(max(0, deadline - time.monotonic())):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = await _write_once(driver, options, item, marks, deadline)
                result["target_attempt_count"] = attempt
                result["target_failures"] = failures
                return result
            except Exception as error:
                failure = {"attempt": attempt, **_safe_failure(error)}
                failures.append(failure)
                context.logger.info(
                    json.dumps({"reference_target_failure": True, **failure})
                )
                if not _retryable(error, driver):
                    if isinstance(error, ReferenceFailure):
                        raise
                    raise ReferenceFailure("target_nonretryable_failure") from None
                if attempt >= MAX_ATTEMPTS:
                    raise ReferenceFailure("target_attempt_budget_exhausted") from None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReferenceFailure("target_retry_deadline_exhausted") from None
                base_delay = min(
                    MAX_BACKOFF_SECONDS, INITIAL_BACKOFF_SECONDS * 2 ** (attempt - 1)
                )
                delay = min(remaining, base_delay * random.uniform(0.8, 1.2))
                failure["delay_seconds"] = delay
                await asyncio.sleep(delay)
    raise ReferenceFailure("target_retry_deadline_exhausted")


def handle(context, input):
    marks = {
        "handle_started_monotonic_ns": time.monotonic_ns(),
        "handle_started_utc": datetime.now(UTC).isoformat(),
        "driver_import_started_monotonic_ns": time.monotonic_ns(),
    }
    try:
        import psycopg
        import psycopg.conninfo

        marks["driver_import_completed_monotonic_ns"] = time.monotonic_ns()
        batch = isinstance(input, dict) and input.get("schema") == BATCH_SCHEMA
        result = asyncio.run(
            (_run_batch if batch else _run)(context, input, psycopg, marks)
        )
        marks["handle_completed_monotonic_ns"] = time.monotonic_ns()
        if not batch:
            result["timing"] = marks
        return result
    except Exception as error:  # noqa: BLE001 - redact driver details at the boundary
        # Never expose DSN, SQL, parameter values or driver exception messages.
        context.logger.info(
            json.dumps({"reference_target_failure": True, **_safe_failure(error)})
        )
        raise RuntimeError("reference_target_write_failed") from None
