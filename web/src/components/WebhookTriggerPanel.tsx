/** 触发器 Tab：M5.3 Webhook 配置区（与 Schedule 区域并列）。 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Input, Select, Space, Spin, Switch, Typography } from "antd";

import { ApiError, api } from "../api";
import type { AdapterWebhook, Credential } from "../types";

/** 后端校验错误 code → 稳定的中文说明。 */
function validationMessage(error: ApiError): string {
  if (error.code === "webhook_credential_type_invalid") {
    return "Webhook 只能绑定 token 类型的凭据";
  }
  if (error.code === "credential_not_found") {
    return "所选凭据不存在，请重新选择";
  }
  if (error.code === "adapter_archived") {
    return "Adapter 已归档，Webhook 为只读";
  }
  return `${error.message} (${error.code})`;
}

interface WebhookTriggerPanelProps {
  adapterId: number;
  productionState: "idle" | "running" | "stopped";
  /** 已归档 Adapter 只读：可查看配置但禁用编辑与保存（与服务端 409 adapter_archived 对齐）。 */
  archived: boolean;
}

export default function WebhookTriggerPanel(props: WebhookTriggerPanelProps) {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);

  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [saved, setSaved] = useState<AdapterWebhook | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [credentialId, setCredentialId] = useState<number | null>(null);

  const tokenCredentials = useMemo(
    () => credentials.filter((credential) => credential.type === "token"),
    [credentials],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [credentialList, webhookResult] = await Promise.allSettled([
        api.listCredentials(),
        api.getWebhook(props.adapterId),
      ]);
      if (credentialList.status === "fulfilled") {
        setCredentials(credentialList.value);
      } else if (!(credentialList.reason instanceof ApiError)) {
        throw credentialList.reason;
      }
      if (webhookResult.status === "fulfilled") {
        const webhook = webhookResult.value;
        setSaved(webhook);
        setEnabled(webhook.enabled);
        setCredentialId(webhook.credential_id);
        return;
      }
      // 未配置是合法初始状态：用默认值初始化表单，不当作错误展示。
      if (
        webhookResult.reason instanceof ApiError &&
        webhookResult.reason.code === "webhook_not_configured"
      ) {
        setSaved(null);
        setEnabled(false);
        setCredentialId(null);
        return;
      }
      throw webhookResult.reason;
    } catch (error) {
      setLoadError(error instanceof ApiError ? `${error.message} (${error.code})` : "请求失败");
    } finally {
      setLoading(false);
    }
  }, [props.adapterId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载时拉取 Webhook 配置的初始加载是有意的异步同步
    void load();
  }, [load]);

  async function handleSave() {
    if (saving || credentialId === null) {
      return;
    }
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    try {
      const stored = await api.putWebhook(props.adapterId, {
        enabled,
        credential_id: credentialId,
      });
      setSaved(stored);
      setEnabled(stored.enabled);
      setCredentialId(stored.credential_id);
      setNotice("Webhook 已保存；地址保持稳定，外部系统可立即使用。");
    } catch (error) {
      setSaveError(error instanceof ApiError ? validationMessage(error) : "请求失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleCopy() {
    if (saved === null) {
      return;
    }
    const fullUrl = window.location.origin + saved.hook_path;
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(fullUrl);
      setNotice("Webhook 地址已复制到剪贴板。");
    } catch {
      setCopyError("复制失败：请手动选择地址文本复制。");
    }
  }

  if (loading) {
    return (
      <div className="webhook-trigger-panel" data-testid="webhook-loading">
        <Spin />
      </div>
    );
  }

  return (
    <div className="webhook-trigger-panel">
      <Typography.Title level={5}>Webhook（事件触发）</Typography.Title>
      <Typography.Paragraph type="secondary">
        一个 Adapter 最多一个 Webhook：外部系统携带 Bearer Token POST JSON 到统一入口，Control
        校验后立即返回 202 并异步执行；生产入口关闭或存在运行中任务时直接拒绝，不排队。
      </Typography.Paragraph>
      {loadError !== null && (
        <Alert type="error" showIcon message={loadError} data-testid="webhook-load-error" />
      )}

      {props.archived && (
        <Alert
          type="warning"
          showIcon
          message="Adapter 已归档，Webhook 为只读：可查看配置，但无法编辑或保存。"
          data-testid="webhook-archived-hint"
        />
      )}

      {saved !== null && props.productionState !== "running" && (
        <Alert
          type="info"
          showIcon
          message="Webhook 已配置，但生产入口当前关闭；外部调用会被拒绝。"
          data-testid="webhook-production-closed-hint"
        />
      )}

      <Space direction="vertical" size="middle" className="webhook-form">
        <label className="settings-field">
          <span className="settings-field-label">启用</span>
          <Switch
            data-testid="webhook-enabled"
            checked={enabled}
            disabled={props.archived}
            onChange={(value) => {
              setEnabled(value);
              setNotice(null);
            }}
          />
        </label>
        <label className="settings-field">
          <span className="settings-field-label">Token 凭据</span>
          <Select
            data-testid="webhook-credential"
            style={{ minWidth: 240 }}
            placeholder="选择 token 类型凭据"
            value={credentialId ?? undefined}
            disabled={props.archived}
            options={tokenCredentials.map((credential) => ({
              label: credential.name,
              value: credential.id,
            }))}
            onChange={(value) => {
              setCredentialId(value);
              setNotice(null);
            }}
          />
        </label>
        {tokenCredentials.length === 0 && (
          <Alert
            type="warning"
            showIcon
            message="尚无 token 类型凭据：请先在系统设置中创建一个。"
            data-testid="webhook-no-token-credential"
          />
        )}

        {saved !== null && (
          <>
            <div className="settings-field">
              <span className="settings-field-label">Webhook 地址</span>
              <Space.Compact style={{ width: "100%", maxWidth: 720 }}>
                <Input
                  data-testid="webhook-url"
                  readOnly
                  value={window.location.origin + saved.hook_path}
                  onFocus={(event) => event.target.select()}
                />
                <Button data-testid="webhook-copy" onClick={() => void handleCopy()}>
                  复制
                </Button>
              </Space.Compact>
            </div>
            {copyError !== null && (
              <Alert type="warning" showIcon message={copyError} data-testid="webhook-copy-error" />
            )}
            <div className="settings-field">
              <span className="settings-field-label">示例请求</span>
              <pre className="webhook-example" data-testid="webhook-example">
                {[
                  `POST ${saved.hook_path}`,
                  "Authorization: Bearer <token>",
                  "Content-Type: application/json",
                  "",
                  "{",
                  '  "event": "vm.created",',
                  '  "data": {}',
                  "}",
                ].join("\n")}
              </pre>
              <Typography.Text type="secondary">
                {"<token> 为所选凭据中的 token 值；浏览器不显示其真值。"}
              </Typography.Text>
            </div>
          </>
        )}

        {saveError !== null && (
          <Alert type="error" showIcon message={saveError} data-testid="webhook-error" />
        )}
        {notice !== null && (
          <Alert type="success" showIcon message={notice} data-testid="webhook-notice" />
        )}
        <div>
          <Button
            type="primary"
            data-testid="webhook-save"
            loading={saving}
            disabled={props.archived || credentialId === null}
            onClick={() => void handleSave()}
          >
            保存
          </Button>
        </div>
      </Space>
    </div>
  );
}
