"""Snapshot export and atomic import using existing target-environment gates."""

import base64
import hashlib
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterInputConfig,
    AdapterSchedule,
    AdapterVersion,
    AdapterWebhook,
    ManagedInputArtifact,
)
from dlr.control.schemas.adapter import VersionCreate
from dlr.control.schemas.input_config import AdapterInputConfigUpsert, InputRetention
from dlr.control.schemas.portable import (
    AdapterExportOptions,
    PortableFile,
    PortableImportRequest,
    PortableInput,
    PortablePackage,
    PortableSchedule,
    PortableVariant,
    PortableWebhook,
)
from dlr.control.security import Principal
from dlr.control.services import adapter as adapter_service
from dlr.control.services import adapter_access, input_config, managed_input_upload
from dlr.control.services.adapter import domain_error
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.portable_zip import MAX_EXPANDED_BYTES, encode_package


def export_adapter(
    session: Session, adapter_id: int, principal: Principal, options: AdapterExportOptions
) -> PortablePackage:
    # All configuration writers lock Adapter first, including input and triggers.
    adapter = adapter_access.require_adapter_access(
        session, adapter_id, principal, "owner", for_update=True
    ).adapter
    version = (
        session.get(AdapterVersion, adapter.latest_version_id)
        if adapter.latest_version_id
        else None
    )
    if version is None:
        raise domain_error(
            409, "portable_saved_version_required", "Save the adapter before exporting"
        )
    config = dict(version.runtime_config)
    pending = adapter.configuration_notes.get("required_parameters", [])
    pending = (
        [key for key in pending if isinstance(key, str) and key not in config]
        if isinstance(pending, list)
        else []
    )
    package = PortablePackage(
        object_type="template" if options.as_template else "adapter",
        name=adapter.name,
        description=adapter.description,
        instructions=str(adapter.configuration_notes.get("instructions", "")),
        license=str(adapter.configuration_notes.get("license", "")),
        provenance=str(adapter.configuration_notes.get("provenance", "")),
        adapter_type=adapter.adapter_type,  # type: ignore[arg-type]
        timeout_seconds=adapter.timeout_seconds,
        variants=[
            PortableVariant(
                language=adapter.language,  # type: ignore[arg-type]
                code=version.code,
                requirements=version.requirements,
                runtime_config={} if options.as_template else config,
                required_parameters=list(dict.fromkeys([*config, *pending]))
                if options.as_template
                else pending,
            )
        ],
    )
    schedule = session.scalar(
        select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter_id)
    )
    if schedule:
        package.schedule = PortableSchedule.model_validate(
            {key: getattr(schedule, key) for key in PortableSchedule.model_fields}
        )
    webhook = session.scalar(select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter_id))
    if webhook:
        package.webhook = PortableWebhook(
            response_mode=webhook.response_mode,
            response_timeout_seconds=webhook.response_timeout_seconds,
        )
    if adapter.adapter_type == "task" and not options.as_template:
        current = input_config.input_config_response(
            input_config.get_input_config(session, adapter_id), session=session
        )
        package.input = PortableInput(source_type=current.source_type)
        if current.source_type == "json" and options.include_json:
            package.input.included = True
            package.input.json_value = current.json_value
        if current.source_type == "managed_files" and options.include_files:
            managed_input_upload.require_feature_enabled()
            store = LocalFileArtifactStore()
            total = 0
            package.input.included = True
            for summary in current.artifacts:
                artifact = session.get(ManagedInputArtifact, summary.id, with_for_update=True)
                if (
                    artifact is None
                    or artifact.adapter_id != adapter_id
                    or artifact.status != "READY"
                    or (artifact.expires_at and artifact.expires_at <= datetime.now(UTC))
                ):
                    raise domain_error(
                        409, "portable_input_unavailable", "Input file is unavailable"
                    )
                total += artifact.size_bytes
                if total > MAX_EXPANDED_BYTES:
                    raise domain_error(
                        413, "portable_package_too_large", "DLR package is too large"
                    )
                with store.open(artifact.storage_key) as handle:
                    content = handle.read(artifact.size_bytes + 1)
                if (
                    len(content) != artifact.size_bytes
                    or hashlib.sha256(content).hexdigest() != artifact.sha256
                ):
                    raise domain_error(
                        409, "portable_input_unavailable", "Input file is unavailable"
                    )
                package.input.files.append(
                    PortableFile(
                        filename=artifact.original_filename,
                        content_type=artifact.content_type,
                        data_base64=base64.b64encode(content).decode(),
                    )
                )
    encode_package(package)
    return package


