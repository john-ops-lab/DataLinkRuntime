"""User-owned gallery persistence and common portable template projection."""

import secrets

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterSchedule, AdapterWebhook, UserTemplate
from dlr.control.schemas.portable import PortablePackage, PortableVariant, TemplateWriteRequest
from dlr.control.schemas.template import (
    TemplateInstantiateRequest,
    TemplateLocalizedText,
    TemplateScenarioDetail,
    TemplateScenarioSummary,
    TemplateVariantResponse,
    TemplateVariantSummary,
)
from dlr.control.security import Principal
from dlr.control.services.adapter import domain_error
from dlr.control.services.import_names import insert_with_available_name
from dlr.control.services.portable_zip import encode_package
from dlr.control.template_catalog import get_template_catalog


def can_manage(row: UserTemplate, principal: Principal | None) -> bool:
    return principal is not None and (
        principal.kind == "superadmin"
        or principal.role == "admin"
        or (principal.user_id is not None and principal.user_id == row.creator_user_id)
    )


def require_row(session: Session, slug: str, *, lock: bool = False) -> UserTemplate:
    row = session.get(UserTemplate, slug, with_for_update=lock)
    if row is None:
        raise domain_error(404, "template_scenario_not_found", "Template not found")
    return row


def localized(value: str) -> TemplateLocalizedText:
    return TemplateLocalizedText.model_validate({"zh-CN": value, "en": value})


def summary(row: UserTemplate, principal: Principal | None = None) -> TemplateScenarioSummary:
    package = PortablePackage.model_validate(row.content)
    return TemplateScenarioSummary(
        slug=row.slug,
        theme_slug=package.category,
        title=localized(package.name),
        summary=localized(package.description),
        vendor="DLR",
        adapter_type=package.adapter_type,
        protocols=[],
        logo_key="custom",
        template_version=str(row.version),
        updated_at=row.updated_at.date(),
        source=row.source,
        can_manage=can_manage(row, principal),
        variants=[TemplateVariantSummary(language=v.language) for v in package.variants],
    )


def list_summaries(session: Session, principal: Principal | None) -> list[TemplateScenarioSummary]:
    """Project only gallery metadata; never load every template's code/attachments."""
    columns = [
        UserTemplate.slug,
        UserTemplate.name,
        UserTemplate.creator_user_id,
        UserTemplate.source,
        UserTemplate.version,
        UserTemplate.updated_at,
    ]
    fields = ("category", "description", "adapter_type")
    rows = session.execute(
        select(
            *columns,
            *(UserTemplate.content[key] for key in fields),
            func.jsonb_path_query_array(
                UserTemplate.content, cast("$.variants[*].language", JSONPATH)
            ),
        ).order_by(UserTemplate.updated_at.desc(), UserTemplate.slug)
    )
    result = []
    for (
        slug,
        name,
        creator,
        source,
        version,
        updated,
        category,
        description,
        adapter_type,
        languages,
    ) in rows:
        managed = principal is not None and (
            principal.kind == "superadmin"
            or principal.role == "admin"
            or (principal.user_id is not None and principal.user_id == creator)
        )
        result.append(
            TemplateScenarioSummary(
                slug=slug,
                theme_slug=category,
                title=localized(name),
                summary=localized(description),
                vendor="DLR",
                adapter_type=adapter_type,
                protocols=[],
                logo_key="custom",
                template_version=str(version),
                updated_at=updated.date(),
                source=source,
                can_manage=managed,
                variants=[TemplateVariantSummary(language=language) for language in languages],
            )
        )
    return result


def detail(session: Session, slug: str, principal: Principal | None) -> TemplateScenarioDetail:
    row = require_row(session, slug)
    return TemplateScenarioDetail(**summary(row, principal).model_dump())


def variant(session: Session, slug: str, language: str) -> TemplateVariantResponse:
    row = require_row(session, slug)
    package = PortablePackage.model_validate(row.content)
    value = next((v for v in package.variants if v.language == language), None)
    if value is None:
        raise domain_error(404, "template_variant_not_found", "Template language not found")
    return TemplateVariantResponse(
        scenario_slug=slug,
        theme_slug=package.category,
        title=localized(package.name),
        adapter_type=package.adapter_type,
        template_version=str(row.version),
        **value.model_dump(),
    )


def normalize_category(package: PortablePackage) -> PortablePackage:
    category = package.category if get_template_catalog().get_theme(package.category) else "other"
    return package.model_copy(update={"category": category})


