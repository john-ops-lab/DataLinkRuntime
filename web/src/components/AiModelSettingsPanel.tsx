/** One global active AI model setting (M4); Credential values never enter this component. */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Input,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Typography,
} from "antd";
import { ProForm } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { i18n } from "../i18n";

import { ApiError, api } from "../api";
import { subscribeCredentialCatalog } from "../credential-catalog";
import type {
  AiModelSetting,
  AiModelSettingDraft,
  AiCustomProvider,
  AiCustomProviderDraft,
  AiProviderCapability,
  AiProviderProtocol,
  AiProvider,
  AiReasoningEffort,
  AiReasoningMode,
  Credential,
} from "../types";
import { userErrorMessage } from "../user-message";

interface AiModelSettingsPanelProps {
  onError: (message: string) => void;
  onSaved?: () => void;
}

const DEFAULT_SETTING: AiModelSettingDraft = {
  provider: "openai",
  custom_provider_id: null,
  base_url: "",
  model: "",
  credential_id: null,
  reasoning_mode: "default",
  reasoning_effort: null,
};

const PROVIDER_OPTIONS: { label: string; value: AiProvider }[] = [
  { label: "OpenAI", value: "openai" },
  { label: "Anthropic Claude", value: "anthropic" },
  { label: "Google Gemini", value: "gemini" },
  { label: "DeepSeek", value: "deepseek" },
  { label: "Alibaba Qwen", value: "qwen" },
  { label: "Kimi", value: "kimi" },
  { label: "MiniMax", value: "minimax" },
  { label: "GLM", value: "glm" },
  { label: "Doubao", value: "doubao" },
  { label: "Hunyuan", value: "hunyuan" },
  { label: "OpenRouter", value: "openrouter" },
  { label: "SiliconFlow", value: "siliconflow" },
  { label: "Ollama", value: "ollama" },
  { label: "custom_openai_compatible", value: "custom_openai_compatible" },
];

const PROVIDER_DEFAULT_BASE_URLS: Partial<Record<AiProvider, string>> = {
  openai: "https://api.openai.com",
  anthropic: "https://api.anthropic.com",
  gemini: "https://generativelanguage.googleapis.com",
  deepseek: "https://api.deepseek.com",
  qwen: "https://dashscope.aliyuncs.com/compatible-mode",
  kimi: "https://api.moonshot.cn",
  minimax: "https://api.minimax.chat",
  glm: "https://open.bigmodel.cn/api/paas",
  doubao: "https://ark.cn-beijing.volces.com/api/v3",
  hunyuan: "https://api.hunyuan.cloud.tencent.com",
  openrouter: "https://openrouter.ai/api",
  siliconflow: "https://api.siliconflow.cn",
  ollama: "http://ollama:11434",
};

const REASONING_EFFORTS_BY_PROVIDER: Record<
  AiProvider,
  readonly AiReasoningEffort[]
> = {
  openai: ["low", "medium", "high", "xhigh"],
  anthropic: [],
  gemini: [],
  deepseek: ["high", "max"],
  qwen: [],
  kimi: [],
  minimax: [],
  glm: [],
  doubao: [],
  hunyuan: [],
  openrouter: [],
  siliconflow: [],
  ollama: [],
  custom_openai_compatible: [],
};

const DEFAULT_CUSTOM_PROVIDER: AiCustomProviderDraft = {
  name: "",
  protocol: "openai_compatible",
  base_url: "",
  credential_id: null,
  images_native: false,
  files_native: false,
  tools_supported: false,
};

function supportedReasoningEfforts(
  provider: AiProvider,
  mode: AiReasoningMode,
): readonly AiReasoningEffort[] {
  return mode === "enabled" ? REASONING_EFFORTS_BY_PROVIDER[provider] : [];
}

function normalizeReasoningEffort(
  provider: AiProvider,
  mode: AiReasoningMode,
  effort: AiReasoningEffort | null,
): AiReasoningEffort | null {
  if (effort === null || !supportedReasoningEfforts(provider, mode).includes(effort)) {
    return null;
  }
  return effort;
}

function errorMessage(error: unknown, fallback: string): string {
  return userErrorMessage(error, fallback);
}

