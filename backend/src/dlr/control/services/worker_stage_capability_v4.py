"""Restricted dependency-free Worker tasks at acceptance and final Claim."""

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from dlr.common.business_credentials_v4 import BusinessCredentialError
from dlr.common.config import settings
from dlr.common.worker_languages_v4 import valid_capabilities
from dlr.control.models import Adapter, AdapterVersion, Execution, Worker
from dlr.control.models.platform import PackageSource
from dlr.control.services import business_credentials_v4 as business_credentials
from dlr.control.services.adapter import domain_error


def require_trigger(trigger: str) -> None:
    if settings.worker_remote_v4_enabled and trigger not in {"manual", "schedule"}:
        _reject("trigger")


def require_input_source(source_type: str) -> None:
    if settings.worker_remote_v4_enabled and source_type not in {"none", "json"}:
        _reject("input_source")


def _reject(reason: str) -> None:
    raise domain_error(
        409,
        "worker_task_capability_unavailable",
        "Remote Worker stage supports dependency-free Python/JavaScript tasks "
        "with none/JSON input only",
        {"reason": reason},
    )


def _configuration_reason(
    session: Session,
    adapter: Adapter,
    *,
    version_id: int | None,
    trigger: str,
    input_source_type: str,
    dependency_check: bool,
) -> str | None:
    if adapter.adapter_type != "task" or trigger not in {"manual", "schedule"}:
        return "trigger"
    if adapter.language not in {"python", "javascript"}:
        return "language"
    if dependency_check:
        return "dependency_check"
    if input_source_type not in {"none", "json"}:
        return "input_source"
    # Do not load business code or dependency text into the Control parent.
    if (
        session.scalar(
            select(
                exists().where(
                    AdapterVersion.id == version_id,
                    AdapterVersion.adapter_id == adapter.id,
                    AdapterVersion.requirements == "",
                )
            )
        )
        is not True
    ):
        return "requirements"
    return None


def require_new_execution(
    session: Session,
    adapter: Adapter,
    *,
    version_id: int | None,
    trigger: str,
    input_source_type: str,
    dependency_check: bool = False,
    target_worker_id: int | None = None,
) -> None:
    if not settings.worker_remote_v4_enabled:
        return
    reason = _configuration_reason(
        session,
        adapter,
        version_id=version_id,
        trigger=trigger,
        input_source_type=input_source_type,
        dependency_check=dependency_check,
    )
    if reason is None:
        try:
            refs = business_credentials.adapter_bindings(session, adapter.id)
            if refs and not settings.master_key:
                from dlr.control.services.secrets import _require_secret_store

                _require_secret_store()
            business_credentials.check(
                session,
                refs,
                adapter.runtime_worker_id if target_worker_id is None else target_worker_id,
            )
        except BusinessCredentialError as error:
            reason = error.reason
    if reason is None and session.scalar(
        select(
            exists().where(
                PackageSource.kind == ("npm" if adapter.language == "javascript" else "pypi"),
                PackageSource.is_default.is_(True),
                PackageSource.index_url
                == (
                    "dlr-builtin://npm"
                    if adapter.language == "javascript"
                    else "dlr-builtin://pypi"
                ),
            )
        )
    ):
        reason = "builtin_packages"
    if reason is not None:
        _reject(reason)


def execution_block_reason(
    session: Session, adapter: Adapter, execution: Execution, *, lock_credentials: bool = False
) -> str | None:
    """Inspect immutable responsibility; never replace it with current defaults."""
    reason = _configuration_reason(
        session,
        adapter,
        version_id=execution.version_id,
        trigger=execution.trigger,
        input_source_type=execution.input_source_type,
        dependency_check=execution.dependency_check,
    )
    if reason is not None:
        return reason
    unsupported = session.execute(
        select(
            func.coalesce(func.jsonb_typeof(Execution.builtin_package_snapshot), "null") != "null",
            Execution.credential_bindings_snapshot != [],
        ).where(Execution.id == execution.id)
    ).one()
    if unsupported[0]:
        return "builtin_packages"
    if unsupported[1]:
        try:
            business_credentials.check(
                session,
                business_credentials.execution_bindings(session, execution.id),
                execution.target_worker_id_snapshot,
                lock=lock_credentials,
            )
        except BusinessCredentialError as error:
            return error.reason
    return None


def dispatch_blocked(session: Session, execution_id: int) -> bool:
    """Recheck frozen work and target without loading code/input into Relay."""
    if not settings.worker_remote_v4_enabled:
        return False
    row = session.execute(
        select(
            Adapter.language,
            Adapter.adapter_type,
            Execution.trigger,
            Execution.input_source_type,
            Execution.dependency_check,
            AdapterVersion.requirements == "",
            Execution.credential_bindings_snapshot == [],
            func.coalesce(func.jsonb_typeof(Execution.builtin_package_snapshot), "null") == "null",
            Worker.capabilities,
            Execution.target_worker_id_snapshot,
        )
        .select_from(Execution)
        .join(Adapter, Adapter.id == Execution.adapter_id)
        .join(
            AdapterVersion,
            (AdapterVersion.id == Execution.version_id) & (AdapterVersion.adapter_id == Adapter.id),
        )
        .outerjoin(Worker, Worker.id == Execution.target_worker_id_snapshot)
        .where(Execution.id == execution_id)
    ).one_or_none()
    if row is None:
        return True
    (
        language,
        kind,
        trigger,
        source,
        dependency,
        empty,
        no_credentials,
        no_builtin,
        languages,
        target,
    ) = row
    if not no_credentials:
        try:
            business_credentials.check(
                session, business_credentials.execution_bindings(session, execution_id), target
            )
        except BusinessCredentialError:
            return True
    return not (
        language in {"python", "javascript"}
        and kind == "task"
        and trigger in {"manual", "schedule"}
        and source in {"none", "json"}
        and dependency is False
        and empty is True
        and no_builtin is True
        and valid_capabilities(languages)
        and language in languages
    )
