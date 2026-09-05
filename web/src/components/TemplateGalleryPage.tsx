import { useEffect, useRef, useState, type RefObject } from "react";
import Editor from "@monaco-editor/react";
import type { InputRef } from "antd";
import {
  Alert,
  Button,
  Empty,
  Input,
  Modal,
  Pagination,
  Select,
  Skeleton,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  CopyOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import { ApiError, api } from "../api";
import { currentSystemLocale } from "../i18n";
import type {
  AdapterLanguage,
  LocalizedText,
  TemplateInstantiatePayload,
  TemplateScenarioDetail,
  TemplateScenarioSummary,
  TemplateTheme,
  TemplateVariant,
} from "../types";
import TemplateScenarioLogo from "./TemplateScenarioLogo";

const PAGE_SIZE = 12;
const SEARCH_DELAY_MS = 250;
const LANGUAGES: AdapterLanguage[] = ["python", "javascript", "java"];

export interface TemplateCopyRequest extends TemplateInstantiatePayload {
  scenarioSlug: string;
  language: AdapterLanguage;
  draft: Pick<TemplateVariant, "code" | "requirements" | "runtime_config">;
}

interface TemplateGalleryPageProps {
  scenarioSlug: string | null;
  busy: boolean;
  onOpenScenario: (scenarioSlug: string) => void;
  onBackToGallery: (useBrowserBack: boolean) => void;
  onInstantiate: (request: TemplateCopyRequest) => Promise<boolean>;
}

interface GalleryFilters {
  vendor?: string;
  adapterType?: "task" | "webhook";
  protocol?: string;
  language?: AdapterLanguage;
}

function localized(value: LocalizedText): string {
  return value[currentSystemLocale()];
}

function jsonText(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}

function TemplateCard({
  scenario,
  onOpen,
}: {
  scenario: TemplateScenarioSummary;
  onOpen: (scenarioSlug: string, trigger: HTMLAnchorElement) => void;
}) {
  const { t } = useTranslation("template");
  return (
    <article className="template-card" data-testid={`template-card-${scenario.slug}`}>
      <TemplateScenarioLogo logoKey={scenario.logo_key} />
      <div className="template-card-content">
        <div className="template-card-heading">
          <div>
            <p className="template-card-vendor">{scenario.vendor}</p>
            <h2>{localized(scenario.title)}</h2>
          </div>
          <Tag bordered={false}>{t(`type.${scenario.adapter_type}`)}</Tag>
        </div>
        <p className="template-card-summary">{localized(scenario.summary)}</p>
        <div className="template-card-tags" aria-label={t("card.languageSummary")}>
          {scenario.protocols.slice(0, 2).map((protocol) => (
            <Tag key={protocol}>{protocol}</Tag>
          ))}
          {scenario.variants.filter((variant) => variant.available).map((variant) => (
            <span key={variant.language} className="template-language-dot">
              <span aria-hidden="true" />{t(`language.${variant.language}`)}
            </span>
          ))}
        </div>
        <div className="template-card-footer">
          <a
            className="template-card-link"
            href={`/templates/${encodeURIComponent(scenario.slug)}`}
            onClick={(event) => {
              if (
                event.button === 0 &&
                !event.metaKey &&
                !event.ctrlKey &&
                !event.shiftKey &&
                !event.altKey
              ) {
                event.preventDefault();
                onOpen(scenario.slug, event.currentTarget);
              }
            }}
          >
            {t("card.view")} <span aria-hidden="true">→</span>
          </a>
        </div>
      </div>
    </article>
  );
}

function GalleryList({
  themes,
  themesLoading,
  themesError,
  hidden,
  containerRef,
  onReloadThemes,
  onOpenScenario,
}: {
  themes: TemplateTheme[];
  themesLoading: boolean;
  themesError: boolean;
  hidden: boolean;
  containerRef: RefObject<HTMLElement | null>;
  onReloadThemes: () => void;
  onOpenScenario: (scenarioSlug: string, trigger: HTMLAnchorElement) => void;
}) {
  const { t } = useTranslation("template");
  const [activeTheme, setActiveTheme] = useState("all");
  const [queryInput, setQueryInput] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [filters, setFilters] = useState<GalleryFilters>({});
  const [pageByTheme, setPageByTheme] = useState<Record<string, number>>({});
  const [scenarios, setScenarios] = useState<TemplateScenarioSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState(false);
  const [knownVendors, setKnownVendors] = useState<string[]>([]);
  const [knownProtocols, setKnownProtocols] = useState<string[]>([]);
  const [settledFacetThemeKey, setSettledFacetThemeKey] = useState("");
  const [listReloadGeneration, setListReloadGeneration] = useState(0);
  const requestGeneration = useRef(0);
  const facetRequestGeneration = useRef(0);
  const debouncedQueryRef = useRef("");

  const resolvedActiveTheme = activeTheme === "all" || themes.some((theme) => theme.slug === activeTheme)
    ? activeTheme
    : "all";
  const facetThemeKey = themes.map((theme) => theme.slug).join("\u0000");
  const facetsLoading = facetThemeKey !== "" && settledFacetThemeKey !== facetThemeKey;

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const nextQuery = queryInput.trim();
      if (nextQuery !== debouncedQueryRef.current) {
        debouncedQueryRef.current = nextQuery;
        setListLoading(true);
        setListError(false);
        setDebouncedQuery(nextQuery);
      }
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timeout);
  }, [queryInput]);

  const currentPage = pageByTheme[resolvedActiveTheme] ?? 1;

  useEffect(() => {
    if (!facetThemeKey) return;
    const controller = new AbortController();
    const generation = ++facetRequestGeneration.current;
    void Promise.all(facetThemeKey.split("\u0000").map((theme) => api.listTemplateScenarios(
      { theme, page: 1, page_size: 48 },
      controller.signal,
    ))).then((responses) => {
      if (generation !== facetRequestGeneration.current || controller.signal.aborted) return;
      const items = responses.flatMap((response) => response.items);
      setKnownVendors([...new Set(items.map((item) => item.vendor))].sort());
      setKnownProtocols([...new Set(items.flatMap((item) => item.protocols))].sort());
    }).catch(() => {
      if (generation === facetRequestGeneration.current && !controller.signal.aborted) {
        // The visible list still contributes observed values below, so a facet
        // preload failure degrades to usable current-page filters.
        controller.abort();
      }
    }).finally(() => {
      if (generation === facetRequestGeneration.current) setSettledFacetThemeKey(facetThemeKey);
    });
    return () => {
      controller.abort();
      if (generation === facetRequestGeneration.current) facetRequestGeneration.current += 1;
    };
  }, [facetThemeKey]);

  useEffect(() => {
    if (!resolvedActiveTheme) return;
    const controller = new AbortController();
    const generation = ++requestGeneration.current;
    void api.listTemplateScenarios(
      {
        theme: resolvedActiveTheme === "all" ? undefined : resolvedActiveTheme,
        q: debouncedQuery || undefined,
        vendor: filters.vendor,
        adapter_type: filters.adapterType,
        protocol: filters.protocol,
        language: filters.language,
        page: currentPage,
        page_size: PAGE_SIZE,
      },
      controller.signal,
    ).then((response) => {
      if (generation !== requestGeneration.current || controller.signal.aborted) return;
      setScenarios(response.items);
      setTotal(response.total);
      setKnownVendors((current) => [...new Set([...current, ...response.items.map((item) => item.vendor)])].sort());
      setKnownProtocols((current) => [...new Set([...current, ...response.items.flatMap((item) => item.protocols)])].sort());
    }).catch((error: unknown) => {
      if (generation !== requestGeneration.current || controller.signal.aborted) return;
      setListError(true);
      if (!(error instanceof ApiError && error.code === "network_error")) {
        // The stable translated recovery state is intentionally the same for
        // transport and catalog errors; raw server text is never rendered.
      }
    }).finally(() => {
      if (generation === requestGeneration.current && !controller.signal.aborted) setListLoading(false);
    });
    return () => {
      controller.abort();
      if (generation === requestGeneration.current) requestGeneration.current += 1;
    };
  }, [resolvedActiveTheme, currentPage, debouncedQuery, filters, listReloadGeneration]);

  function resetActivePage(): void {
    if (resolvedActiveTheme) {
      setPageByTheme((current) => ({ ...current, [resolvedActiveTheme]: 1 }));
    }
  }

  function updateFilter<Key extends keyof GalleryFilters>(key: Key, value: GalleryFilters[Key]): void {
    resetActivePage();
    setListLoading(true);
    setListError(false);
    setFilters((current) => ({ ...current, [key]: value }));
  }

  const selectWidth = { width: "100%" };
  const filterItems = [
    {
      key: "vendor",
      label: t("filters.vendor"),
      node: (
        <Select
          aria-label={t("filters.vendor")}
          allowClear
          loading={facetsLoading && knownVendors.length === 0}
          value={filters.vendor}
          placeholder={t("filters.all")}
          options={knownVendors.map((value) => ({ value, label: value }))}
          onChange={(value) => updateFilter("vendor", value)}
          style={selectWidth}
        />
      ),
    },
    {
      key: "adapterType",
      label: t("filters.adapterType"),
      node: (
        <Select
          aria-label={t("filters.adapterType")}
          allowClear
          value={filters.adapterType}
          placeholder={t("filters.all")}
          options={(["task", "webhook"] as const).map((value) => ({ value, label: t(`type.${value}`) }))}
          onChange={(value) => updateFilter("adapterType", value)}
          style={selectWidth}
        />
      ),
    },
    {
      key: "protocol",
      label: t("filters.protocol"),
      node: (
        <Select
          aria-label={t("filters.protocol")}
          allowClear
          loading={facetsLoading && knownProtocols.length === 0}
          value={filters.protocol}
          placeholder={t("filters.all")}
          options={knownProtocols.map((value) => ({ value, label: value }))}
          onChange={(value) => updateFilter("protocol", value)}
          style={selectWidth}
        />
      ),
    },
    {
      key: "language",
      label: t("filters.language"),
      node: (
        <Select
          aria-label={t("filters.language")}
          allowClear
          value={filters.language}
          placeholder={t("filters.all")}
          options={LANGUAGES.map((value) => ({ value, label: t(`language.${value}`) }))}
          onChange={(value) => updateFilter("language", value)}
          style={selectWidth}
        />
      ),
    },
  ];

  const galleryPanelContent = (
    <>
      <section className="template-filter-bar" aria-label={t("filters.groupAria")}>
        {filterItems.map((item) => (
          <label key={item.key} className="template-filter">
            <span>{item.label}</span>
            {item.node}
          </label>
        ))}
      </section>

      <div className="template-result-summary" role="status" aria-live="polite">
        {listLoading ? t("gallery.loading") : t("gallery.resultCount", { count: total })}
      </div>

      {listLoading ? (
        <div className="template-card-grid" aria-hidden="true">
          {Array.from({ length: 6 }, (_, index) => (
            <div className="template-card template-card-skeleton" key={index}>
              <Skeleton active avatar paragraph={{ rows: 3 }} />
            </div>
          ))}
        </div>
      ) : listError ? (
        <Alert
          className="template-list-state"
          showIcon
          type="error"
          message={t("gallery.loadFailed")}
          action={(
            <Button
              icon={<ReloadOutlined aria-hidden="true" />}
              onClick={() => {
                setListLoading(true);
                setListError(false);
                setListReloadGeneration((value) => value + 1);
              }}
            >
              {t("gallery.retry")}
            </Button>
          )}
        />
      ) : scenarios.length === 0 ? (
        <Empty
          className="template-list-state"
          description={(
            <div>
              <strong>{t("gallery.emptyTitle")}</strong>
              <p>{t("gallery.emptyDescription")}</p>
            </div>
          )}
        />
      ) : (
        <>
          <section className="template-card-grid">
            {scenarios.map((scenario) => (
              <TemplateCard key={scenario.slug} scenario={scenario} onOpen={onOpenScenario} />
            ))}
          </section>
          <Pagination
            className="template-pagination"
            current={currentPage}
            pageSize={PAGE_SIZE}
            total={total}
            showSizeChanger={false}
            onChange={(page) => {
              setListLoading(true);
              setListError(false);
              setPageByTheme((current) => ({ ...current, [resolvedActiveTheme]: page }));
            }}
          />
        </>
      )}
    </>
  );

  return (
    <main
      ref={containerRef}
      className="template-gallery"
      data-testid="template-gallery"
      hidden={hidden}
      tabIndex={-1}
      aria-busy={themesLoading || (!themesError && themes.length > 0 && (listLoading || facetsLoading))}
    >
      <span className="template-loading-announcement" role="status" aria-live="polite">
        {themesLoading
          ? t("gallery.loading")
          : facetsLoading
            ? t("filters.loading")
            : ""}
      </span>
      <header className="template-gallery-hero">
        <Typography.Title level={1}>{t("gallery.title")}</Typography.Title>
        <Input
          size="large"
          allowClear
          prefix={<SearchOutlined aria-hidden="true" />}
          aria-label={t("gallery.searchAria")}
          placeholder={t("gallery.searchPlaceholder")}
          value={queryInput}
          onChange={(event) => {
            resetActivePage();
            setQueryInput(event.target.value);
          }}
        />
      </header>

      {themesError ? (
        <Alert
          showIcon
          type="error"
          message={t("gallery.loadFailed")}
          action={<Button icon={<ReloadOutlined aria-hidden="true" />} onClick={onReloadThemes}>{t("gallery.retry")}</Button>}
        />
      ) : themesLoading ? (
        <Skeleton active paragraph={{ rows: 2 }} />
      ) : (
        <Tabs
          className="template-theme-tabs"
          activeKey={resolvedActiveTheme}
          onChange={(theme) => {
            setListLoading(true);
            setListError(false);
            setActiveTheme(theme);
          }}
          items={[{
            key: "all",
            label: `${t("filters.all")} · ${themes.reduce((count, theme) => count + theme.scenario_count, 0)}`,
            children: resolvedActiveTheme === "all" ? galleryPanelContent : null,
          }, ...themes.map((theme) => ({
            key: theme.slug,
            label: `${localized(theme.name)} · ${theme.scenario_count}`,
            children: theme.slug === resolvedActiveTheme ? galleryPanelContent : null,
          }))]}
        />
      )}
    </main>
  );
}

