"""Ordinary reference Adapter source, saved through the normal version API.

Requirements: psycopg[binary]==3.3.4. Bind TARGET_DSN before enabling reception.
One invocation opens one connection and one transaction, without target retries.
"""

import base64
import hashlib
import json
import time
from datetime import UTC, datetime


def handle(context, input):
    started = time.monotonic_ns()
    started_at = datetime.now(UTC).isoformat()
    marks = {"handle_started_utc": started_at, "handle_started_monotonic_ns": started}
    connection = None
    outcome = "failed"
    message_id = None
    raw_hash = None
    try:
        marks["driver_import_started_monotonic_ns"] = time.monotonic_ns()
        import psycopg

        marks["driver_import_completed_monotonic_ns"] = time.monotonic_ns()
        marks["driver_import_completed_utc"] = datetime.now(UTC).isoformat()
        raw = base64.b64decode(input["data"]["value"], validate=True)
        if len(raw) != 2048 or input["data"]["encoding"] != "base64":
            raise ValueError("invalid_fixture_body")
        item = json.loads(raw)
        message_id = item["message_id"]
        if (
            input["source"]["position"]["message_id"] != message_id
            or type(item["value"]) is not int
            or item["value"] != item["seq"]
        ):
            raise ValueError("invalid_fixture_identity")
        run_id, sequence = item["run_id"], item["seq"]
        raw_hash = hashlib.sha256(raw).hexdigest()
        normalized = 2 * item["value"] + 1
        marks["target_connect_started_monotonic_ns"] = time.monotonic_ns()
        marks["target_connect_started_utc"] = datetime.now(UTC).isoformat()
        target_dsn = context.secrets.get("TARGET_DSN")
        if not isinstance(target_dsn, str) or not target_dsn.strip():
            raise ValueError("target_dsn_required")
        connection = psycopg.connect(target_dsn, connect_timeout=5)
        marks["target_connected_monotonic_ns"] = time.monotonic_ns()
        marks["target_connected_utc"] = datetime.now(UTC).isoformat()
        connection.execute("SET LOCAL statement_timeout='5s'")
        if connection.execute("SHOW synchronous_commit").fetchone()[0] != "on":
            raise ValueError("target_requires_synchronous_commit_on")
        inserted = connection.execute(
            "INSERT INTO dlr_ingress_b0_results "
            "(run_id,message_id,seq,raw_sha256,normalized_value) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (run_id,message_id) DO NOTHING RETURNING created_at",
            (run_id, message_id, sequence, raw_hash, normalized),
        ).fetchone()
        prior = connection.execute(
            "SELECT seq,raw_sha256,normalized_value,attempts,created_at "
            "FROM dlr_ingress_b0_results WHERE run_id=%s AND message_id=%s FOR UPDATE",
            (run_id, message_id),
        ).fetchone()
        if prior is None or tuple(prior[:3]) != (sequence, raw_hash, normalized):
            raise ValueError("target_identity_content_conflict")
        if prior[3] >= 64:
            raise ValueError("target_attempt_budget_exhausted")
        ordinal = connection.execute(
            "UPDATE dlr_ingress_b0_results SET attempts=attempts+1 "
            "WHERE run_id=%s AND message_id=%s RETURNING attempts",
            (run_id, message_id),
        ).fetchone()[0]
        outcome = "inserted" if inserted else "duplicate"
        target_at = connection.execute(
            "INSERT INTO dlr_ingress_b0_attempts "
            "(run_id,message_id,ordinal,outcome,raw_sha256,normalized_value,"
            "handle_started_at,connected_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING recorded_at",
            (
                run_id,
                message_id,
                ordinal,
                outcome,
                raw_hash,
                normalized,
                started_at,
                marks["target_connected_utc"],
            ),
        ).fetchone()[0]
        marks["target_commit_started_monotonic_ns"] = time.monotonic_ns()
        marks["target_commit_started_utc"] = datetime.now(UTC).isoformat()
        connection.commit()
        marks["target_commit_returned_monotonic_ns"] = time.monotonic_ns()
        marks["target_commit_returned_utc"] = datetime.now(UTC).isoformat()
        connection.close()
        connection = None
        marks["target_closed_monotonic_ns"] = time.monotonic_ns()
        marks["target_closed_utc"] = datetime.now(UTC).isoformat()
        marks["handle_completed_monotonic_ns"] = time.monotonic_ns()
        marks["handle_completed_utc"] = datetime.now(UTC).isoformat()
        return {
            "run_id": run_id,
            "message_id": message_id,
            "raw_sha256": raw_hash,
            "normalized_value": normalized,
            "target_outcome": outcome,
            "target_attempt_ordinal": ordinal,
            "target_recorded_utc": target_at.isoformat(),
            "target_created_utc": prior[4].isoformat(),
            "timing": marks,
        }
    except Exception as error:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
        # SQL/driver exception text can contain a DSN or a value. Emit safe timing only.
        marks["handle_failed_monotonic_ns"] = time.monotonic_ns()
        marks["handle_failed_utc"] = datetime.now(UTC).isoformat()
        context.logger.info(
            json.dumps(
                {
                    "b0_target_failure": True,
                    "message_id": message_id,
                    "raw_sha256": raw_hash,
                    "error_type": type(error).__name__,
                    "timing": marks,
                }
            )
        )
        raise RuntimeError("b0_target_write_failed") from None
    finally:
        if connection is not None:
            connection.close()
