/** One global active AI model setting (M4); Credential values never enter this component. */

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Select, Space, Spin, Typography } from "antd";

import { ApiError, api } from "../api";
import type {
  AiModelSetting,
  AiModelSettingDraft,
  AiProvider,
  AiReasoningEffort,
  AiReasoningMode,
  Credential,
} from "../types";

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
  { label: "Custom OpenAI-compatible", value: "custom_openai_compatible" },
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
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "AI 设置请求失败";
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
  const reasoningEfforts = supportedReasoningEfforts(form.provider, form.reasoning_mode);

  const fail = useCallback(
    (message: string) => {
      setPanelError(message);
      onError(message);
    },
    [onError],
  );

  const load = useCallback(async () => {
    setLoading(true);
    const [settingResult, credentialsResult] = await Promise.allSettled([
      api.getAiSetting(),
      api.listCredentials(),
    ]);

    if (settingResult.status === "fulfilled") {
      setForm(normalizeSetting(settingResult.value));
    } else if (
      !(settingResult.reason instanceof ApiError) ||
      settingResult.reason.code !== "ai_not_configured"
    ) {
      fail(errorMessage(settingResult.reason));
    }

    if (credentialsResult.status === "fulfilled") {
      setCredentials(credentialsResult.value.filter((credential) => credential.type === "token"));
    } else {
      fail(errorMessage(credentialsResult.reason));
    }
    setLoading(false);
  }, [fail]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- tab mount intentionally loads global settings
    void load();
  }, [load]);

  function currentPayload(): AiModelSettingDraft | null {
    const baseUrl = form.base_url.trim();
    const model = form.model.trim();
    if (baseUrl === "" || model === "") {
      fail("Base URL 与 Model ID 均不能为空");
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
    const baseUrl = form.base_url.trim();
    if (baseUrl === "") {
      fail("刷新模型前请填写 Base URL");
      return;
    }
    setRefreshingModels(true);
    setPanelError(null);
    setNotice(null);
    try {
      const response = await api.refreshAiModels({
        provider: form.provider,
        base_url: baseUrl,
        credential_id: form.credential_id,
      });
      setModelOptions(response.models);
      setNotice(`已刷新 ${response.models.length} 个 Model ID；不会自动更改当前选择。`);
    } catch (error) {
      fail(`${errorMessage(error)}；仍可手工输入 Model ID。`);
    } finally {
      setRefreshingModels(false);
    }
  }

  async function handleSave() {
    const payload = currentPayload();
    if (payload === null || saving) {
      return;
    }
    setSaving(true);
    setPanelError(null);
    setNotice(null);
    try {
      const saved = await api.updateAiSetting(payload);
      setForm(normalizeSetting(saved));
      setNotice("AI 模型设置已保存。Model ID 仅在管理员再次修改时才会变化。");
    } catch (error) {
      fail(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    const payload = currentPayload();
    if (payload === null || testing) {
      return;
    }
    setTesting(true);
    setPanelError(null);
    setNotice(null);
    try {
      const result = await api.testAiSetting(payload);
      if (result.ok) {
        setNotice(result.message?.trim() || "连接测试通过：模型返回可解析的最小响应。");
      } else {
        fail(result.message?.trim() || "连接测试失败");
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
        type="warning"
        showIcon
        message="外部模型数据边界"
        description="当前 Working Copy 与非敏感运行参数会发送到这里配置的模型服务。Credential 真值不会返回浏览器；请勿在 Adapter 代码中硬编码 Secret。"
        data-testid="ai-data-boundary-warning"
      />

      <label className="settings-field">
        <span>Provider</span>
        <Select<AiProvider>
          data-testid="ai-provider"
          value={form.provider}
          options={PROVIDER_OPTIONS}
          onChange={(provider) =>
            setForm((current) => ({
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
        <span>Base URL</span>
        <Input
          data-testid="ai-base-url"
          placeholder="https://api.example.com"
          value={form.base_url}
          onChange={(event) =>
            setForm((current) => ({ ...current, base_url: event.target.value }))
          }
        />
        <Typography.Text type="secondary">
          填写模型服务根 URL；末尾带或不带 /v1 均可。
        </Typography.Text>
      </label>

      <label className="settings-field">
        <span>API Key Credential（可选，仅 token 类型）</span>
        <Select<number>
          data-testid="ai-credential"
          allowClear
          placeholder="无鉴权 / 选择 token Credential"
          value={form.credential_id ?? undefined}
          options={credentials.map((credential) => ({
            label: credential.name,
            value: credential.id,
          }))}
          onChange={(credentialId) =>
            setForm((current) => ({ ...current, credential_id: credentialId ?? null }))
          }
        />
      </label>

      <label className="settings-field">
        <span>Model ID</span>
        <Input
          data-testid="ai-model-input"
          list="ai-model-suggestions"
          placeholder="可从模型列表选择，也可手工输入"
          value={form.model}
          onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
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
          onClick={() => void handleRefreshModels()}
        >
          刷新模型
        </Button>
        <Typography.Text type="secondary">
          刷新失败不影响手工输入；刷新成功也不会自动切换已保存 Model ID。
        </Typography.Text>
      </Space>

      <label className="settings-field">
        <span>推理策略</span>
        <Select<AiReasoningMode>
          data-testid="ai-reasoning-mode"
          value={form.reasoning_mode}
          options={[
            { label: "跟随模型默认", value: "default" },
            { label: "开启推理", value: "enabled" },
            { label: "关闭推理", value: "disabled" },
          ]}
          onChange={(reasoningMode) =>
            setForm((current) => ({
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
            allowClear={form.provider !== "openai"}
            placeholder={
              form.provider === "openai" ? "请选择推理强度" : "不覆盖模型默认强度"
            }
            value={form.reasoning_effort ?? undefined}
            options={reasoningEfforts.map((effort) => ({ label: effort, value: effort }))}
            onChange={(reasoningEffort) =>
              setForm((current) => ({ ...current, reasoning_effort: reasoningEffort ?? null }))
            }
          />
        </label>
      )}

      <Typography.Text type="secondary">
        “跟随模型默认”不会主动发送 reasoning 开关。显式配置若不受支持，服务端会返回 ai_reasoning_unsupported，不会静默忽略。
      </Typography.Text>

      {panelError !== null && (
        <p className="settings-panel-error" role="alert" data-testid="ai-settings-error">
          {panelError}
        </p>
      )}
      {notice !== null && (
        <p className="settings-panel-success" data-testid="ai-settings-notice">
          {notice}
        </p>
      )}

      <Space>
        <Button
          data-testid="ai-test-connection"
          loading={testing}
          onClick={() => void handleTest()}
        >
          测试连接
        </Button>
        <Button
          type="primary"
          data-testid="ai-save-settings"
          loading={saving}
          onClick={() => void handleSave()}
        >
          保存
        </Button>
      </Space>
    </div>
  );
}
