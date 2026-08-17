/** One global active AI model setting (M4); Credential values never enter this component. */

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Collapse, Input, Select, Space, Spin, Typography } from "antd";

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
  { label: "自定义 OpenAI 兼容服务", value: "custom_openai_compatible" },
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

function errorMessage(error: unknown): string {
  return userErrorMessage(error, "AI 设置请求失败");
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
      fail(errorMessage(error));
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
      fail(errorMessage(settingResult.reason));
    }

    if (credentialsResult.status === "fulfilled") {
      setCredentials(
        credentialsResult.value.filter((credential) => credential.type === "token"),
      );
    } else {
      fail(errorMessage(credentialsResult.reason));
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
      fail("基础 URL 与模型 ID 均不能为空");
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
      fail("OpenAI 开启推理时必须显式选择受支持的推理强度");
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
      fail("刷新模型前请填写基础 URL");
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
          ? "刷新成功，但未发现可用模型；可手工填写模型 ID。"
          : `已刷新 ${response.models.length} 个模型 ID；不会自动更改当前选择。`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.code === "ai_models_not_supported") {
        // 服务端文案已包含"可手工填写模型 ID"，不再重复追加。
        fail(errorMessage(error));
      } else {
        fail(`${errorMessage(error)}；仍可手工输入模型 ID。`);
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
      setNotice("AI 模型设置已保存。模型 ID 仅在管理员再次修改时才会变化。");
    } catch (error) {
      fail(errorMessage(error));
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
        setNotice(detail === "" ? "连接测试通过：模型返回可解析的最小响应。" : `连接测试通过：${detail}`);
      } else {
        fail(detail === "" ? "连接测试失败；请检查模型服务配置与网络。" : `连接测试失败：${detail}`);
      }
    } catch (error) {
      fail(errorMessage(error));
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
        message="AI 使用说明"
        description="使用 AI 功能时，当前 Adapter 的代码和普通配置会发送到你配置的模型服务，用于生成建议。密码、Token、密钥等敏感凭据不会发送给模型。请不要把密码或密钥直接写在 Adapter 代码中。"
        data-testid="ai-data-boundary-warning"
      />

      <label className="settings-field">
        <span>模型服务商</span>
        <Select<AiProvider>
          data-testid="ai-provider"
          disabled={actionBusy}
          value={form.provider}
          options={PROVIDER_OPTIONS}
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
        <span>基础 URL</span>
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
          填写模型服务根 URL；末尾带或不带 /v1 均可。
        </Typography.Text>
      </label>

      <label className="settings-field">
        <span>API Key 凭据（可选，仅 token 类型）</span>
        <Select<number>
          data-testid="ai-credential"
          disabled={actionBusy}
          allowClear
          placeholder="无鉴权 / 选择 token 凭据"
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
        <span>模型 ID</span>
        <Input
          data-testid="ai-model-input"
          disabled={actionBusy}
          list="ai-model-suggestions"
          placeholder="可从模型列表选择，也可手工输入"
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
          刷新模型
        </Button>
        <Typography.Text type="secondary">
          刷新失败不影响手工输入；刷新成功也不会自动切换已保存模型 ID。
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
            label: `高级：推理策略（${
              form.reasoning_mode === "default"
                ? "跟随模型默认"
                : form.reasoning_mode === "enabled"
                  ? "开启"
                  : "关闭"
            }）`,
            children: (
              <div className="settings-advanced">
                <label className="settings-field">
                  <span>推理策略</span>
                  <Select<AiReasoningMode>
                    data-testid="ai-reasoning-mode"
                    disabled={actionBusy}
                    value={form.reasoning_mode}
                    options={[
                      { label: "跟随模型默认", value: "default" },
                      { label: "开启推理", value: "enabled" },
                      { label: "关闭推理", value: "disabled" },
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
                    <span>{form.provider === "openai" ? "推理强度" : "推理强度（可选）"}</span>
                    <Select<AiReasoningEffort>
                      data-testid="ai-reasoning-effort"
                      disabled={actionBusy}
                      allowClear={form.provider !== "openai"}
                      placeholder={
                        form.provider === "openai" ? "请选择推理强度" : "不覆盖模型默认强度"
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
                  “跟随模型默认”不会主动发送 reasoning 开关。显式配置若不受支持，服务端会返回 ai_reasoning_unsupported，不会静默忽略。
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
          测试连接
        </Button>
        <Button
          type="primary"
          data-testid="ai-save-settings"
          loading={saving}
          disabled={actionBusy}
          onClick={() => void handleSave()}
        >
          保存
        </Button>
      </Space>
    </div>
  );
}
