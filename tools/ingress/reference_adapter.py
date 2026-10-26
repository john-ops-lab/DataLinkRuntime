"""Single-message PostgreSQL reference Adapter with explicit bounded retries.

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


async def _write_once(driver, options, item, marks, deadline):
    run_id, message_id, sequence, payload_hash, normalized = item
    connection = None
    phase = "connect"
    marks["target_connect_started_monotonic_ns"] = time.monotonic_ns()
    try:
        # One attempt, including its statements/commit/close, is bounded by
        # both five seconds and the remaining overall retry deadline.
        async with asyncio.timeout(
            min(ATTEMPT_SECONDS, max(0, deadline - time.monotonic()))
        ):
            try:
                connection = await driver.AsyncConnection.connect(**options)
            except driver.OperationalError as error:
                pgconn = getattr(error, "pgconn", None)
                if (
                    not error.sqlstate
                    and pgconn is not None
                    and (pgconn.needs_password or pgconn.used_password)
                ):
                    # libpq startup errors may omit SQLSTATE, even for a wrong
                    # password. Do not guess from localized secret-bearing
                    # exception text or blindly retry an unverified identity.
                    raise AuthenticationUnverified(
                        "target_authentication_unverified"
                    ) from None
                raise
            marks["target_connected_monotonic_ns"] = time.monotonic_ns()
            connected_at = datetime.now(UTC).isoformat()
            phase = "query"
            remaining_ms = max(
                1, int(min(ATTEMPT_SECONDS, deadline - time.monotonic()) * 1000)
            )
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
                    connected_at,
                ),
            )
            target_at = (await cursor.fetchone())[0]
            phase = "commit"
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
    except TimeoutError:
        raise AttemptTimedOut(phase) from None
    finally:
        if connection is not None:
            # AsyncConnection.close only closes local libpq/socket state; it
            # performs no network rollback or blocking resolver work.
            await connection.close()
            marks["target_closed_monotonic_ns"] = time.monotonic_ns()


async def _run(context, input, driver, marks):
    item = _payload(input)
    options = _connection_options(context.secrets.get("TARGET_DSN"), driver)
    deadline = time.monotonic() + RETRY_SECONDS
    failures = []
    async with asyncio.timeout(RETRY_SECONDS):
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
        result = asyncio.run(_run(context, input, psycopg, marks))
        marks["handle_completed_monotonic_ns"] = time.monotonic_ns()
        result["timing"] = marks
        return result
    except Exception as error:  # noqa: BLE001 - redact driver details at the boundary
        # Never expose DSN, SQL, parameter values or driver exception messages.
        context.logger.info(
            json.dumps({"reference_target_failure": True, **_safe_failure(error)})
        )
        raise RuntimeError("reference_target_write_failed") from None