function normalizeSetting(setting: AiModelSetting | null): AiModelSettingDraft {
  if (setting === null) {
    return { ...DEFAULT_SETTING };
  }
  return {
    provider: setting.provider,
    custom_provider_id: setting.custom_provider_id ?? null,
    base_url: setting.base_url,
    model: setting.model,
    credential_id: setting.credential_id,
    reasoning_mode: setting.reasoning_mode,
    reasoning_effort: normalizeReasoningEffort(
      setting.provider,
      setting.reasoning_mode,
      setting.reasoning_effort,
    ),
  };
}

export default function AiModelSettingsPanel(props: AiModelSettingsPanelProps) {
  const { t } = useTranslation(["ai", "common"]);
  const { onError, onSaved } = props;
  const [form, setForm] = useState<AiModelSettingDraft>({ ...DEFAULT_SETTING });
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [providerCatalog, setProviderCatalog] = useState<AiProviderCapability[]>([]);
  const [customProviders, setCustomProviders] = useState<AiCustomProvider[]>([]);
  const [customDraft, setCustomDraft] = useState<AiCustomProviderDraft>({
    ...DEFAULT_CUSTOM_PROVIDER,
  });
  const [editingCustomId, setEditingCustomId] = useState<number | null>(null);
  const [customBusy, setCustomBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [refreshingModels, setRefreshingModels] = useState(false);
  const [testing, setTesting] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const reasoningEfforts = supportedReasoningEfforts(form.provider, form.reasoning_mode);
  const actionBusy = saving || refreshingModels || testing || customBusy;
  const catalogById = new Map(providerCatalog.map((provider) => [provider.id, provider]));
  const providerOptions = PROVIDER_OPTIONS.map((option) =>
    option.value === "custom_openai_compatible"
      ? { ...option, label: t("model.provider.custom") }
      : { ...option, label: catalogById.get(option.value)?.name ?? option.label },
  );
  const providerSelectOptions = [
    ...providerOptions,
    ...customProviders.map((provider) => ({
      label: t("model.customOption", { name: provider.name }),
      value: `custom:${provider.id}`,
    })),
  ];
  const selectedProviderValue =
    form.custom_provider_id === null || form.custom_provider_id === undefined
      ? form.provider
      : `custom:${form.custom_provider_id}`;
  const selectedProviderLabel =
    providerSelectOptions.find((option) => option.value === selectedProviderValue)?.label ??
    (form.provider === "custom_openai_compatible"
      ? t("model.provider.custom")
      : form.provider);
  const selectedCredentialName = credentials.find(
    (credential) => credential.id === form.credential_id,
  )?.name;

  const fail = useCallback(
    (message: string) => {
      setPanelError(message);
      setNotice(null);
      onError(message);
    },
    [onError],
  );

  const loadCredentials = useCallback(async () => {
    try {
      const credentialList = await api.listCredentials();
      setCredentials(credentialList.filter((credential) => credential.type === "token"));
    } catch (error) {
      fail(userErrorMessage(error, i18n.t("model.requestFailed")));
    }
  }, [fail]);

  const load = useCallback(async () => {
    setLoading(true);
    const [settingResult, credentialsResult, catalogResult, customResult] = await Promise.allSettled([
      api.getAiSetting(),
      api.listCredentials(),
      api.getAiProviders(),
      api.listAiCustomProviders(),
    ]);

    if (settingResult.status === "fulfilled") {
      const normalized = normalizeSetting(settingResult.value);
      setForm(normalized);
      setAdvancedOpen(normalized.reasoning_mode !== "default");
    } else if (
      !(settingResult.reason instanceof ApiError) ||
      settingResult.reason.code !== "ai_not_configured"
    ) {
      fail(userErrorMessage(settingResult.reason, i18n.t("model.requestFailed")));
    }

    if (credentialsResult.status === "fulfilled") {
      setCredentials(
        credentialsResult.value.filter((credential) => credential.type === "token"),
      );
    } else {
      fail(userErrorMessage(credentialsResult.reason, i18n.t("model.requestFailed")));
    }
    if (catalogResult.status === "fulfilled") {
      setProviderCatalog(catalogResult.value.providers);
    }
    if (customResult.status === "fulfilled") {
      setCustomProviders(customResult.value.providers);
    }
    setLoading(false);
  }, [fail]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- tab mount intentionally loads global settings
    void load();
  }, [load]);

  // 凭据增删改后无需 F5 即可看到最新 token 凭据（UX-003）；只刷新元数据，
  // 已填写的表单字段不受影响。
  useEffect(
    () => subscribeCredentialCatalog(() => void loadCredentials()),
    [loadCredentials],
  );

  function editForm(updater: (current: AiModelSettingDraft) => AiModelSettingDraft) {
    setPanelError(null);
    setNotice(null);
    setForm(updater);
  }

  function currentPayload(): AiModelSettingDraft | null {
    const baseUrl = form.base_url.trim();
    const model = form.model.trim();
    if (baseUrl === "" || model === "") {
      fail(t("model.baseUrlModelRequired"));
      return null;
    }
    const reasoningEffort = normalizeReasoningEffort(
      form.provider,
      form.reasoning_mode,
      form.reasoning_effort,
    );
    if (
      form.provider === "openai" &&
      form.reasoning_mode === "enabled" &&
      reasoningEffort === null
    ) {
      fail(t("model.openaiReasoningRequired"));
      return null;
    }
    const payload: AiModelSettingDraft = {
      provider: form.provider,
      base_url: baseUrl,
      model,
      credential_id: form.credential_id,
      reasoning_mode: form.reasoning_mode,
      reasoning_effort: reasoningEffort,
    };
    if (form.custom_provider_id !== null && form.custom_provider_id !== undefined) {
      payload.custom_provider_id = form.custom_provider_id;
    }
    return payload;
  }

  async function handleRefreshModels() {
    if (actionBusy) {
      return;
    }
    setPanelError(null);
    setNotice(null);
    const baseUrl = form.base_url.trim();
    if (baseUrl === "") {
      fail(t("model.refreshBaseUrlRequired"));
      return;
    }
    setRefreshingModels(true);
    try {
      const refreshPayload: Parameters<typeof api.refreshAiModels>[0] = {
        provider: form.provider,
        base_url: baseUrl,
        credential_id: form.credential_id,
      };
      if (form.custom_provider_id !== null && form.custom_provider_id !== undefined) {
        refreshPayload.custom_provider_id = form.custom_provider_id;
      }
      const response = await api.refreshAiModels(refreshPayload);
      setModelOptions(response.models);
      setNotice(
        response.models.length === 0
          ? t("model.refreshEmpty")
          : t("model.refreshSuccess", { count: response.models.length }),
      );
    } catch (error) {
      if (error instanceof ApiError && error.code === "ai_models_not_supported") {
        // 服务端文案已包含"可手工填写模型 ID"，不再重复追加。
        fail(errorMessage(error, t("model.requestFailed")));
      } else {
        fail(t("model.refreshFailedManualHint", { error: errorMessage(error, t("model.requestFailed")) }));
      }
    } finally {
      setRefreshingModels(false);
    }
  }

  async function handleSave() {
    if (actionBusy) {
      return;
    }
    setPanelError(null);
    setNotice(null);
    const payload = currentPayload();
    if (payload === null) {
      return;
    }
    setSaving(true);
    try {
      const saved = await api.updateAiSetting(payload);
      const normalized = normalizeSetting(saved);
      setForm(normalized);
       setNotice(t("model.saveNotice"));
      onSaved?.();
    } catch (error) {
      fail(errorMessage(error, t("model.requestFailed")));
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    if (actionBusy) {
      return;
    }
    setPanelError(null);
    setNotice(null);
    const payload = currentPayload();
    if (payload === null) {
      return;
    }
    setTesting(true);
    try {
      const result = await api.testAiSetting(payload);
      const detail = result.message.trim();
      // The success sentence is DLR-owned and fully localized here; the
      // server message is a compatibility field and is never interpolated
      // verbatim, so the en Console cannot mix in zh-CN server text.
      if (result.ok) {
        setNotice(t("model.testSuccess"));
      } else {
        fail(detail === "" ? t("model.testFailed") : t("model.testFailedDetail", { detail }));
      }
    } catch (error) {
      fail(errorMessage(error, t("model.requestFailed")));
    } finally {
      setTesting(false);
    }
  }

  function editCustomDraft(updater: (current: AiCustomProviderDraft) => AiCustomProviderDraft) {
    setPanelError(null);
    setNotice(null);
    setCustomDraft(updater);
  }

  function startCustomEdit(provider: AiCustomProvider) {
    setEditingCustomId(provider.id);
    setCustomDraft({
      name: provider.name,
      protocol: provider.protocol,
      base_url: provider.base_url,
      credential_id: provider.credential_id,
      images_native: provider.images_native,
      files_native: provider.files_native,
      tools_supported: provider.tools_supported,
    });
    setPanelError(null);
    setNotice(null);
  }

  function resetCustomEditor() {
    setEditingCustomId(null);
    setCustomDraft({ ...DEFAULT_CUSTOM_PROVIDER });
  }

  async function handleCustomSave() {
    if (actionBusy) {
      return;
    }
    if (customDraft.name.trim() === "" || customDraft.base_url.trim() === "") {
      fail(t("model.customRequired"));
      return;
    }
    setCustomBusy(true);
    setPanelError(null);
    setNotice(null);
    try {
      const saved = editingCustomId === null
        ? await api.createAiCustomProvider({ ...customDraft, name: customDraft.name.trim(), base_url: customDraft.base_url.trim() })
        : await api.updateAiCustomProvider(editingCustomId, {
            ...customDraft,
            name: customDraft.name.trim(),
            base_url: customDraft.base_url.trim(),
          });
      setCustomProviders((current) => {
        const withoutSaved = current.filter((provider) => provider.id !== saved.id);
        return [...withoutSaved, saved].sort((left, right) => left.name.localeCompare(right.name));
      });
      if (form.custom_provider_id === saved.id) {
        editForm((current) => ({
          ...current,
          provider: "custom_openai_compatible",
          custom_provider_id: saved.id,
          base_url: saved.base_url,
          credential_id: saved.credential_id,
        }));
      }
      setNotice(t("model.customSaved"));
      resetCustomEditor();
    } catch (error) {
      fail(errorMessage(error, t("model.requestFailed")));
    } finally {
      setCustomBusy(false);
    }
  }

  async function handleCustomTest(provider: AiCustomProvider) {
    if (actionBusy || form.model.trim() === "") {
      fail(t("model.baseUrlModelRequired"));
      return;
    }
    setCustomBusy(true);
    setPanelError(null);
    setNotice(null);
    try {
      const result = await api.testAiCustomProvider(provider.id, form.model.trim());
      if (result.ok) {
        setNotice(t("model.testSuccess"));
      } else {
        fail(result.message.trim() || t("model.testFailed"));
      }
    } catch (error) {
      fail(errorMessage(error, t("model.requestFailed")));
    } finally {
      setCustomBusy(false);
    }
  }

  async function handleCustomDelete(provider: AiCustomProvider) {
    if (actionBusy) {
      return;
    }
    setCustomBusy(true);
    setPanelError(null);
    setNotice(null);
    try {
      await api.deleteAiCustomProvider(provider.id);
      setCustomProviders((current) => current.filter((item) => item.id !== provider.id));
      if (form.custom_provider_id === provider.id) {
        editForm((current) => ({
          ...current,
          provider: "custom_openai_compatible",
          custom_provider_id: null,
          base_url: "",
          credential_id: null,
        }));
      }
      if (editingCustomId === provider.id) {
        resetCustomEditor();
      }
      setNotice(t("model.customDeleted"));
    } catch (error) {
      fail(errorMessage(error, t("model.requestFailed")));
    } finally {
      setCustomBusy(false);
    }
  }

  if (loading) {
    return <Spin />;
  }

  return (
    <div className="settings-panel ai-model-settings" data-testid="ai-model-settings-panel">
      <Alert
        type="info"
        showIcon
        message={t("model.notice")}
        description={t("model.boundary")}
        data-testid="ai-data-boundary-warning"
      />
      <ProForm<AiModelSettingDraft>
        className="wave-c-form ai-model-form"
        layout="vertical"
        submitter={false}
      >
        <div className="ai-primary-config" data-testid="ai-primary-config">
          <Card
            size="small"
            title={t("model.currentConfig")}
            className="ai-current-config-summary"
            data-testid="ai-current-config-summary"
          >
            <dl className="ai-current-config-summary-list">
              <div>
                <dt>{t("model.summaryProvider")}</dt>
                <dd data-testid="ai-summary-provider">{selectedProviderLabel}</dd>
              </div>
              <div>
                <dt>{t("model.summaryBaseUrl")}</dt>
                <dd data-testid="ai-summary-base-url">
                  {form.base_url.trim() || t("model.summaryNotConfigured")}
                </dd>
              </div>
              <div>
                <dt>{t("model.summaryModel")}</dt>
                <dd data-testid="ai-summary-model">
                  {form.model.trim() || t("model.summaryNotConfigured")}
                </dd>
              </div>
              <div>
                <dt>{t("model.summaryCredential")}</dt>
                <dd data-testid="ai-summary-credential">
                  {selectedCredentialName ?? t("model.summaryNoCredential")}
                </dd>
              </div>
            </dl>
          </Card>

        <ProForm.Item label={t("model.provider.label")}>
          <Select<string>
            data-testid="ai-provider"
            disabled={actionBusy}
            showSearch
            virtual={false}
            optionFilterProp="label"
            value={selectedProviderValue}
            options={providerSelectOptions}
            onChange={(selection) => {
              setModelOptions([]);
              if (selection.startsWith("custom:")) {
                const providerId = Number(selection.slice("custom:".length));
                const custom = customProviders.find((item) => item.id === providerId);
                if (custom !== undefined) {
                  editForm((current) => ({
                    ...current,
                    provider: "custom_openai_compatible",
                    custom_provider_id: custom.id,
                    base_url: custom.base_url,
                    credential_id: custom.credential_id,
                    reasoning_effort: null,
                  }));
                }
                return;
              }
              const provider = selection as AiProvider;
              editForm((current) => ({
                ...current,
                provider,
                custom_provider_id: null,
                base_url:
                  current.base_url.trim() === ""
                    ? catalogById.get(provider)?.base_url ??
                      PROVIDER_DEFAULT_BASE_URLS[provider] ??
                      current.base_url
                    : current.base_url,
                reasoning_effort: normalizeReasoningEffort(
                  provider,
                  current.reasoning_mode,
                  current.reasoning_effort,
                ),
              }));
            }}
          />
        </ProForm.Item>

        <ProForm.Item label={t("model.baseUrl")}>
          <Input
            data-testid="ai-base-url"
            disabled={actionBusy}
            placeholder="https://api.example.com"
            value={form.base_url}
            onChange={(event) => {
              setModelOptions([]);
              editForm((current) => ({ ...current, base_url: event.target.value }));
            }}
          />
          <Typography.Text type="secondary">{t("model.baseUrlHint")}</Typography.Text>
        </ProForm.Item>

        <ProForm.Item label={t("model.credential")}>
          <Select<number>
            data-testid="ai-credential"
            disabled={actionBusy}
            allowClear
            placeholder={t("model.credentialPlaceholder")}
            value={form.credential_id ?? undefined}
            options={credentials.map((credential) => ({
              label: credential.name,
              value: credential.id,
            }))}
            onChange={(credentialId) =>
              editForm((current) => ({ ...current, credential_id: credentialId ?? null }))
            }
          />
        </ProForm.Item>

        <ProForm.Item label={t("model.modelId")}>
          <Input
            data-testid="ai-model-input"
            disabled={actionBusy}
            list="ai-model-suggestions"
            placeholder={t("model.modelPlaceholder")}
            value={form.model}
            onChange={(event) => editForm((current) => ({ ...current, model: event.target.value }))}
          />
          <datalist id="ai-model-suggestions" data-testid="ai-model-suggestions">
            {modelOptions.map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
          <Select<string>
            data-testid="ai-model-search"
            disabled={actionBusy || modelOptions.length === 0}
            showSearch
            allowClear
            optionFilterProp="label"
            placeholder={t("model.modelSearch")}
            options={modelOptions.map((model) => ({ label: model, value: model }))}
            onChange={(model) => {
              if (model !== undefined) {
                editForm((current) => ({ ...current, model }));
              }
            }}
          />
        </ProForm.Item>

        <Space wrap>
          <Button
            data-testid="ai-refresh-models"
            loading={refreshingModels}
            disabled={actionBusy}
            onClick={() => void handleRefreshModels()}
          >
            {t("model.refreshModels")}
          </Button>
          <Typography.Text type="secondary">{t("model.refreshHint")}</Typography.Text>
        </Space>
        </div>

        <Collapse
          ghost
          size="small"
          className="ai-secondary-settings"
          collapsible={actionBusy ? "disabled" : "header"}
          defaultActiveKey={[]}
          items={[
            {
              key: "custom-providers",
              label: t("model.customProviders"),
              children: (
                <div className="ai-custom-providers" data-testid="ai-custom-providers">
                  <Typography.Text type="secondary">{t("model.customProvidersHint")}</Typography.Text>
                  <Space direction="vertical" size="small" className="ai-custom-provider-list">
                    {customProviders.map((provider) => (
                      <Space key={provider.id} wrap className="ai-custom-provider-row">
                        <Typography.Text strong>{provider.name}</Typography.Text>
                        <Typography.Text type="secondary">{provider.protocol}</Typography.Text>
                        {provider.referenced && (
                          <Typography.Text type="secondary">{t("model.customReferenced")}</Typography.Text>
                        )}
                        <Button size="small" onClick={() => startCustomEdit(provider)} disabled={actionBusy}>
                          {t("model.customEdit")}
                        </Button>
                        <Button
                          size="small"
                          onClick={() => void handleCustomTest(provider)}
                          disabled={actionBusy}
                        >
                          {t("model.customTest")}
                        </Button>
                        <Popconfirm
                          title={t("model.customDeleteConfirm")}
                          description={provider.referenced ? t("model.customDeleteReferenced") : undefined}
                          okText={t("model.customDelete")}
                          cancelText={t("model.customCancel")}
                          onConfirm={() => void handleCustomDelete(provider)}
                        >
                          <Button size="small" danger disabled={actionBusy}>
                            {t("model.customDelete")}
                          </Button>
                        </Popconfirm>
                      </Space>
                    ))}
                  </Space>
                  <Space direction="vertical" size="small" className="ai-custom-provider-editor">
                    <Input
                      data-testid="ai-custom-name"
                      disabled={actionBusy}
                      placeholder={t("model.customName")}
                      value={customDraft.name}
                      onChange={(event) =>
                        editCustomDraft((current) => ({ ...current, name: event.target.value }))
                      }
                    />
                    <Select<AiProviderProtocol>
                      data-testid="ai-custom-protocol"
                      disabled={actionBusy}
                      value={customDraft.protocol}
                      options={[
                        { label: "OpenAI-compatible", value: "openai_compatible" },
                        { label: "Anthropic Messages", value: "anthropic" },
                        { label: "Google Gemini", value: "gemini" },
                      ]}
                      onChange={(protocol) => editCustomDraft((current) => ({ ...current, protocol }))}
                    />
                    <Input
                      data-testid="ai-custom-base-url"
                      disabled={actionBusy}
                      placeholder={t("model.baseUrl")}
                      value={customDraft.base_url}
                      onChange={(event) =>
                        editCustomDraft((current) => ({ ...current, base_url: event.target.value }))
                      }
                    />
                    <Select<number>
                      data-testid="ai-custom-credential"
                      disabled={actionBusy}
                      allowClear
                      placeholder={t("model.credentialPlaceholder")}
                      value={customDraft.credential_id ?? undefined}
                      options={credentials.map((credential) => ({
                        label: credential.name,
                        value: credential.id,
                      }))}
                      onChange={(credentialId) =>
                        editCustomDraft((current) => ({ ...current, credential_id: credentialId ?? null }))
                      }
                    />
                    <Space wrap>
                      <Typography.Text>{t("model.customImages")}</Typography.Text>
                      <Switch
                        checked={customDraft.images_native}
                        disabled={actionBusy}
                        onChange={(images_native) =>
                          editCustomDraft((current) => ({ ...current, images_native }))
                        }
                      />
                      <Typography.Text>{t("model.customTools")}</Typography.Text>
                      <Switch
                        checked={customDraft.tools_supported}
                        disabled={actionBusy}
                        onChange={(tools_supported) =>
                          editCustomDraft((current) => ({ ...current, tools_supported }))
                        }
                      />
                      <Typography.Text>{t("model.customFiles")}</Typography.Text>
                      <Switch
                        checked={customDraft.files_native}
                        disabled={actionBusy}
                        onChange={(files_native) =>
                          editCustomDraft((current) => ({ ...current, files_native }))
                        }
                      />
                    </Space>
                    <Space>
                      <Button
                        type="primary"
                        data-testid="ai-custom-save"
                        disabled={actionBusy}
                        onClick={() => void handleCustomSave()}
                      >
                        {editingCustomId === null ? t("model.customCreate") : t("model.customUpdate")}
                      </Button>
                      {editingCustomId !== null && (
                        <Button disabled={actionBusy} onClick={resetCustomEditor}>
                          {t("model.customCancel")}
                        </Button>
                      )}
                    </Space>
                  </Space>
                </div>
              ),
            },
          ]}
        />

        <Collapse
          ghost
          size="small"
          collapsible={actionBusy ? "disabled" : "header"}
          activeKey={advancedOpen ? ["reasoning"] : []}
          onChange={(key) =>
            setAdvancedOpen(Array.isArray(key) ? key.includes("reasoning") : key === "reasoning")
          }
          items={[
            {
              key: "reasoning",
              label: t("model.advanced", {
                value:
                  form.reasoning_mode === "default"
                    ? t("model.reasoningDefault")
                    : form.reasoning_mode === "enabled"
                      ? t("model.reasoningEnabled")
                      : t("model.reasoningDisabled"),
              }),
              children: (
                <div className="settings-advanced">
                  <ProForm.Item label={t("model.reasoningMode")}>
                    <Select<AiReasoningMode>
                      data-testid="ai-reasoning-mode"
                      disabled={actionBusy}
                      value={form.reasoning_mode}
                      options={[
                        { label: t("model.reasoningDefault"), value: "default" },
                        { label: t("model.reasoningOpen"), value: "enabled" },
                        { label: t("model.reasoningClose"), value: "disabled" },
                      ]}
                      onChange={(reasoningMode) =>
                        editForm((current) => ({
                          ...current,
                          reasoning_mode: reasoningMode,
                          reasoning_effort: normalizeReasoningEffort(
                            current.provider,
                            reasoningMode,
                            current.reasoning_effort,
                          ),
                        }))
                      }
                    />
                  </ProForm.Item>

                  {reasoningEfforts.length > 0 && (
                    <ProForm.Item label={form.provider === "openai" ? t("model.reasoningEffort") : t("model.reasoningEffortOptional")}>
                      <Select<AiReasoningEffort>
                        data-testid="ai-reasoning-effort"
                        disabled={actionBusy}
                        allowClear={form.provider !== "openai"}
                        placeholder={
                          form.provider === "openai"
                            ? t("model.reasoningEffortPlaceholder")
                            : t("model.reasoningEffortDefault")
                        }
                        value={form.reasoning_effort ?? undefined}
                        options={reasoningEfforts.map((effort) => ({ label: effort, value: effort }))}
                        onChange={(reasoningEffort) =>
                          editForm((current) => ({
                            ...current,
                            reasoning_effort: reasoningEffort ?? null,
                          }))
                        }
                      />
                    </ProForm.Item>
                  )}

                  <Typography.Text type="secondary">{t("model.reasoningHint")}</Typography.Text>
                </div>
              ),
            },
          ]}
        />

        {panelError !== null && (
          <p className="settings-panel-error" role="alert" data-testid="ai-settings-error">
            {panelError}
          </p>
        )}
        {notice !== null && (
          <p className="settings-panel-success" role="status" data-testid="ai-settings-notice">
            {notice}
          </p>
        )}

        <Space>
          <Button
            data-testid="ai-test-connection"
            loading={testing}
            disabled={actionBusy}
            onClick={() => void handleTest()}
          >
            {t("actions.testConnection", { ns: "common" })}
          </Button>
          <Button
            type="primary"
            data-testid="ai-save-settings"
            loading={saving}
            disabled={actionBusy}
            onClick={() => void handleSave()}
          >
            {t("actions.save", { ns: "common" })}
          </Button>
        </Space>
      </ProForm>
    </div>
  );
}
