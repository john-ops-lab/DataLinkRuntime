"""Short PostgreSQL leases for traversing shared storage without open transactions."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.control.services import control_ha_capacity, storage_backends
from dlr.control.services.shared_posix import SharedNamespace


@dataclass(frozen=True)
class Page:
    namespace: SharedNamespace
    owner: UUID
    generation: int
    shard: int
    after_key: str


def claim(session: Session, namespace: SharedNamespace) -> Page | None:
    control_ha_capacity.assert_local_control(session)
    generation = storage_backends.binding_generation(session, namespace)
    owner = uuid4()
    row = None
    if generation is not None:
        row = session.execute(
            text("""
                UPDATE control_storage_maintenance SET owner=:owner,
                    lease_until=pg_catalog.clock_timestamp() + interval '30 seconds'
                WHERE purpose IN (
                    SELECT purpose FROM control_storage_maintenance WHERE purpose=:purpose
                      AND (owner IS NULL OR lease_until <= pg_catalog.clock_timestamp())
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING shard, after_key
            """),
            {"purpose": namespace.purpose, "owner": owner},
        ).one_or_none()
    control_ha_capacity.recheck_local_control(session)
    session.commit()
    if row is None or generation is None:
        return None
    return Page(namespace, owner, generation, row.shard, row.after_key)


def current(session: Session, page: Page) -> bool:
    control_ha_capacity.assert_local_control(session)
    return storage_backends.binding_generation(session, page.namespace) == page.generation and (
        session.scalar(
            text("""
                SELECT owner=:owner AND lease_until > pg_catalog.clock_timestamp()
                FROM control_storage_maintenance WHERE purpose=:purpose
            """),
            {"purpose": page.namespace.purpose, "owner": page.owner},
        )
        is True
    )


def finish(session: Session, page: Page, *, shard: int, after_key: str) -> None:
    if not current(session, page):
        session.rollback()
        return
    session.execute(
        text("""
            UPDATE control_storage_maintenance SET owner=NULL, lease_until=NULL,
                shard=:shard, after_key=:after_key
            WHERE purpose=:purpose AND owner=:owner AND lease_until > pg_catalog.clock_timestamp()
        """),
        {
            "purpose": page.namespace.purpose,
            "owner": page.owner,
            "shard": shard,
            "after_key": after_key,
        },
    )
    control_ha_capacity.recheck_local_control(session)
    session.commit()
