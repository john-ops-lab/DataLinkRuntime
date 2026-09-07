import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Drawer,
  Skeleton,
  Select,
  Tabs,
  type InputRef,
} from "antd";
import { useTranslation } from "react-i18next";
import { ApiError, api } from "../api";
import { currentSystemLocale } from "../i18n";
import type {
  Adapter,
  PortablePackage,
  PortableVariant,
  TemplateTheme,
  Worker,
} from "../types";

export type PortableMode =
  | "importAdapter"
  | "exportAdapter"
  | "saveTemplate"
  | "importTemplate"
  | "exportTemplate"
  | "editTemplate";
export interface PortableDialogRequest {
  mode: PortableMode;
  adapterId?: number;
  slug?: string;
  expectedVersion?: string;
  hasUnsavedChanges?: boolean;
}
interface Props extends PortableDialogRequest {
  workers?: Worker[];
  onClose: () => void;
  onAdapterCreated?: (adapter: Adapter) => void;
  onTemplateSaved?: (slug: string) => void;
}
const json = (value: unknown) => JSON.stringify(value, null, 2);

function download(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function PortablePackageDialog({
  mode,
  adapterId,
  slug,
  expectedVersion,
  hasUnsavedChanges,
  workers = [],
  onClose,
  onAdapterCreated,
  onTemplateSaved,
}: Props) {
  const { t } = useTranslation("portable");
  const [value, setValue] = useState<PortablePackage | null>(null);
  const [themes, setThemes] = useState<TemplateTheme[]>([]);
  const [includeJson, setIncludeJson] = useState(false);
  const [includeFiles, setIncludeFiles] = useState(false);
  const [workerId, setWorkerId] = useState<number | null>(null);
  const [busy, setBusy] = useState(!mode.startsWith("import"));
  const [savedName, setSavedName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const nameInputRef = useRef<InputRef>(null);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const template = mode.endsWith("Template");
  const importing = mode.startsWith("import");
  const exporting = mode.startsWith("export");
  const editableTemplate = template && !exporting;
  const previewReady = value !== null;

  useEffect(() => {
    if (previewReady)
      nameInputRef.current?.focus({ cursor: "end", preventScroll: true });
  }, [previewReady]);

  useEffect(() => {
    mounted.current = true;
    if (template) {
      void api
        .listTemplateThemes()
        .then((items) => {
          if (mounted.current) setThemes(items);
        })
        .catch(() => {
          if (mounted.current) setError(t("failed"));
        });
    }
    return () => {
      mounted.current = false;
    };
  }, [template, t]);

  const setPackage = useCallback((next: PortablePackage): void => {
    setValue(next);
  }, []);

  const errorText = useCallback(
    (err: unknown): string => {
      if (
        err instanceof ApiError &&
        ["adapter_name_conflict", "template_name_conflict"].includes(err.code)
      )
        return t("nameConflict");
      if (err instanceof ApiError && err.code === "template_version_conflict")
        return t("versionConflict");
      if (
        err instanceof ApiError &&
        ["portable_package_too_large", "input_file_too_large"].includes(
          err.code,
        )
      )
        return t("tooLarge");
      if (
        err instanceof ApiError &&
        [
          "worker_not_found",
          "worker_capability_missing",
          "runtime_worker_required",
          "worker_offline",
        ].includes(err.code)
      )
        return t("workerError");
      if (
        err instanceof ApiError &&
        ["input_source_not_available", "artifact_store_unavailable"].includes(
          err.code,
        )
      )
        return t("filesUnavailable");
      if (
        err instanceof ApiError &&
        err.code === "portable_saved_version_required"
      )
        return t("saveFirst");
      if (
        err instanceof ApiError &&
        ["portable_package_invalid", "portable_object_type"].includes(err.code)
      )
        return t("invalidPackage");
      return t("failed");
    },
    [t],
  );

  const load = useCallback(
    async (file?: File): Promise<void> => {
      if (inFlight.current) return;
      inFlight.current = true;
      setBusy(true);
      setError(null);
      try {
        if (file && file.size > 16 * 1024 * 1024)
          throw new ApiError(413, "portable_package_too_large", "");
        const next = file
          ? await api.previewPortableFile(file)
          : adapterId !== undefined
            ? await api.previewAdapterPackage(adapterId, {
                include_json: false,
                include_files: false,
                as_template: template,
              })
            : await api.getTemplatePackage(slug!);
        if (next.object_type !== (template ? "template" : "adapter"))
          throw new ApiError(422, "portable_object_type", "");
        if (mounted.current) setPackage(next);
      } catch (err) {
        if (mounted.current) setError(errorText(err));
      } finally {
        inFlight.current = false;
        if (mounted.current) setBusy(false);
      }
    },
    [adapterId, slug, template, errorText, setPackage],
  );

  useEffect(() => {
    let active = true;
    if (!importing)
      void Promise.resolve().then(() => {
        if (active) void load();
      });
    return () => {
      active = false;
    };
  }, [importing, load]);

  async function changeIncludedInput(
    kind: "json" | "files",
    checked: boolean,
  ): Promise<void> {
    if (inFlight.current || adapterId === undefined) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const next = await api.previewAdapterPackage(adapterId, {
        as_template: false,
        include_json: kind === "json" ? checked : includeJson,
        include_files: kind === "files" ? checked : includeFiles,
      });
      if (mounted.current) {
        setValue((current) =>
          current ? { ...current, input: next.input } : current,
        );
        if (kind === "json") setIncludeJson(checked);
        else setIncludeFiles(checked);
      }
    } catch (cause) {
      if (mounted.current) setError(errorText(cause));
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  function patchVariant(index: number, patch: Partial<PortableVariant>): void {
    if (!value) return;
    setValue({
      ...value,
      variants: value.variants.map((item, i) =>
        i === index ? { ...item, ...patch } : item,
      ),
    });
  }

  async function submit(): Promise<void> {
    if (!value || inFlight.current) return;
    if (!value.name.trim()) {
      setError(t("nameRequired"));
      return;
    }
    const reviewed: PortablePackage = { ...value, name: value.name.trim() };
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      if (exporting) {
        download(
          await api.exportPortablePackage(reviewed),
          `${reviewed.name.replace(/[/\\]/g, "-")}.${reviewed.object_type}.dlr.zip`,
        );
      } else if (template) {
        const result = await api.saveTemplatePackage(reviewed, {
          adapterId,
          slug: mode === "editTemplate" ? slug : undefined,
          expectedVersion,
        });
        setSavedName(result.title[currentSystemLocale()]);
        onTemplateSaved?.(result.slug);
      } else {
        const result = await api.importAdapterPackage(reviewed, workerId);
        setSavedName(result.name);
        onAdapterCreated?.(result);
      }
      if (mounted.current) setSuccess(true);
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  const languageSummary = value?.variants
    .map((item) => t(`language.${item.language}`, { ns: "template" }))
    .join(" / ");
  const workerRequired =
    mode === "importAdapter" &&
    value?.adapter_type === "task" &&
    workerId === null;
  return (
    <Drawer
      open
      title={t(mode)}
      width="min(1180px, 100vw)"
      onClose={busy ? undefined : onClose}
      footer={
        <div className="portable-footer">
          {success ? (
            <Button type="primary" autoFocus onClick={onClose}>
              {t("close")}
            </Button>
          ) : (
            <>
              <div className="portable-footer-actions">
                <Button disabled={busy} onClick={onClose}>
                  {t("cancel")}
                </Button>
                <Button
                  type="primary"
                  autoInsertSpace={false}
                  loading={busy}
                  disabled={!value || workerRequired || busy}
                  onClick={() => void submit()}
                >
                  {exporting ? t("download") : importing ? t("import") : t("save")}
                </Button>
              </div>
            </>
          )}
        </div>
      }
      closable={!busy}
      maskClosable={false}
      keyboard={!busy}
      destroyOnHidden
    >
      <div className="portable-dialog portable-page" aria-busy={busy}>
        {success ? (
          <Alert
            description={savedName || undefined}
            type="success"
            showIcon
            message={
              exporting
                ? t("exported")
                : template
                  ? t("templateSaved")
                  : t("adapterCreated")
            }
          />
        ) : (
          <>
            {(!template || exporting) && (
              <Alert showIcon type="warning" message={t("sensitiveWarning")} />
            )}
            {hasUnsavedChanges && (
              <Alert showIcon type="warning" message={t("unsavedWarning")} />
            )}
            {template && !exporting && (
              <Alert showIcon type="info" message={t("galleryWarning")} />
            )}
            {error && (
              <div id="portable-error" role="alert">
                <Alert type="error" showIcon message={error} />
              </div>
            )}
            {!value ? (
              <>
                {importing ? (
                  <label className="portable-field">
                    <span>{t("file")}</span>
                    <input
                      type="file"
                      accept=".zip"
                      disabled={busy}
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) void load(file);
                        event.target.value = "";
                      }}
                    />
                  </label>
                ) : busy ? (
                  <Skeleton active paragraph={{ rows: 8 }} />
                ) : (
                  <Button onClick={() => void load()}>{t("retry")}</Button>
                )}
                {importing && <p>{t("previewNote")}</p>}
              </>
            ) : (
              <fieldset disabled={busy} className="portable-fields">
                <div className="portable-topline">
                  <span>
                    {t(`type.${value.adapter_type}`, { ns: "template" })} ·{" "}
                    {languageSummary}
                  </span>
                  {importing && (
                    <Button
                      disabled={busy}
                      onClick={() => {
                        setValue(null);
                        setError(null);
                      }}
                    >
                      {t("chooseAnotherFile")}
                    </Button>
                  )}
                </div>
                <section className="portable-page-section">
                  <h3>{t("basicSettings")}</h3>
                  <label className="portable-field">
                    <span>{t("name")}</span>
                    <Input
                      ref={nameInputRef}
                      required
                      aria-required="true"
                      maxLength={128}
                      value={value.name}
                      aria-describedby={error ? "portable-error" : undefined}
                      onChange={(event) => {
                        setValue({ ...value, name: event.target.value });
                      }}
                    />
                  </label>
                  <label className="portable-field">
                    <span>{t("description")}</span>
                    <Input.TextArea
                      rows={4}
                      value={value.description}
                      onChange={(event) => {
                        setValue({ ...value, description: event.target.value });
                      }}
                    />
                  </label>
                  {template && (
                    <>
                      <label className="portable-field">
                        <span>{t("category")}</span>
                        <Select
                          aria-label={t("category")}
                          disabled={busy || exporting}
                          value={
                            themes.some(
                              (theme) => theme.slug === value.category,
                            )
                              ? value.category
                              : "other"
                          }
                          options={
                            themes.length
                              ? themes.map((theme) => ({
                                  value: theme.slug,
                                  label: theme.name[currentSystemLocale()],
                                }))
                              : [{ value: "other", label: t("other") }]
                          }
                          onChange={(category) => {
                            setValue({ ...value, category });
                          }}
                        />
                      </label>
                    </>
                  )}
                </section>
                <section className="portable-page-section">
                  <h3>{t("contentSettings")}</h3>
                  <Tabs
                    items={value.variants.map((variant, index) => ({
                      key: variant.language,
                      label: t(`language.${variant.language}`, {
                        ns: "template",
                      }),
                      children: (
                        <div className="portable-variant">
                          <label className="portable-field">
                            <span>{t("code")}</span>
                            <Input.TextArea
                              rows={12}
                              className="portable-code"
                              value={variant.code}
                              readOnly={!editableTemplate}
                              onChange={(event) =>
                                patchVariant(index, {
                                  code: event.target.value,
                                })
                              }
                            />
                          </label>
                          <label className="portable-field">
                            <span>{t("requirements")}</span>
                            <Input.TextArea
                              rows={3}
                              value={variant.requirements}
                              readOnly={!editableTemplate}
                              onChange={(event) =>
                                patchVariant(index, {
                                  requirements: event.target.value,
                                })
                              }
                            />
                          </label>
                        </div>
                      ),
                    }))}
                  />
                </section>
                {!template && (
                  <details>
                    <summary>{t("runtimeSettings")}</summary>
                    <pre>
                      {json({
                        timeout_seconds: value.timeout_seconds,
                        schedule: value.schedule,
                        webhook: value.webhook,
                      })}
                    </pre>
                  </details>
                )}
                {!template && value.adapter_type === "task" && (
                  <section>
                    <h3>{t("input")}</h3>
                    {mode === "exportAdapter" && (
                      <div className="portable-optional-content">
                        <Checkbox
                          checked={includeJson}
                          disabled={busy}
                          onChange={(event) =>
                            void changeIncludedInput(
                              "json",
                              event.target.checked,
                            )
                          }
                        >
                          {t("includeJson")}
                        </Checkbox>
                        <Checkbox
                          checked={includeFiles}
                          disabled={busy}
                          onChange={(event) =>
                            void changeIncludedInput(
                              "files",
                              event.target.checked,
                            )
                          }
                        >
                          {t("includeFiles")}
                        </Checkbox>
                      </div>
                    )}
                    <p>
                      {value.input.included
                        ? t("inputIncluded")
                        : t("inputOmitted")}{" "}
                      · {value.input.source_type}
                    </p>
                    {value.input.included &&
                      value.input.source_type === "json" && (
                        <pre>{json(value.input.json_value)}</pre>
                      )}
                    <ul>
                      {value.input.files.map((file, index) => (
                        <li key={index}>
                          {file.filename}{" "}
                          <Button
                            size="small"
                            onClick={() =>
                              download(
                                new Blob([
                                  Uint8Array.from(atob(file.data_base64), (c) =>
                                    c.charCodeAt(0),
                                  ),
                                ]),
                                file.filename,
                              )
                            }
                          >
                            {t("inspectFile")}
                          </Button>
                        </li>
                      ))}
                    </ul>
                  </section>
                )}
                {(value.license || value.provenance) && (
                  <details>
                    <summary>{t("attribution")}</summary>
                    {value.license && (
                      <>
                        <h4>{t("license")}</h4>
                        <pre>{value.license}</pre>
                      </>
                    )}
                    {value.provenance && (
                      <>
                        <h4>{t("provenance")}</h4>
                        <pre>{value.provenance}</pre>
                      </>
                    )}
                  </details>
                )}
                {mode === "importAdapter" && (
                  <>
                    <Alert type="info" showIcon message={t("targetWarning")} />
                    <label className="portable-field">
                      <span>{t("worker")}</span>
                      <Select
                        aria-label={t("worker")}
                        allowClear
                        disabled={busy}
                        value={workerId}
                        placeholder={t("chooseWorker")}
                        options={workers
                          .filter((worker) =>
                            worker.capabilities.includes(
                              value.variants[0].language,
                            ),
                          )
                          .map((worker) => ({
                            value: worker.id,
                            label: `${worker.name} (${worker.status})`,
                          }))}
                        onChange={(id: number | undefined) => {
                          setWorkerId(id ?? null);
                        }}
                      />
                    </label>
                    {workerRequired && <p>{t("workerRequired")}</p>}
                  </>
                )}
                {importing && <p>{t("autoRename")}</p>}
              </fieldset>
            )}
          </>
        )}
      </div>
    </Drawer>
  );
}
