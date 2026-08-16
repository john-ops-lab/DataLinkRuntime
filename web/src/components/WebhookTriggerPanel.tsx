/** Webhook Adapter final runtime settings: URL, token, Worker and receive state. */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useState } from "react";
import { Alert, Button, Input, Select, Space, Spin, Tag, Typography } from "antd";

import { ApiError, api } from "../api";
import type { Adapter, AdapterWebhook, Credential, Worker } from "../types";
import { userErrorMessage } from "../user-message";

const PATH_PATTERN = /^[a-z0-9][a-z0-9-]{2,63}$/;

function errorMessage(error: unknown, publicId: string): string {
  if (error instanceof ApiError) {
    if (error.code === "webhook_path_in_use") {
      return `Webhook 地址 ${publicId} 当前正在被另一个运行中的适配器使用，请先停止旧适配器后再启动当前适配器。`;
    }
    if (error.code === "webhook_credential_type_invalid") {
      return "Webhook 只能绑定 token 类型的凭据";
    }
    if (error.code === "webhook_path_invalid") {
      return "Webhook 路径只允许 3–64 位小写字母、数字和连字符，且必须以字母或数字开头。";
    }
    return userErrorMessage(error);
  }
  return userErrorMessage(error);
}

interface Props {
  adapter: Adapter;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  onAdapterChange: (adapter: Adapter) => void;
  onReceivingChange: (enabled: boolean) => void;
  onRuntimeStateChange: (state: WebhookRuntimeState) => void;
  onError: (message: string | null) => void;
}

export interface WebhookRuntimeState {
  loaded: boolean;
  enabled: boolean;
  runtimeLocked: boolean;
  changingState: boolean;
  startBlockedReason: string | null;
}

export interface WebhookTriggerHandle {
  toggleReceiving: () => void;
}

