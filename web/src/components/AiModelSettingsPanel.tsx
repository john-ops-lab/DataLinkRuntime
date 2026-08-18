/** One global active AI model setting (M4); Credential values never enter this component. */

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Collapse, Input, Select, Space, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { i18n } from "../i18n";

import { ApiError, api } from "../api";
import { subscribeCredentialCatalog } from "../credential-catalog";
import type {
  AiModelSetting,
  AiModelSettingDraft,
  AiProvider,
  AiReasoningEffort,
  AiReasoningMode,
  Credential,
} from "../types";
import { userErrorMessage } from "../user-message";

interface AiModelSettingsPanelProps {
  onError: (message: string) => void;
}

const DEFAULT_SETTING: AiModelSettingDraft = {
  provider: "openai",
  base_url: "",
  model: "",
  credential_id: null,
  reasoning_mode: "default",
  reasoning_effort: null,
};

const PROVIDER_OPTIONS: { label: string; value: AiProvider }[] = [
  { label: "OpenAI", value: "openai" },
  { label: "DeepSeek", value: "deepseek" },
  { label: "Kimi", value: "kimi" },
  { label: "MiniMax", value: "minimax" },
  { label: "custom_openai_compatible", value: "custom_openai_compatible" },
];

const REASONING_EFFORTS_BY_PROVIDER: Record<
  AiProvider,
  readonly AiReasoningEffort[]
> = {
  openai: ["low", "medium", "high", "xhigh"],
  deepseek: ["high", "max"],
  kimi: [],
  minimax: [],
  custom_openai_compatible: [],
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
  const { onError } = props;
  const [form, setForm] = useState<AiModelSettingDraft>({ ...DEFAULT_SETTING });
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [refreshingModels, setRefreshingModels] = useState(false);
  const [testing, setTesting] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const reasoningEfforts = supportedReasoningEfforts(form.provider, form.reasoning_mode);
  const actionBusy = saving || refreshingModels || testing;
  const providerOptions = PROVIDER_OPTIONS.map((option) =>
    option.value === "custom_openai_compatible"
      ? { ...option, label: t("model.provider.custom") }
      : option,
  );

  function editForm(updater: (current: AiModelSettingDraft) => AiModelSettingDraft) {
    setPanelError(null);
    setNotice(null);
    setForm(updater);
  }

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
    const [settingResult, credentialsResult] = await Promise.allSettled([
      api.getAiSetting(),
      api.listCredentials(),
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
    return {
      provider: form.provider,
      base_url: baseUrl,
      model,
      credential_id: form.credential_id,
      reasoning_mode: form.reasoning_mode,
      reasoning_effort: reasoningEffort,
    };
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
      const response = await api.refreshAiModels({
        provider: form.provider,
        base_url: baseUrl,
        credential_id: form.credential_id,
      });
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
      setForm(normalizeSetting(saved));
       setNotice(t("model.saveNotice"));
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
      if (result.ok) {
         setNotice(detail === "" ? t("model.testSuccess") : t("model.testSuccessDetail", { detail }));
      } else {
         fail(detail === "" ? t("model.testFailed") : t("model.testFailedDetail", { detail }));
      }
    } catch (error) {
      fail(errorMessage(error, t("model.requestFailed")));
    } finally {
      setTesting(false);
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

      <label className="settings-field">
         <span>{t("model.provider")}</span>
        <Select<AiProvider>
          data-testid="ai-provider"
          disabled={actionBusy}
          value={form.provider}
           options={providerOptions}
          onChange={(provider) =>
            editForm((current) => ({
              ...current,
              provider,
              reasoning_effort: normalizeReasoningEffort(
                provider,
                current.reasoning_mode,
                current.reasoning_effort,
              ),
            }))
          }
        />
      </label>

      <label className="settings-field">
         <span>{t("model.baseUrl")}</span>
        <Input
          data-testid="ai-base-url"
          disabled={actionBusy}
          placeholder="https://api.example.com"
          value={form.base_url}
          onChange={(event) =>
            editForm((current) => ({ ...current, base_url: event.target.value }))
          }
        />
        <Typography.Text type="secondary">
          {t("model.baseUrlHint")}
        </Typography.Text>
      </label>

      <label className="settings-field">
         <span>{t("model.credential")}</span>
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
      </label>

      <label className="settings-field">
         <span>{t("model.modelId")}</span>
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
      </label>

      <Space wrap>
        <Button
          data-testid="ai-refresh-models"
          loading={refreshingModels}
          disabled={actionBusy}
          onClick={() => void handleRefreshModels()}
        >
          {t("model.refreshModels")}
        </Button>
        <Typography.Text type="secondary">
          {t("model.refreshHint")}
        </Typography.Text>
      </Space>

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
             label: t("model.advanced", { value:
               form.reasoning_mode === "default"
                 ? t("model.reasoningDefault")
                 : form.reasoning_mode === "enabled"
                   ? t("model.reasoningEnabled")
                   : t("model.reasoningDisabled")
             }),
            children: (
              <div className="settings-advanced">
                <label className="settings-field">
                   <span>{t("model.reasoningMode")}</span>
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
                </label>

                {reasoningEfforts.length > 0 && (
                  <label className="settings-field">
                   <span>{form.provider === "openai" ? t("model.reasoningEffort") : t("model.reasoningEffortOptional")}</span>
                    <Select<AiReasoningEffort>
                      data-testid="ai-reasoning-effort"
                      disabled={actionBusy}
                      allowClear={form.provider !== "openai"}
                      placeholder={
                         form.provider === "openai" ? t("model.reasoningEffortPlaceholder") : t("model.reasoningEffortDefault")
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
                  </label>
                )}

                <Typography.Text type="secondary">
                   {t("model.reasoningHint")}
                </Typography.Text>
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
    </div>
  );
}