function CopyTemplateModal({
  open,
  detail,
  variant,
  onClose,
  onInstantiate,
}: {
  open: boolean;
  detail: TemplateScenarioDetail;
  variant: TemplateVariant;
  onClose: () => void;
  onInstantiate: (request: TemplateCopyRequest) => Promise<boolean>;
}) {
  const { t } = useTranslation("template");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const inputRef = useRef<InputRef>(null);

  function close(): void {
    setName("");
    setDescription("");
    setFieldError(null);
    setSubmitError(null);
    setSubmitting(false);
    submittingRef.current = false;
    onClose();
  }

  function validate(): string | null {
    if (!name.trim()) return t("copy.nameRequired");
    if (name.trim().length > 128) return t("copy.nameTooLong");
    return null;
  }

  async function submit(): Promise<void> {
    if (submittingRef.current) return;
    const validation = validate();
    setFieldError(validation);
    setSubmitError(null);
    if (validation !== null) {
      inputRef.current?.focus();
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    try {
      const completed = await onInstantiate({
        scenarioSlug: detail.slug,
        language: variant.language,
        name: name.trim(),
        description: description.trim() || undefined,
        expected_template_version: variant.template_version,
        draft: { code: variant.code, requirements: variant.requirements, runtime_config: variant.runtime_config },
      });
      if (completed) close();
    } catch (error) {
      if (error instanceof ApiError && error.code === "adapter_name_conflict") {
        setFieldError(t("copy.nameConflict"));
        inputRef.current?.focus();
      } else if (error instanceof ApiError && error.code === "template_version_conflict") {
        setSubmitError(t("copy.versionConflict"));
      } else {
        setSubmitError(t("copy.failed"));
      }
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title={t("copy.title")}
      open={open}
      okText={t("copy.confirm")}
      cancelText={t("copy.cancel")}
      confirmLoading={submitting}
      okButtonProps={{ disabled: submitting }}
      focusTriggerAfterClose
      destroyOnHidden
      afterOpenChange={(isOpen) => {
        if (isOpen) window.requestAnimationFrame(() => inputRef.current?.focus());
      }}
      onCancel={submitting ? undefined : close}
      onOk={() => void submit()}
    >
      <div className="template-copy-modal">
        <p>{t("copy.description", { language: t(`language.${variant.language}`) })}</p>
        <dl className="template-copy-facts">
          <div><dt>{t("copy.language")}</dt><dd>{t(`language.${variant.language}`)}</dd></div>
        </dl>
        <label className="template-copy-field">
          <span>{t("copy.name")}</span>
          <Input
            ref={inputRef}
            value={name}
            maxLength={129}
            required
            aria-required="true"
            status={fieldError ? "error" : undefined}
            aria-invalid={fieldError !== null}
            aria-describedby={fieldError ? "template-copy-name-error" : undefined}
            placeholder={t("copy.namePlaceholder")}
            onChange={(event) => {
              setName(event.target.value);
              if (fieldError !== null) setFieldError(null);
            }}
            onPressEnter={() => void submit()}
          />
        </label>
        {fieldError && <p id="template-copy-name-error" className="template-copy-error" role="alert">{fieldError}</p>}
        <label className="template-copy-field">
          <span>{t("copy.adapterDescription")}</span>
          <Input.TextArea
            value={description}
            rows={3}
            maxLength={2000}
            placeholder={t("copy.descriptionPlaceholder")}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        {submitError && <Alert showIcon type="error" message={submitError} />}
      </div>
    </Modal>
  );
}

function TemplateDetail({
  scenarioSlug,
  busy,
  onBack,
  onInstantiate,
}: {
  scenarioSlug: string;
  busy: boolean;
  onBack: () => void;
  onInstantiate: (request: TemplateCopyRequest) => Promise<boolean>;
}) {
  const { t } = useTranslation("template");
  const [detail, setDetail] = useState<TemplateScenarioDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(true);
  const [detailError, setDetailError] = useState<"not-found" | "load-failed" | null>(null);
  const [detailReloadGeneration, setDetailReloadGeneration] = useState(0);
  const [language, setLanguage] = useState<AdapterLanguage>("python");
  const [variant, setVariant] = useState<TemplateVariant | null>(null);
  const [variantLoading, setVariantLoading] = useState(true);
  const [variantError, setVariantError] = useState(false);
  const [variantReloadGeneration, setVariantReloadGeneration] = useState(0);
  const [copyOpen, setCopyOpen] = useState(false);
  const detailGeneration = useRef(0);
  const variantGeneration = useRef(0);
  const mainRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => mainRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [scenarioSlug]);

  useEffect(() => {
    const controller = new AbortController();
    const generation = ++detailGeneration.current;
    void api.getTemplateScenario(scenarioSlug, controller.signal).then((response) => {
      if (generation === detailGeneration.current && !controller.signal.aborted) {
        setLanguage(response.variants.find((item) => item.available)?.language ?? "python");
        setDetail(response);
      }
    }).catch((error: unknown) => {
      if (generation === detailGeneration.current && !controller.signal.aborted) {
        setDetailError(error instanceof ApiError && error.status === 404 ? "not-found" : "load-failed");
      }
    }).finally(() => {
      if (generation === detailGeneration.current && !controller.signal.aborted) setDetailLoading(false);
    });
    return () => {
      controller.abort();
      if (generation === detailGeneration.current) detailGeneration.current += 1;
    };
  }, [scenarioSlug, detailReloadGeneration]);

  useEffect(() => {
    if (detail === null) return;
    const generation = ++variantGeneration.current;
    void api.getTemplateVariant(detail.slug, detail.template_version, language).then((response) => {
      if (generation === variantGeneration.current) setVariant(response);
    }).catch(() => {
      if (generation === variantGeneration.current) setVariantError(true);
    }).finally(() => {
      if (generation === variantGeneration.current) setVariantLoading(false);
    });
    return () => {
      if (generation === variantGeneration.current) variantGeneration.current += 1;
    };
  }, [detail, language, variantReloadGeneration]);

  function retryVariant(): void {
    variantGeneration.current += 1;
    setVariant(null);
    setVariantLoading(true);
    setVariantError(false);
    setVariantReloadGeneration((value) => value + 1);
  }

  if (detailLoading) {
    return (
      <main
        ref={mainRef}
        className="template-detail template-detail-state"
        tabIndex={-1}
        aria-busy="true"
      >
        <span className="template-loading-announcement" role="status">{t("detail.loading")}</span>
        <Skeleton active paragraph={{ rows: 10 }} aria-hidden="true" />
      </main>
    );
  }

  if (detailError !== null || detail === null) {
    const recoverable = detailError !== "not-found";
    return (
      <main ref={mainRef} className="template-detail template-detail-state" tabIndex={-1}>
        <Empty description={t(recoverable ? "detail.loadFailed" : "detail.notFound")} />
        <div className="template-detail-state-actions">
          {recoverable && (
            <Button
              type="primary"
              icon={<ReloadOutlined aria-hidden="true" />}
              onClick={() => {
                setDetail(null);
                setDetailLoading(true);
                setDetailError(null);
                setDetailReloadGeneration((value) => value + 1);
              }}
            >
              {t("detail.retry")}
            </Button>
          )}
          <Button icon={<ArrowLeftOutlined aria-hidden="true" />} onClick={onBack}>{t("detail.back")}</Button>
        </div>
      </main>
    );
  }

  const recipePanelContent = variantLoading ? (
    <Skeleton active paragraph={{ rows: 12 }} />
  ) : variantError || variant === null ? (
    <Alert
      showIcon
      type="error"
      message={t("detail.variantFailed")}
      action={(
        <Button size="small" icon={<ReloadOutlined aria-hidden="true" />} onClick={retryVariant}>
          {t("detail.retry")}
        </Button>
      )}
    />
  ) : (
    <>
      <div className="template-recipe-heading">
        <h2>{t("detail.recipe")}</h2>
      </div>
      <div className="template-code-view" data-testid="template-code-view">
        <Editor
          height="100%"
          language={variant.language}
          value={variant.code}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            lineNumbersMinChars: 3,
            scrollBeyondLastLine: false,
            ariaLabel: t("detail.recipe"),
          }}
        />
      </div>
      <section className="template-recipe-facts">
        <h3>{t("detail.requirements")}</h3>
        <pre tabIndex={0}>{variant.requirements || "—"}</pre>
        {Object.keys(variant.input_skeleton).length > 0 && <>
          <h3>{t("detail.inputSkeleton")}</h3>
          <pre tabIndex={0}>{jsonText(variant.input_skeleton)}</pre>
        </>}
        {variant.output_example && Object.keys(variant.output_example).length > 0 && <>
          <h3>{t("detail.outputExample")}</h3>
          <pre tabIndex={0}>{jsonText(variant.output_example)}</pre>
        </>}
      </section>
    </>
  );

  return (
    <main
      ref={mainRef}
      className="template-detail"
      data-testid="template-detail"
      tabIndex={-1}
      aria-busy={variantLoading}
    >
      <span className="template-loading-announcement" role="status" aria-live="polite">
        {variantLoading ? t("detail.variantLoading") : ""}
      </span>
      <Button className="template-detail-back" type="link" icon={<ArrowLeftOutlined aria-hidden="true" />} onClick={onBack}>
        {t("detail.back")}
      </Button>

      <header className="template-detail-hero">
        <TemplateScenarioLogo logoKey={detail.logo_key} />
        <div className="template-detail-title">
          <p>{detail.vendor}</p>
          <Typography.Title level={1}>{localized(detail.title)}</Typography.Title>
          <Typography.Paragraph>{localized(detail.summary)}</Typography.Paragraph>
          <div className="template-detail-meta">
            <Tag>{t(`type.${detail.adapter_type}`)}</Tag>
            {detail.protocols.map((protocol) => <Tag key={protocol}>{protocol}</Tag>)}
          </div>
        </div>
        <Button
          type="primary"
          size="large"
          icon={<CopyOutlined aria-hidden="true" />}
          disabled={busy || variantLoading || variantError || variant === null}
          onClick={() => setCopyOpen(true)}
        >
          {t("detail.copy")}
        </Button>
      </header>

      <div className="template-detail-layout">
        <div className="template-detail-overview">
          <section className="template-detail-panel">
            <h2>{t("detail.purpose")}</h2>
            <p>{localized(detail.details)}</p>
          </section>
        </div>

        <section className="template-recipe-panel">
          <Tabs
            className="template-language-tabs"
            activeKey={language}
            onChange={(key) => {
              variantGeneration.current += 1;
              setVariant(null);
              setVariantLoading(true);
              setVariantError(false);
              setLanguage(key as AdapterLanguage);
            }}
            items={detail.variants.filter((item) => item.available).map(({ language: item }) => ({
              key: item,
              label: t(`language.${item}`),
              children: item === language ? recipePanelContent : null,
            }))}
          />
        </section>
      </div>

      {variant !== null && (
        <CopyTemplateModal
          open={copyOpen}
          detail={detail}
          variant={variant}
          onClose={() => setCopyOpen(false)}
          onInstantiate={onInstantiate}
        />
      )}
    </main>
  );
}