const WebhookTriggerPanel = forwardRef<WebhookTriggerHandle, Props>(function WebhookTriggerPanel(props, ref) {
  const adapterId = props.adapter.id;
  const onAdapterChange = props.onAdapterChange;
  const onError = props.onError;
  const onRuntimeStateChange = props.onRuntimeStateChange;
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [changingState, setChangingState] = useState(false);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [saved, setSaved] = useState<AdapterWebhook | null>(null);
  const [publicId, setPublicId] = useState("");
  const [credentialId, setCredentialId] = useState<number | null>(null);
  const [workerId, setWorkerId] = useState<number | null>(props.adapter.runtime_worker_id ?? null);
  const [notice, setNotice] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);

  const tokenCredentials = useMemo(
    () => credentials.filter((credential) => credential.type === "token"),
    [credentials],
  );
  const compatibleWorkers = props.workers.filter((worker) =>
    worker.capabilities.includes(props.adapter.language),
  );

  const load = useCallback(async () => {
    setLoading(true);
    onError(null);
    try {
      const [credentialList, webhook, adapter] = await Promise.all([
        api.listCredentials(),
        api.getWebhook(adapterId),
        api.getAdapter(adapterId),
      ]);
      setCredentials(credentialList);
      setSaved(webhook);
      setPublicId(webhook.public_id);
      setCredentialId(webhook.credential_id);
      setWorkerId(adapter.runtime_worker_id ?? null);
      onAdapterChange(adapter);
    } catch (error) {
      onError(errorMessage(error, ""));
    } finally {
      setLoading(false);
    }
  }, [adapterId, onAdapterChange, onError]);

  useEffect(() => {
    void load();
    // publicId changes are local edits and must not reload the form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapterId]);

  const archived = !!props.adapter.archived_at;
  const enabled = saved?.enabled === true;
  // Treat enabled as locked immediately, even before the follow-up Adapter
  // refresh returns. The backend remains authoritative for active calls after
  // Stop, which are represented by adapter.runtime_locked.
  const runtimeLocked = enabled || props.adapter.runtime_locked === true;
  const pathValid = PATH_PATTERN.test(publicId);
  // M5.3 generated token_urlsafe paths that may contain uppercase letters or
  // underscores. Preserve an unchanged legacy URL so an upgraded Webhook can
  // still Stop/Start; once edited, the final path contract applies.
  const unchangedLegacyPath =
    saved !== null && saved.public_id === publicId && !pathValid;
  const pathAcceptable = pathValid || unchangedLegacyPath;
  const dirty =
    saved !== null &&
    (saved.public_id !== publicId ||
      saved.credential_id !== credentialId ||
      (props.adapter.runtime_worker_id ?? null) !== workerId);
  const canConfigure = !archived && !runtimeLocked && !saving && !changingState;
  const startBlockedReason =
    props.adapter.latest_version_id === null
      ? "请先保存适配器。"
      : workerId === null
        ? "请先选择并保存运行节点。"
        : credentialId === null
          ? "请先选择并保存 Token 凭据。"
          : dirty
            ? "运行设置有未保存修改，请先保存。"
            : null;
  const gatewayPrefix = `${window.location.origin}/api/hooks/`;
  const fullUrl = gatewayPrefix + publicId;

  useEffect(() => {
    onRuntimeStateChange({
      loaded: !loading && saved !== null,
      enabled,
      runtimeLocked,
      changingState,
      startBlockedReason,
    });
  }, [
    changingState,
    enabled,
    loading,
    onRuntimeStateChange,
    runtimeLocked,
    saved,
    startBlockedReason,
  ]);

  useImperativeHandle(ref, () => ({
    toggleReceiving: () => void setReceiving(!enabled),
  }));

  async function saveConfiguration() {
    if (!canConfigure || !pathAcceptable || workerId === null) return;
    setSaving(true);
    setNotice(null);
    props.onError(null);
    try {
      const adapter = await api.updateAdapter(adapterId, { runtime_worker_id: workerId });
      const webhook = await api.putWebhook(adapterId, {
        enabled: false,
        public_id: publicId,
        credential_id: credentialId,
      });
      setSaved(webhook);
      props.onAdapterChange(adapter);
      setNotice("运行设置已保存。");
    } catch (error) {
      props.onError(errorMessage(error, publicId));
    } finally {
      setSaving(false);
    }
  }

  async function setReceiving(nextEnabled: boolean) {
    if (saved === null || changingState) return;
    setChangingState(true);
    setNotice(null);
    props.onError(null);
    try {
      const webhook = await api.putWebhook(adapterId, {
        enabled: nextEnabled,
        public_id: saved.public_id,
        credential_id: saved.credential_id,
      });
      setSaved(webhook);
      // A successful state transition must lock the UI conservatively while
      // the derived Adapter runtime state is refreshed. If that refresh fails,
      // Refresh can reconcile without exposing an unsafe edit window.
      props.onAdapterChange({ ...props.adapter, runtime_locked: true });
      props.onReceivingChange(nextEnabled);
      const adapter = await api.getAdapter(adapterId);
      props.onAdapterChange(adapter);
      setNotice(nextEnabled ? "已开启接收。" : "已停止接收；已有调用会继续运行到终态。");
    } catch (error) {
      props.onError(errorMessage(error, saved.public_id));
    } finally {
      setChangingState(false);
    }
  }

  async function copyUrl() {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(fullUrl);
      setNotice("Webhook 地址已复制到剪贴板。");
    } catch {
      setCopyError("复制失败，请手动选择完整地址复制。");
    }
  }

  if (loading || saved === null) {
    return <div className="webhook-trigger-panel" data-testid="webhook-loading"><Spin /></div>;
  }

  return (
    <div className="webhook-trigger-panel" data-testid="webhook-run-settings">
      <Typography.Title level={5}>Webhook 运行设置</Typography.Title>
      <Space direction="vertical" size="middle" className="webhook-form">
        <div className="settings-field">
          <span className="settings-field-label">接收状态</span>
          <Tag color={enabled ? "green" : "default"}>{enabled ? "接收中" : "已停止"}</Tag>
        </div>
        {runtimeLocked && (
          <Alert
            type="warning"
            showIcon
            data-testid="webhook-runtime-locked"
            message="运行配置已锁定"
            description={enabled
              ? "请先停止接收；如果仍有活跃执行，需等待其进入终态后才能修改。"
              : "已有调用仍在运行；进入终态后刷新即可修改 URL、Token、运行节点、代码和依赖。"}
          />
        )}
        <div className="settings-field">
          <span className="settings-field-label">Webhook URL</span>
          <Space.Compact style={{ width: "100%", maxWidth: 760 }}>
            <Input data-testid="webhook-prefix" readOnly value={gatewayPrefix} />
            <Input
              data-testid="webhook-public-id"
              aria-label="Webhook 路径"
              value={publicId}
              disabled={!canConfigure}
              onChange={(event) => { setPublicId(event.target.value); setNotice(null); }}
            />
            <Button data-testid="webhook-copy" onClick={() => void copyUrl()}>复制</Button>
          </Space.Compact>
          <Typography.Text type="secondary">
            系统已自动生成随机地址，也可以改成便于识别的路径，例如 receive-sys1-data。
          </Typography.Text>
          {!pathValid && !unchangedLegacyPath && (
            <Alert type="error" showIcon data-testid="webhook-path-invalid" message="只允许 3–64 位小写字母、数字和连字符，且必须以字母或数字开头。" />
          )}
          {unchangedLegacyPath && (
            <Alert
              type="warning"
              showIcon
              data-testid="webhook-path-legacy"
              message="这是升级前创建的兼容地址；未修改时可继续启停。编辑后必须使用小写字母、数字和连字符。"
            />
          )}
          <Input data-testid="webhook-url" readOnly value={fullUrl} onFocus={(event) => event.target.select()} />
        </div>
        <label className="settings-field">
          <span className="settings-field-label">Token 凭据</span>
          <Select
            data-testid="webhook-credential"
            value={credentialId ?? undefined}
            placeholder="选择 token 类型凭据"
            disabled={!canConfigure}
            options={tokenCredentials.map((credential) => ({ label: credential.name, value: credential.id }))}
            onChange={(value) => { setCredentialId(value); setNotice(null); }}
          />
        </label>
        {tokenCredentials.length === 0 && <Alert type="warning" showIcon message="尚无 token 类型凭据，请先在系统设置中创建。" />}
        <label className="settings-field">
          <span className="settings-field-label">运行节点</span>
          <Select
            data-testid="webhook-runtime-worker"
            value={workerId ?? undefined}
            placeholder="选择支持当前语言的运行节点"
            loading={props.workersLoading}
            disabled={!canConfigure || props.workersLoading}
            options={compatibleWorkers.map((worker) => ({
              label: `${worker.name}（${worker.status === "online" ? "在线" : "离线"}）`,
              value: worker.id,
              disabled: worker.status !== "online",
            }))}
            onChange={(value) => { setWorkerId(value); setNotice(null); }}
          />
        </label>
        {props.workersError !== null && <Alert type="error" showIcon message={props.workersError} />}
        {copyError !== null && <Alert type="warning" showIcon message={copyError} />}
        {notice !== null && <Alert type="success" showIcon data-testid="webhook-notice" message={notice} />}
        <Space>
          <Button
            data-testid="webhook-save"
            loading={saving}
            disabled={!canConfigure || !pathAcceptable || workerId === null || !dirty}
            onClick={() => void saveConfiguration()}
          >保存运行设置</Button>
          {enabled ? (
            <Button danger data-testid="webhook-stop" loading={changingState} onClick={() => void setReceiving(false)}>停止接收</Button>
          ) : (
            <Button
              type="primary"
              data-testid="webhook-start"
              loading={changingState}
              disabled={archived || runtimeLocked || startBlockedReason !== null}
              onClick={() => void setReceiving(true)}
            >开启接收</Button>
          )}
          <Button onClick={() => void load()}>刷新</Button>
        </Space>
        {!enabled && startBlockedReason !== null && <Alert type="info" showIcon data-testid="webhook-start-blocked" message={startBlockedReason} />}
      </Space>
    </div>
  );
});

export default WebhookTriggerPanel;
