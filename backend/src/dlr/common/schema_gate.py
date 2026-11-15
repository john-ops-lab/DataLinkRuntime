"""PostgreSQL transaction locks for cooperating Alembic and Control processes.

The two-int key is private to this purpose and independent of bigint advisory
locks. Locks are database-scoped, automatically released on commit/rollback or
connection death, and do not coordinate old binaries or manual DDL.
"""

from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

# Fixed namespace/key (ASCII DLRG / schema-v1), never derived from tenant data.
_GATE_NAMESPACE = 0x444C5247
_GATE_KEY = 0x53434831
_MIGRATION_WAIT_SECONDS = 5.0


class SchemaGateUnavailable(Exception):
    """Migration/Control schema coordination is unavailable."""


def try_control_gate(executor: Connection | Session) -> None:
    """Acquire a shared transaction lock before schema reads or business locks."""
    if executor.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise SchemaGateUnavailable("schema gate unavailable")
    acquired = executor.scalar(
        text("SELECT pg_catalog.pg_try_advisory_xact_lock_shared(:namespace, :key)"),
        {"namespace": _GATE_NAMESPACE, "key": _GATE_KEY},
    )
    if acquired is not True:
        raise SchemaGateUnavailable("schema gate unavailable")


def acquire_migration_gate(executor: Connection | Session) -> None:
    """Bound the exclusive wait; retain the lock for the entire migration txn."""
    deadline = time.monotonic() + _MIGRATION_WAIT_SECONDS
    while True:
        acquired = executor.scalar(
            text("SELECT pg_catalog.pg_try_advisory_xact_lock(:namespace, :key)"),
            {"namespace": _GATE_NAMESPACE, "key": _GATE_KEY},
        )
        if acquired is True:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SchemaGateUnavailable("schema gate unavailable")
        time.sleep(min(0.05, remaining))


def offline_lock_sql() -> str:
    """SQL for generated scripts; the script must run in one transaction."""
    return f"SELECT pg_catalog.pg_advisory_xact_lock({_GATE_NAMESPACE}, {_GATE_KEY})"
