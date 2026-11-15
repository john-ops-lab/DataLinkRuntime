"""Restricted fixed Python Worker capabilities at acceptance and final Claim."""

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterCredentialBinding, AdapterVersion, Execution
from dlr.control.models.platform import PackageSource
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
        "Remote Worker stage supports standard-library Python tasks with none/JSON input only",
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
    if adapter.language != "python":
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
    if reason is None and session.scalar(
        select(exists().where(AdapterCredentialBinding.adapter_id == adapter.id))
    ):
        reason = "credentials"
    if reason is None and session.scalar(
        select(
            exists().where(
                PackageSource.kind == "pypi",
                PackageSource.is_default.is_(True),
                PackageSource.index_url == "dlr-builtin://pypi",
            )
        )
    ):
        reason = "builtin_packages"
    if reason is not None:
        _reject(reason)


def execution_block_reason(session: Session, adapter: Adapter, execution: Execution) -> str | None:
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
        return "credentials"
    return None
