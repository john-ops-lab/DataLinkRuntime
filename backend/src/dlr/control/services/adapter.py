"""Domain service for Adapter and AdapterVersion management.

Owns transactions, version numbering and publish semantics. Pointer fields
(``latest_version_id`` / ``published_version_id``) are only ever modified
here, never from public API input.
"""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterVersion, Execution
from dlr.control.schemas.adapter import AdapterCreate, AdapterUpdate, VersionCreate


def domain_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build the stable M1 domain error format (detail object with a code)."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def list_adapters(session: Session) -> list[Adapter]:
    return list(
        session.scalars(
            select(Adapter).order_by(Adapter.updated_at.desc(), Adapter.id.desc())
        ).all()
    )


def get_adapter(session: Session, adapter_id: int) -> Adapter:
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    return adapter


def create_adapter(session: Session, data: AdapterCreate) -> Adapter:
    existing = session.scalar(select(Adapter).where(Adapter.name == data.name))
    if existing is not None:
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
    adapter = Adapter(name=data.name, description=data.description, language=data.language)
    session.add(adapter)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # Lost a race against a concurrent create with the same name.
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    session.refresh(adapter)
    return adapter


def update_adapter(session: Session, adapter_id: int, data: AdapterUpdate) -> Adapter:
    adapter = get_adapter(session, adapter_id)
    if data.name is not None and data.name != adapter.name:
        conflict = session.scalar(
            select(Adapter).where(Adapter.name == data.name, Adapter.id != adapter_id)
        )
        if conflict is not None:
            raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
        adapter.name = data.name
    if data.description is not None:
        adapter.description = data.description
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    session.refresh(adapter)
    return adapter


def delete_adapter(session: Session, adapter_id: int) -> None:
    adapter = get_adapter(session, adapter_id)
    # M2: execution history must survive, so an Adapter with any Execution
    # can no longer be physically deleted.
    if (
        session.scalar(select(Execution.id).where(Execution.adapter_id == adapter_id).limit(1))
        is not None
    ):
        raise domain_error(
            409,
            "adapter_has_executions",
            "Adapter has execution history and cannot be deleted",
        )
    # Clear the version pointers first so the FK checks never block deletion;
    # versions themselves are removed by adapter_versions ON DELETE CASCADE.
    adapter.latest_version_id = None
    adapter.published_version_id = None
    session.delete(adapter)
    session.commit()


def list_versions(session: Session, adapter_id: int) -> list[AdapterVersion]:
    get_adapter(session, adapter_id)
    return list(
        session.scalars(
            select(AdapterVersion)
            .where(AdapterVersion.adapter_id == adapter_id)
            .order_by(AdapterVersion.seq.desc())
        ).all()
    )


def get_version(session: Session, adapter_id: int, version_id: int) -> AdapterVersion:
    """Fetch one version; cross-adapter lookups never leak and return 404."""
    get_adapter(session, adapter_id)
    version = session.get(AdapterVersion, version_id)
    if version is None or version.adapter_id != adapter_id:
        raise domain_error(404, "version_not_found", "Version not found")
    return version


def save_version(session: Session, adapter_id: int, data: VersionCreate) -> AdapterVersion:
    """Save new version: single transaction with a row lock on the Adapter.

    The lock guarantees concurrent saves on the same Adapter cannot produce
    duplicate seq values or leave latest pointing at the wrong version.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    max_seq = session.scalar(
        select(func.max(AdapterVersion.seq)).where(AdapterVersion.adapter_id == adapter_id)
    )
    next_seq = (max_seq or 0) + 1
    version = AdapterVersion(
        adapter_id=adapter_id,
        seq=next_seq,
        code=data.code,
        requirements=data.requirements,
        runtime_config=data.runtime_config,
    )
    session.add(version)
    session.flush()  # assign version.id inside the locked transaction
    adapter.latest_version_id = version.id
    session.commit()
    session.refresh(version)
    return version


def publish_version(session: Session, adapter_id: int, version_id: int) -> Adapter:
    """Point published_version_id at an existing version of this Adapter.

    Publish never creates or mutates versions and never touches latest;
    re-publishing the same version is idempotent.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    version = session.get(AdapterVersion, version_id)
    if version is None or version.adapter_id != adapter_id:
        raise domain_error(404, "version_not_found", "Version not found")
    if adapter.published_version_id != version_id:
        adapter.published_version_id = version_id
    session.commit()
    session.refresh(adapter)
    return adapter