export default function TemplateGalleryPage({
  scenarioSlug,
  busy,
  onOpenScenario,
  onBackToGallery,
  onInstantiate,
}: TemplateGalleryPageProps) {
  const [themes, setThemes] = useState<TemplateTheme[]>([]);
  const [themesLoading, setThemesLoading] = useState(true);
  const [themesError, setThemesError] = useState(false);
  const [reloadGeneration, setReloadGeneration] = useState(0);
  const galleryRef = useRef<HTMLElement>(null);
  const lastScenarioTriggerRef = useRef<HTMLAnchorElement | null>(null);
  const scenarioOpenedFromGalleryRef = useRef<string | null>(null);
  const previousScenarioSlugRef = useRef(scenarioSlug);

  useEffect(() => {
    let current = true;
    void api.listTemplateThemes().then((response) => {
      if (current) setThemes(response);
    }).catch(() => {
      if (current) setThemesError(true);
    }).finally(() => {
      if (current) setThemesLoading(false);
    });
    return () => { current = false; };
  }, [reloadGeneration]);

  function reloadThemes(): void {
    setThemesLoading(true);
    setThemesError(false);
    setReloadGeneration((value) => value + 1);
  }

  useEffect(() => {
    const previousScenarioSlug = previousScenarioSlugRef.current;
    previousScenarioSlugRef.current = scenarioSlug;
    if (previousScenarioSlug === null || scenarioSlug !== null) return;
    const frame = window.requestAnimationFrame(() => {
      const trigger = lastScenarioTriggerRef.current;
      if (trigger?.isConnected) {
        trigger.focus();
      } else {
        galleryRef.current?.focus();
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [scenarioSlug]);

  return (
    <>
      <GalleryList
        themes={themes}
        themesLoading={themesLoading}
        themesError={themesError}
        hidden={scenarioSlug !== null}
        containerRef={galleryRef}
        onReloadThemes={reloadThemes}
        onOpenScenario={(slug, trigger) => {
          lastScenarioTriggerRef.current = trigger;
          scenarioOpenedFromGalleryRef.current = slug;
          onOpenScenario(slug);
        }}
      />
      {scenarioSlug !== null && (
        <TemplateDetail
          key={scenarioSlug}
          scenarioSlug={scenarioSlug}
          busy={busy}
          onBack={() => onBackToGallery(
            scenarioOpenedFromGalleryRef.current === scenarioSlug
              && lastScenarioTriggerRef.current?.isConnected === true,
          )}
          onInstantiate={onInstantiate}
        />
      )}
    </>
  );
}