def import_adapter(
    session: Session, request: PortableImportRequest, principal: Principal
) -> Adapter:
    package = request.package
    if package.object_type != "adapter":
        raise domain_error(422, "portable_object_type", "Expected an adapter package")
    encode_package(package)
    variant = package.variants[0]
    if package.adapter_type == "task" and request.runtime_worker_id is None:
        raise domain_error(
            409, "runtime_worker_required", "Select a target Worker before importing"
        )
    if request.runtime_worker_id is not None:
        adapter_service._validate_runtime_worker_assignment(
            session, request.runtime_worker_id, variant.language
        )
    if adapter_service._active_name_conflict(session, package.name):
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
    if package.input.files:
        managed_input_upload.require_feature_enabled()
    adapter = Adapter(
        name=package.name,
        description=package.description,
        language=variant.language,
        adapter_type=package.adapter_type,
        run_mode="manual",
        timeout_seconds=package.timeout_seconds,
        runtime_worker_id=request.runtime_worker_id,
        owner_user_id=principal.user_id if principal.kind == "account" else None,
        configuration_notes={
            "required_parameters": variant.required_parameters,
            "input_required": package.input.source_type if not package.input.included else "none",
            "review_environment": True,
            "instructions": package.instructions,
            "provenance": package.provenance,
            "license": package.license,
        },
    )
    store: LocalFileArtifactStore | None = None
    created_keys: list[str] = []
    session.add(adapter)
    try:
        session.flush()
        session.add(AdapterExecutionSlot(adapter_id=adapter.id, slot_no=0))
        if adapter.adapter_type == "task":
            session.add(AdapterInputConfig(adapter_id=adapter.id))
            if package.schedule:
                session.add(
                    AdapterSchedule(
                        adapter_id=adapter.id,
                        enabled=False,
                        next_run_at=None,
                        **package.schedule.model_dump(),
                    )
                )
        else:
            session.add(
                AdapterWebhook(
                    adapter_id=adapter.id,
                    public_id=secrets.token_hex(8),
                    enabled=False,
                    credential_id=None,
                    **(package.webhook or PortableWebhook()).model_dump(),
                )
            )
        session.flush()
        # Existing services own their commits. SAVEPOINT sessions keep all of those
        # commits inside this outer transaction, including capacity and input binding.
        with Session(bind=session.connection(), join_transaction_mode="create_savepoint") as nested:
            if package.input.included and package.input.source_type == "json":
                input_config.upsert_input_config(
                    nested,
                    adapter.id,
                    AdapterInputConfigUpsert(
                        expected_revision=1,
                        source_type="json",
                        json_value=package.input.json_value,
                    ),
                )
            if package.input.included and package.input.source_type == "managed_files":
                store = LocalFileArtifactStore()
                artifact_ids = []
                for file in package.input.files:
                    state = managed_input_upload.begin_upload(
                        nested,
                        adapter.id,
                        original_filename=file.filename,
                        content_type=file.content_type,
                        created_by_user_id=principal.user_id,
                        actor_kind=principal.kind,
                        store=store,
                    )
                    created_keys.append(state.storage_key)
                    content = base64.b64decode(file.data_base64, validate=True)
                    managed_input_upload.expand_upload_reservation(
                        nested,
                        adapter.id,
                        state.upload_session_id,
                        requested_total_bytes=len(content),
                        store=store,
                    )
                    with store.put_part(state.storage_key) as handle:
                        handle.write(content)
                    store.commit(state.storage_key)
                    artifact = managed_input_upload.consume_upload_reservation(
                        nested,
                        adapter.id,
                        state.upload_session_id,
                        actual_size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        store=store,
                    )
                    artifact_ids.append(artifact.id)
                input_config.upsert_input_config(
                    nested,
                    adapter.id,
                    AdapterInputConfigUpsert(
                        expected_revision=1,
                        source_type="managed_files",
                        artifact_ids=artifact_ids,
                        retention=InputRetention(mode="system_default"),
                    ),
                )
            # Preserve the normal first-save gate; no implicit Worker selection.
            adapter_service.save_version(
                nested,
                adapter.id,
                VersionCreate(
                    code=variant.code,
                    requirements=variant.requirements,
                    runtime_config=variant.runtime_config,
                ),
            )
        session.commit()
        session.refresh(adapter)
        return adapter
    except BaseException as exc:
        session.rollback()
        if store:
            for key in created_keys:
                store.delete_part(key)
                store.delete(key)
        if isinstance(exc, IntegrityError) and adapter_service._active_name_conflict(
            session, package.name
        ):
            raise domain_error(
                409, "adapter_name_conflict", "Adapter name already exists"
            ) from None
        raise
