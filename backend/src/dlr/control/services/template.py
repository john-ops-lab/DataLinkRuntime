"""Read-only Template Gallery queries and atomic Adapter instantiation."""

from __future__ import annotations

import copy
import secrets as stdlib_secrets
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterInputConfig,
    AdapterVersion,
    AdapterWebhook,
)
from dlr.control.schemas.adapter import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from dlr.control.schemas.template import (
    TemplateInstantiateRequest,
    TemplateLocalizedText,
    TemplateScenarioDetail,
    TemplateScenarioListResponse,
    TemplateScenarioSummary,
    TemplateSourceResponse,
    TemplateThemeResponse,
    TemplateVariantResponse,
    TemplateVariantSummary,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.locale import get_system_locale
from dlr.control.template_catalog import TemplateCatalog, get_template_catalog
from dlr.control.template_catalog.models import (
    LocalizedText,
    TemplateProvenanceSource,
    TemplateScenarioAsset,
)

SUPPORTED_LANGUAGES = frozenset({"python", "javascript", "java"})
SUPPORTED_MATURITIES = frozenset(
    {"reference-generated", "syntax-verified", "fixture-verified", "live-verified"}
)
SUPPORTED_ADAPTER_TYPES = frozenset({"task", "webhook"})


def _localized(value: LocalizedText) -> TemplateLocalizedText:
    return TemplateLocalizedText.model_validate(value.model_dump(by_alias=True))


def _source_response(source: TemplateProvenanceSource) -> TemplateSourceResponse:
    return TemplateSourceResponse(
        id=source.id,
        url=source.url,
        revision=source.revision,
        reference=source.reference,
        license=source.license,
        license_evidence=source.license_evidence,
        use_mode=source.use_mode,
        checked_at=source.checked_at,
    )


def _variant_summaries(scenario: TemplateScenarioAsset) -> list[TemplateVariantSummary]:
    variants = {variant.language: variant for variant in scenario.variants}
    return [
        TemplateVariantSummary(language=language, maturity=variants[language].maturity)
        for language in ("python", "javascript", "java")
    ]


def _scenario_summary(scenario: TemplateScenarioAsset) -> TemplateScenarioSummary:
    return TemplateScenarioSummary(
        slug=scenario.slug,
        theme_slug=scenario.theme_slug,
        title=_localized(scenario.title),
        summary=_localized(scenario.summary),
        vendor=scenario.vendor,
        adapter_type=scenario.adapter_type,
        protocols=list(scenario.protocols),
        tags=list(scenario.tags),
        logo_key=scenario.logo_key,
        template_version=scenario.version,
        updated_at=scenario.updated_at,
        variants=_variant_summaries(scenario),
    )


def _catalog_or_default(catalog: TemplateCatalog | None) -> TemplateCatalog:
    return catalog if catalog is not None else get_template_catalog()


def list_template_themes(
    catalog: TemplateCatalog | None = None,
) -> list[TemplateThemeResponse]:
    selected = _catalog_or_default(catalog)
    counts: dict[str, int] = {theme.slug: 0 for theme in selected.themes}
    for scenario in selected.scenarios:
        counts[scenario.theme_slug] += 1
    return [
        TemplateThemeResponse(
            slug=theme.slug,
            name=_localized(theme.name),
            description=_localized(theme.description),
            sort_order=theme.sort_order,
            scenario_count=counts[theme.slug],
        )
        for theme in selected.themes
    ]


def _invalid_filter(field: str, _value: str) -> None:
    raise domain_error(
        422,
        "template_filter_invalid",
        "Template filter is invalid",
        # Do not reflect an arbitrary query value into a structured response.
        {"field": field},
    )


def _require_filter(value: str | None, allowed: frozenset[str], field: str) -> None:
    if value is not None and value not in allowed:
        _invalid_filter(field, value)


def list_template_scenarios(
    *,
    theme: str,
    q: str | None = None,
    vendor: str | None = None,
    adapter_type: str | None = None,
    protocol: str | None = None,
    language: str | None = None,
    maturity: str | None = None,
    page: int = 1,
    page_size: int = 12,
    catalog: TemplateCatalog | None = None,
) -> TemplateScenarioListResponse:
    selected = _catalog_or_default(catalog)
    if selected.get_theme(theme) is None:
        _invalid_filter("theme", theme)
    _require_filter(vendor, selected.vendors, "vendor")
    _require_filter(adapter_type, SUPPORTED_ADAPTER_TYPES, "adapter_type")
    _require_filter(protocol, selected.protocols, "protocol")
    _require_filter(language, SUPPORTED_LANGUAGES, "language")
    _require_filter(maturity, SUPPORTED_MATURITIES, "maturity")

    scenarios = [item for item in selected.scenarios if item.theme_slug == theme]
    if q is not None and (query := q.strip().casefold()):
        scenarios = [
            item
            for item in scenarios
            if query
            in "\n".join(
                (
                    item.title.zh_cn,
                    item.title.en,
                    item.summary.zh_cn,
                    item.summary.en,
                    item.vendor,
                    *item.tags,
                )
            ).casefold()
        ]
    if vendor is not None:
        scenarios = [item for item in scenarios if item.vendor == vendor]
    if adapter_type is not None:
        scenarios = [item for item in scenarios if item.adapter_type == adapter_type]
    if protocol is not None:
        scenarios = [item for item in scenarios if protocol in item.protocols]
    if language is not None:
        scenarios = [
            item for item in scenarios if any(v.language == language for v in item.variants)
        ]
    if maturity is not None:
        if language is None:
            scenarios = [
                item for item in scenarios if any(v.maturity == maturity for v in item.variants)
            ]
        else:
            scenarios = [
                item
                for item in scenarios
                if any(v.language == language and v.maturity == maturity for v in item.variants)
            ]

    # Stable sort: least-significant key first keeps the contract obvious.
    scenarios.sort(key=lambda item: item.slug)
    scenarios.sort(key=lambda item: item.updated_at, reverse=True)
    scenarios.sort(key=lambda item: item.featured_rank)
    total = len(scenarios)
    start = (page - 1) * page_size
    items = scenarios[start : start + page_size]
    return TemplateScenarioListResponse(
        items=[_scenario_summary(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
    )


def _require_scenario(catalog: TemplateCatalog, scenario_slug: str) -> TemplateScenarioAsset:
    scenario = catalog.get_scenario(scenario_slug)
    if scenario is None:
        raise domain_error(404, "template_scenario_not_found", "Template scenario not found")
    return scenario


def get_template_scenario(
    scenario_slug: str, catalog: TemplateCatalog | None = None
) -> TemplateScenarioDetail:
    selected = _catalog_or_default(catalog)
    scenario = _require_scenario(selected, scenario_slug)
    summary = _scenario_summary(scenario)
    return TemplateScenarioDetail(
        **summary.model_dump(),
        details=_localized(scenario.details),
        input_summary=_localized(scenario.input_summary),
        output_summary=_localized(scenario.output_summary),
        risk=_localized(scenario.risk),
        modes=list(scenario.modes),
        sources=[
            _source_response(source) for source in selected.sources_for(scenario.provenance_ids)
        ],
    )


def get_template_variant(
    scenario_slug: str,
    language: str,
    catalog: TemplateCatalog | None = None,
) -> TemplateVariantResponse:
    selected = _catalog_or_default(catalog)
    scenario = _require_scenario(selected, scenario_slug)
    loaded = selected.load_variant(scenario_slug, language)
    if loaded is None:
        raise domain_error(404, "template_variant_not_found", "Template variant not found")
    variant = loaded.variant
    return TemplateVariantResponse(
        scenario_slug=scenario.slug,
        theme_slug=scenario.theme_slug,
        title=_localized(scenario.title),
        language=variant.language,
        adapter_type=scenario.adapter_type,
        template_version=scenario.version,
        behavior_contract_version=variant.behavior_contract_version,
        maturity=variant.maturity,
        code=loaded.code,
        requirements=variant.requirements,
        install_notes=_localized(variant.install_notes),
        input_skeleton=copy.deepcopy(variant.input_skeleton),
        input_contract=copy.deepcopy(variant.input_contract),
        output_contract=copy.deepcopy(variant.output_contract),
        runtime_config=copy.deepcopy(variant.runtime_config),
        runtime_guidance=_localized(variant.runtime_guidance),
        sources=[_source_response(source) for source in loaded.sources],
    )


def _active_name_conflict(session: Session, name: str) -> bool:
    return (
        session.scalar(
            select(Adapter.id).where(Adapter.name == name, Adapter.archived_at.is_(None))
        )
        is not None
    )


def _integrity_constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    value = getattr(diagnostic, "constraint_name", None)
    return str(value) if value else None


def _add_template_slot(session: Session, adapter_id: int) -> None:
    session.add(AdapterExecutionSlot(adapter_id=adapter_id, slot_no=0))
    session.flush()


def _add_template_type_configuration(session: Session, adapter_id: int, adapter_type: str) -> None:
    if adapter_type == "task":
        session.add(AdapterInputConfig(adapter_id=adapter_id))
    else:
        session.add(
            AdapterWebhook(
                adapter_id=adapter_id,
                public_id=stdlib_secrets.token_hex(8),
                enabled=False,
                credential_id=None,
            )
        )
    session.flush()


def _add_template_revision(
    session: Session,
    adapter: Adapter,
    *,
    code: str,
    requirements: str,
    runtime_config: dict[str, object],
) -> AdapterVersion:
    revision = AdapterVersion(
        adapter_id=adapter.id,
        seq=1,
        code=code,
        requirements=requirements,
        runtime_config=copy.deepcopy(runtime_config),
    )
    session.add(revision)
    session.flush()
    adapter.latest_version_id = revision.id
    session.flush()
    return revision


def instantiate_template_adapter(
    session: Session,
    *,
    scenario_slug: str,
    language: str,
    payload: TemplateInstantiateRequest,
    owner_user_id: int | None,
    catalog: TemplateCatalog | None = None,
) -> Adapter:
    """Atomically create a detached stopped Adapter and its first Revision."""
    selected = _catalog_or_default(catalog)
    scenario = _require_scenario(selected, scenario_slug)
    loaded = selected.load_variant(scenario_slug, language)
    if loaded is None:
        raise domain_error(404, "template_variant_not_found", "Template variant not found")
    if payload.expected_template_version != scenario.version:
        raise domain_error(
            409,
            "template_version_conflict",
            "Template version changed; refresh before copying",
            {
                "current_template_version": scenario.version,
            },
        )
    if _active_name_conflict(session, payload.name):
        raise domain_error(
            409,
            "adapter_name_conflict",
            "Adapter name already exists",
        )

    locale = get_system_locale(session)
    template_description = scenario.summary.zh_cn if locale == "zh-CN" else scenario.summary.en
    adapter = Adapter(
        name=payload.name,
        description=(
            payload.description if payload.description is not None else template_description
        ),
        language=loaded.variant.language,
        adapter_type=scenario.adapter_type,
        run_mode="manual",
        timeout_seconds=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        owner_user_id=owner_user_id,
        latest_version_id=None,
        runtime_worker_id=None,
        template_scenario_slug=scenario.slug,
        template_version=scenario.version,
    )
    session.add(adapter)
    try:
        session.flush()
        _add_template_slot(session, adapter.id)
        _add_template_type_configuration(session, adapter.id, adapter.adapter_type)
        _add_template_revision(
            session,
            adapter,
            code=loaded.code,
            requirements=loaded.variant.requirements,
            runtime_config=loaded.variant.runtime_config,
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _integrity_constraint_name(exc) == "uq_adapters_active_name":
            raise domain_error(
                409,
                "adapter_name_conflict",
                "Adapter name already exists",
            ) from None
        raise
    except Exception:
        session.rollback()
        raise
    session.refresh(adapter)
    return adapter


def owner_user_id_for_template_principal(
    *, principal_kind: Literal["superadmin", "account"], user_id: int | None
) -> int | None:
    """Account users and account admins own copies; token admin stays system-owned."""
    if principal_kind == "account":
        if user_id is None:  # pragma: no cover - impossible valid Principal
            raise RuntimeError("account principal has no user id")
        return user_id
    return None
