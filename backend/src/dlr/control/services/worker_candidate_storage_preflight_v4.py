"""Unwired, narrow PostgreSQL candidate-storage posture observation.

This is not cluster readiness or permission to enable v4. It does not prove
other Control versions, keys, clocks, restore/failover, physical fencing or
the absence of privileged database-side code. A caller must fence schema,
role, grant and connection changes and keep the same transaction through the
protected action. Blocked or unknown callers must roll it back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.common.schema_gate import try_control_gate
from dlr.common.schema_version import SCHEMA_REVISION

_EXPECTED_PROSRC = """
        BEGIN
            IF NEW.writer_xid IS DISTINCT FROM pg_catalog.txid_current() THEN
                RAISE EXCEPTION 'v4 writer transaction mismatch' USING ERRCODE = '23514';
            END IF;
            IF TG_TABLE_NAME = 'worker_lease_candidates_v4' AND TG_OP = 'UPDATE' THEN
                IF OLD.retired AND NOT NEW.retired AND
                   (NEW.instance_id IS NOT DISTINCT FROM OLD.instance_id OR
                    NEW.instance_generation <= OLD.instance_generation) THEN
                    RAISE EXCEPTION 'retired Worker generation cannot revive'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END
        """

_EXPECTED_DISPOSITION_PROSRC = """
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.recovery_disposition_id IS NOT NULL THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'v4 recovery disposition immutable' USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.recovery_disposition_id IS NOT NULL THEN
                IF NEW.recovery_disposition_xid IS DISTINCT FROM pg_catalog.txid_current() THEN
                    RAISE EXCEPTION 'v4 recovery transaction mismatch' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END
        """

_EXPECTED_AUDIT_PROSRC = """
        BEGIN
          RAISE EXCEPTION 'Worker maintenance audit is immutable' USING ERRCODE = '23514';
        END """

_TABLES = (
    ("worker_lease_candidates_v4", "worker_candidate_xid_guard_v4", 23),
    ("worker_lease_candidates_v4", "worker_candidate_xid_postguard_v4", 21),
    ("attempt_lease_candidates_v4", "attempt_candidate_xid_guard_v4", 23),
    ("attempt_lease_candidates_v4", "attempt_candidate_xid_postguard_v4", 21),
    ("attempt_result_intents_v4", "result_intent_xid_guard_v4", 23),
    ("attempt_result_intents_v4", "result_intent_xid_postguard_v4", 21),
    ("worker_cleanup_intents_v4", "worker_cleanup_intent_xid_guard_v4", 23),
    ("worker_cleanup_intents_v4", "worker_cleanup_intent_xid_postguard_v4", 21),
    ("worker_maintenance_operations", "recovery_disposition_guard_v4", 23),
    ("worker_maintenance_operations", "recovery_disposition_postguard_v4", 21),
    ("worker_maintenance_operations", "dlr_worker_maintenance_immutable", 27),
)


@dataclass(frozen=True)
class CandidateStorageObservation:
    state: Literal["observed", "blocked", "unknown"]
    code: str


_BLOCKED = CandidateStorageObservation("blocked", "candidate_storage_unavailable")
_UNKNOWN = CandidateStorageObservation("unknown", "candidate_storage_unavailable")
_OBSERVED = CandidateStorageObservation("observed", "candidate_storage_posture_observed")


def inspect_candidate_storage(
    session: Session, *, expected_schema: str
) -> CandidateStorageObservation:
    """Inspect only the current connection and required catalog/role facts."""
    if (
        type(expected_schema) is not str
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expected_schema) is None
    ):
        return _BLOCKED
    try:
        if len(expected_schema.encode("utf-8")) > 63:
            return _BLOCKED
        if session.get_bind().dialect.name != "postgresql":
            return _BLOCKED
        try_control_gate(session)
        connection = (
            session.execute(
                text(
                    """
                SELECT current_user = session_user AS same_role,
                       pg_catalog.current_schemas(true) AS paths,
                       pg_catalog.current_setting('server_version_num')::integer AS version,
                       pg_catalog.current_setting('transaction_isolation') AS isolation,
                       EXISTS (
                           SELECT 1 FROM pg_catalog.pg_roles AS r
                           WHERE (r.rolname = current_user OR
                                  pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
                             AND (r.rolsuper OR
                                  pg_catalog.has_parameter_privilege(
                                      r.oid, 'session_replication_role', 'SET'
                                  ) OR
                                  pg_catalog.has_database_privilege(
                                      r.oid, pg_catalog.current_database(), 'CREATE,TEMPORARY'
                                  ))
                       ) AS unsafe_role
                """
                )
            )
            .mappings()
            .one()
        )
        if (
            connection["same_role"] is not True
            or connection["paths"] != ["pg_catalog", expected_schema]
            or not 160000 <= connection["version"] < 170000
            or connection["isolation"] != "read committed"
            or connection["unsafe_role"] is not False
        ):
            return _BLOCKED
        placement_privileges_safe = session.scalar(
            text("""
                SELECT NOT EXISTS (
                    SELECT 1 FROM (VALUES ('worker_placement_cutover'),
                                          ('worker_capacity_verifiers')) AS owned(name)
                    WHERE NOT pg_catalog.has_table_privilege(current_user, name, 'SELECT')
                       OR pg_catalog.has_table_privilege(
                              current_user, name, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER'
                          )
                       OR pg_catalog.has_any_column_privilege(
                              current_user, name, 'INSERT,UPDATE'
                          )
                ) AND pg_catalog.has_table_privilege(
                    current_user, 'execution_placement_actions', 'INSERT'
                ) AND NOT pg_catalog.has_table_privilege(
                    current_user, 'execution_placement_actions', 'UPDATE,DELETE,TRUNCATE,TRIGGER'
                ) AND NOT pg_catalog.has_any_column_privilege(
                    current_user, 'execution_placement_actions', 'UPDATE'
                )
            """)
        )
        if placement_privileges_safe is not True:
            return _BLOCKED
        # Block conflicting table/trigger DDL through this transaction without
        # blocking ordinary writers. Other DDL and role/grant changes still
        # require a separate maintenance fence.
        preparer = session.get_bind().dialect.identifier_preparer
        schema = preparer.quote_schema(expected_schema)
        for table_name in sorted({item[0] for item in _TABLES}):
            table = preparer.quote(table_name)
            session.execute(text(f"LOCK TABLE {schema}.{table} IN ROW EXCLUSIVE MODE NOWAIT"))
        version_table = (
            session.execute(
                text(
                    """
                SELECT c.oid AS table_oid, c.relkind, c.relrowsecurity,
                       c.relforcerowsecurity, c.relowner AS table_owner,
                       n.oid AS schema_oid, n.nspowner AS schema_owner,
                       (c.oid = pg_catalog.to_regclass('alembic_version')) AS resolves_to_expected,
                       pg_catalog.has_table_privilege(current_user, c.oid, 'SELECT') AS can_select,
                       (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger AS triggers
                        WHERE triggers.tgrelid = c.oid AND NOT triggers.tgisinternal)
                           AS user_trigger_count,
                       (SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite AS rules
                        WHERE rules.ev_class = c.oid) AS rule_count
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE c.oid = pg_catalog.to_regclass(
                    pg_catalog.format('%I.%I', CAST(:schema_name AS text), 'alembic_version')
                )
                """
                ),
                {"schema_name": expected_schema},
            )
            .mappings()
            .one_or_none()
        )
        if version_table is None or (
            version_table["relkind"] != "r"
            or version_table["relrowsecurity"] is not False
            or version_table["relforcerowsecurity"] is not False
            or version_table["resolves_to_expected"] is not True
            or version_table["can_select"] is not True
            or version_table["user_trigger_count"] != 0
            or version_table["rule_count"] != 0
        ):
            return _BLOCKED
        version_unsafe = session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_roles AS r
                    WHERE (r.rolname = current_user OR
                           pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
                      AND (
                          pg_catalog.pg_has_role(r.oid, :table_owner, 'MEMBER') OR
                          pg_catalog.pg_has_role(r.oid, :schema_owner, 'MEMBER') OR
                          pg_catalog.has_schema_privilege(r.oid, :schema_oid, 'CREATE') OR
                          pg_catalog.has_table_privilege(
                              r.oid, :table_oid, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER'
                          )
                      )
                )
                """
            ),
            {
                "table_owner": version_table["table_owner"],
                "schema_owner": version_table["schema_owner"],
                "schema_oid": version_table["schema_oid"],
                "table_oid": version_table["table_oid"],
            },
        )
        if version_unsafe is not False:
            return _BLOCKED
        versions = session.execute(text(f"SELECT version_num FROM {schema}.alembic_version"))
        if versions.scalars().all() != [SCHEMA_REVISION]:
            return _BLOCKED
        for table_name, trigger_name, trigger_type in _TABLES:
            disposition = table_name == "worker_maintenance_operations"
            immutable = trigger_name == "dlr_worker_maintenance_immutable"
            expected_function = (
                "dlr_v4_require_disposition" if disposition else "dlr_v4_require_writer_xid"
            )
            expected_source = _EXPECTED_DISPOSITION_PROSRC if disposition else _EXPECTED_PROSRC
            if immutable:
                expected_function = "dlr_worker_maintenance_immutable"
                expected_source = _EXPECTED_AUDIT_PROSRC
            row = (
                session.execute(
                    text(
                        """
                    SELECT c.oid AS table_oid, c.relkind, c.relrowsecurity,
                           c.relforcerowsecurity, c.relowner AS table_owner,
                           n.oid AS schema_oid, n.nspowner AS schema_owner,
                           t.tgenabled, t.tgtype, t.tgnargs,
                           (t.tgqual IS NULL) AS no_trigger_condition,
                           t.tgisinternal,
                           (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger AS all_triggers
                            WHERE all_triggers.tgrelid = c.oid AND NOT all_triggers.tgisinternal)
                               AS user_trigger_count,
                           (SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite AS rules
                            WHERE rules.ev_class = c.oid) AS rule_count,
                           p.proowner AS function_owner, p.proname, p.prosrc,
                           p.prosecdef, p.provolatile, p.proconfig, p.prokind,
                           pn.nspname AS function_schema, l.lanname,
                           (p.prorettype = 'pg_catalog.trigger'::pg_catalog.regtype)
                               AS returns_trigger,
                           (c.oid = pg_catalog.to_regclass(:table_name)) AS resolves_to_expected,
                           pg_catalog.has_table_privilege(
                               current_user, c.oid, 'SELECT'
                           ) AS can_select,
                           pg_catalog.has_table_privilege(
                               current_user, c.oid, 'INSERT'
                           ) AS can_insert,
                           pg_catalog.has_table_privilege(
                               current_user, c.oid, 'UPDATE'
                           ) AS can_update
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    LEFT JOIN pg_catalog.pg_trigger AS t
                        ON t.tgrelid = c.oid AND t.tgname = :trigger_name
                    LEFT JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid
                    LEFT JOIN pg_catalog.pg_namespace AS pn ON pn.oid = p.pronamespace
                    LEFT JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang
                    WHERE c.oid = pg_catalog.to_regclass(
                        pg_catalog.format(
                            '%I.%I', CAST(:schema_name AS text), CAST(:table_name AS text)
                        )
                    )
                    """
                    ),
                    {
                        "schema_name": expected_schema,
                        "table_name": table_name,
                        "trigger_name": trigger_name,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if row is None or (
                row["relkind"] != "r"
                or row["relrowsecurity"] is not False
                or row["relforcerowsecurity"] is not False
                or row["tgenabled"] != "A"
                or row["tgtype"] != trigger_type
                or row["tgnargs"] != 0
                or row["no_trigger_condition"] is not True
                or row["tgisinternal"] is not False
                or row["user_trigger_count"] != (3 if disposition else 2)
                or row["rule_count"] != 0
                or row["proname"] != expected_function
                or row["function_schema"] != expected_schema
                or row["prosrc"] != expected_source
                or row["prosecdef"] is not False
                or row["provolatile"] != "v"
                or row["prokind"] != "f"
                or row["proconfig"] is not None
                or row["lanname"] != "plpgsql"
                or row["returns_trigger"] is not True
                or row["resolves_to_expected"] is not True
                or row["can_select"] is not True
                or row["can_insert"] is not True
                or row["can_update"] is not True
            ):
                return _BLOCKED
            unsafe = session.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_catalog.pg_roles AS r
                        WHERE (r.rolname = current_user OR
                               pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
                          AND (
                              pg_catalog.pg_has_role(r.oid, :table_owner, 'MEMBER') OR
                              pg_catalog.pg_has_role(r.oid, :schema_owner, 'MEMBER') OR
                              pg_catalog.pg_has_role(r.oid, :function_owner, 'MEMBER') OR
                              pg_catalog.has_schema_privilege(r.oid, :schema_oid, 'CREATE') OR
                              pg_catalog.has_table_privilege(
                                  r.oid, :table_oid, 'DELETE,TRUNCATE,TRIGGER'
                              )
                          )
                    )
                    """
                ),
                {
                    "table_owner": row["table_owner"],
                    "schema_owner": row["schema_owner"],
                    "function_owner": row["function_owner"],
                    "schema_oid": row["schema_oid"],
                    "table_oid": row["table_oid"],
                },
            )
            if unsafe is not False:
                return _BLOCKED
        return _OBSERVED
    except UnicodeError:
        return _BLOCKED
    except Exception:
        return _UNKNOWN