def write_template(
    session: Session,
    request: TemplateWriteRequest,
    principal: Principal,
    *,
    source: str,
    slug: str | None = None,
) -> UserTemplate:
    package = normalize_category(request.package)
    if package.object_type != "template":
        raise domain_error(422, "portable_object_type", "Expected a template package")
    encode_package(package)
    row = require_row(session, slug, lock=True) if slug else None
    if row and not can_manage(row, principal):
        raise domain_error(
            403, "template_manage_forbidden", "Only the creator or administrator may edit"
        )
    if row and request.expected_version != str(row.version):
        raise domain_error(
            409, "template_version_conflict", "Template changed; refresh before editing"
        )
    names = {
        name
        for item in get_template_catalog().scenarios
        for name in (item.title.zh_cn, item.title.en)
    }

    def occupied(name: str) -> bool:
        return (
            name in names
            or session.scalar(
                select(UserTemplate.slug).where(
                    UserTemplate.name == name, UserTemplate.slug != (slug or "")
                )
            )
            is not None
        )

    automatic_name = source == "imported" and row is None
    if not automatic_name and occupied(package.name):
        raise domain_error(409, "template_name_conflict", "Template name already exists")
    if row:
        row.version += 1
        row.name = package.name
        row.content = package.model_dump(mode="json")
    else:
        row = UserTemplate(
            slug="user-" + secrets.token_hex(12),
            name=package.name,
            creator_user_id=principal.user_id,
            source=source,
            version=1,
            content=package.model_dump(mode="json"),
        )
        if not automatic_name:
            session.add(row)
    try:
        if automatic_name:

            def insert(name: str) -> None:
                row.name = name
                row.content = package.model_copy(update={"name": name}).model_dump(mode="json")
                session.add(row)

            insert_with_available_name(
                session, package.name, occupied, insert, "user_templates_name_key"
            )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            != "user_templates_name_key"
        ):
            raise
        raise domain_error(409, "template_name_conflict", "Template name already exists") from None
    session.refresh(row)
    return row


def delete_template(
    session: Session, slug: str, principal: Principal, expected_version: str
) -> None:
    row = require_row(session, slug, lock=True)
    if not can_manage(row, principal):
        raise domain_error(
            403, "template_manage_forbidden", "Only the creator or administrator may delete"
        )
    if str(row.version) != expected_version:
        raise domain_error(
            409, "template_version_conflict", "Template changed; refresh before deleting"
        )
    session.delete(row)
    session.commit()


def export_template(session: Session, slug: str) -> PortablePackage:
    if slug.startswith("user-"):
        return PortablePackage.model_validate(require_row(session, slug).content)
    catalog = get_template_catalog()
    scenario = catalog.get_scenario(slug)
    if scenario is None:
        raise domain_error(404, "template_scenario_not_found", "Template not found")
    variants = []
    provenance: set[str] = set()
    licenses: set[str] = set()
    for item in scenario.variants:
        loaded = catalog.load_variant(slug, item.language)
        assert loaded is not None
        variants.append(
            PortableVariant(
                language=item.language,
                code=loaded.code,
                requirements=item.requirements,
                runtime_config=dict(item.runtime_config),
            )
        )
        for source in loaded.sources:
            provenance.add(f"{source.url} @ {source.revision}: {source.reference}")
            licenses.add(f"{source.license}: {source.license_evidence}")
    return PortablePackage(
        object_type="template",
        name=scenario.title.zh_cn,
        description=scenario.summary.zh_cn,
        category=scenario.theme_slug,
        adapter_type=scenario.adapter_type,
        variants=variants,
        provenance="\n".join(sorted(provenance)),
        license="\n".join(sorted(licenses)),
    )


def instantiate(
    session: Session,
    slug: str,
    language: str,
    payload: TemplateInstantiateRequest,
    owner_user_id: int | None,
) -> Adapter:
    from dlr.control.services import template as templates

    row = require_row(session, slug, lock=True)
    value = variant(session, slug, language)
    if payload.expected_template_version != str(row.version):
        raise domain_error(
            409, "template_version_conflict", "Template changed; refresh before copying"
        )
    if templates._active_name_conflict(session, payload.name):
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists")
    package = PortablePackage.model_validate(row.content)
    adapter = Adapter(
        name=payload.name,
        description=payload.description if payload.description is not None else package.description,
        language=language,
        adapter_type=value.adapter_type,
        run_mode="manual",
        owner_user_id=owner_user_id,
        timeout_seconds=package.timeout_seconds,
        configuration_notes={
            "review_environment": True,
        },
    )
    session.add(adapter)
    try:
        session.flush()
        templates._add_template_slot(session, adapter.id)
        templates._add_template_type_configuration(session, adapter.id, adapter.adapter_type)
        if package.schedule:
            session.add(
                AdapterSchedule(
                    adapter_id=adapter.id,
                    enabled=False,
                    next_run_at=None,
                    **package.schedule.model_dump(),
                )
            )
        if package.webhook:
            webhook = session.scalar(
                select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter.id)
            )
            assert webhook is not None
            webhook.response_mode = package.webhook.response_mode
            webhook.response_timeout_seconds = package.webhook.response_timeout_seconds
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "adapter_name_conflict", "Adapter name already exists") from None
    except Exception:
        session.rollback()
        raise
    session.refresh(adapter)
    return adapter
