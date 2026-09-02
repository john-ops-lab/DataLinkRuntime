/** Webhook Adapter final runtime settings (M5.5.12 / M5.11 Wave C).
 *
 * 运行设置只保留五类字段：Webhook 路径、完整地址、入口 Bearer Token、运行节点、
 * 单次执行超时（复用 #57 的 Adapter 级 timeout_seconds，不另造实现）。
 * 开启/停止接收只保留在 Header 右上角，本页不重复出现；接收中（或仍有
 * 活跃调用）配置锁定但保持可读，用锁图标表达，不把文本灰到难以阅读。
 *
 * 停止接收交互（#58）：
 * - 无 active Webhook Execution 时直接停止接收；
 * - 有 active Webhook Execution 时弹出三选一：直接结束当前调用（复用
 *   已有 Execution cancel 机制）/ 等待调用结束 / 取消；
 * - 无论选择哪种，新请求都立即停止接收；区别只在当前调用是否被取消；
 * - cancel 失败时保持真实状态（已停止接收、调用仍在执行），绝不偷偷
 *   重新开启 Webhook。
 */

import { forwardRef, useEffect, useImperativeHandle, useMemo, useState } from "react";
import { Alert, Button, Input, InputNumber, Modal, Radio, Select, Space, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { i18n } from "../i18n";

import { ApiError, api } from "../api";
import { subscribeCredentialCatalog } from "../credential-catalog";
import type { Adapter, AdapterWebhook, Credential, Worker } from "../types";
import { userErrorMessage } from "../user-message";

const PATH_PATTERN = /^[a-z0-9][a-z0-9-]{2,63}$/;

// M5.5.11 单次执行超时合同（秒为权威值）：Webhook 与 Task 共用 Adapter 级能力。
const DEFAULT_TIMEOUT_SECONDS = 300;
const MAX_TIMEOUT_SECONDS = 24 * 60 * 60; // 24 小时
const TIMEOUT_PRESET_MINUTES = [1, 5, 10, 30, 60] as const;

/** Stable runtime-namespace translator for effects and subscriptions, so a
 * locale switch never re-runs the webhook load effect (which would discard
 * in-progress edits). */
function runtimeTranslate(key: string, options?: Record<string, unknown>): string {
  return i18n.t(key, { ns: "runtime", ...options });
}

function errorMessage(
  error: unknown,
  publicId: string,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  if (error instanceof ApiError) {
    if (error.code === "webhook_path_in_use") {
      return translate("webhook.settings.pathInUse", { path: publicId });
    }
    if (error.code === "webhook_credential_type_invalid") {
      return translate("webhook.settings.credentialTypeInvalid");
    }
    if (error.code === "webhook_path_invalid") {
      return translate("webhook.settings.pathInvalid");
    }
    return userErrorMessage(error);
  }
  return userErrorMessage(error);
}

/** 接收中锁定字段的小锁图标（不引入额外图标依赖）。 */
function LockGlyph() {
  return (
    <svg
      className="webhook-lock-glyph"
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

/** 锁定字段的只读展示：可读、不可编辑、带锁图标。 */
function LockedValue({ testId, children }: { testId: string; children: React.ReactNode }) {
  return (
    <div className="webhook-locked-value" data-testid={testId}>
      <LockGlyph />
      <span>{children}</span>
    </div>
  );
}

function formatTimeout(
  seconds: number,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  const minutes = seconds / 60;
  return Number.isInteger(minutes)
    ? translate("units.minutes", { value: minutes })
    : translate("units.seconds", { value: seconds });
}

function presetMinutesFor(seconds: number): number | undefined {
  return TIMEOUT_PRESET_MINUTES.find((minutes) => minutes * 60 === seconds);
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
  readOnly?: boolean;
  canManageCredentials?: boolean;
  /** Account entry uses the Adapter-scoped metadata endpoint; Token entry
   * keeps the existing global admin endpoint. */
  useScopedCredentialOptions?: boolean;
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
  const { t } = useTranslation(["runtime", "common"]);
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
  // M5.5.11: 表单内超时值（秒）；null = 跟随 Adapter 保存值。
  const [timeoutOverride, setTimeoutOverride] = useState<number | null>(null);
  const [timeoutCustomMode, setTimeoutCustomMode] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [stopDialogOpen, setStopDialogOpen] = useState(false);

  const tokenCredentials = useMemo(
    () => credentials.filter((credential) => credential.type === "token"),
    [credentials],
  );
  const compatibleWorkers = props.workers.filter((worker) =>
    worker.capabilities.includes(props.adapter.language),
  );

  useEffect(() => {
    // 面板按 adapter key 重挂载：loading 初始为 true，加载完成后清除。
    let cancelled = false;
    void (async () => {
      onError(null);
      try {
        const [credentialList, webhook, adapter] = await Promise.all([
          props.canManageCredentials === true
            ? props.useScopedCredentialOptions === true
              ? api.listAdapterCredentialOptions(adapterId)
              : api.listCredentials()
            : Promise.resolve([]),
          api.getWebhook(adapterId),
          api.getAdapter(adapterId),
        ]);
        if (cancelled) {
          return;
        }
        setCredentials(credentialList);
        setSaved(webhook);
        setPublicId(webhook.public_id);
        setCredentialId(webhook.credential_id);
        setWorkerId(adapter.runtime_worker_id ?? null);
        setTimeoutOverride(null);
        setTimeoutCustomMode(false);
        onAdapterChange(adapter);
      } catch (error) {
        if (!cancelled) {
           onError(errorMessage(error, "", runtimeTranslate));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // publicId changes are local edits and must not reload the form.
  }, [
    adapterId,
    onAdapterChange,
    onError,
    props.canManageCredentials,
    props.useScopedCredentialOptions,
  ]);

  // 凭据增删改后仅刷新 token 凭据选项（UX-003）；不会重载 Webhook 配置，
  // 未保存的本地编辑保持不变。
  useEffect(
    () =>
      subscribeCredentialCatalog(() => {
        if (props.canManageCredentials !== true) {
          return;
        }
        void (props.useScopedCredentialOptions === true
          ? api.listAdapterCredentialOptions(adapterId)
          : api.listCredentials())
          .then((credentialList) => setCredentials(credentialList))
          .catch((error) => onError(errorMessage(error, "", runtimeTranslate)));
      }),
    [adapterId, onError, props.canManageCredentials, props.useScopedCredentialOptions],
  );

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
  // M5.5.11: 表单显示值 = 表单覆盖 ?? Adapter 权威值 ?? 默认 300 秒。
  const effectiveTimeoutSeconds =
    timeoutOverride ?? props.adapter.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS;
  const effectiveCustom =
    timeoutCustomMode || presetMinutesFor(effectiveTimeoutSeconds) === undefined;
  const timeoutDirty =
    (props.adapter.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS) !== effectiveTimeoutSeconds;
  const dirty =
    saved !== null &&
    (saved.public_id !== publicId ||
      saved.credential_id !== credentialId ||
      (props.adapter.runtime_worker_id ?? null) !== workerId ||
      timeoutDirty);
  const canConfigure = !props.readOnly && !archived && !runtimeLocked && !saving && !changingState;
  const startBlockedReason =
    props.adapter.latest_version_id === null
      ? t("webhook.reasons.noVersion")
      : workerId === null
        ? t("webhook.reasons.noWorker")
        : credentialId === null
          ? t("webhook.reasons.noCredential")
          : dirty
            ? t("webhook.reasons.dirty")
            : null;
  const gatewayPrefix = `${window.location.origin}/api/hooks/`;
  const fullUrl = gatewayPrefix + publicId;
  const credentialName =
    saved?.credential_name ??
    tokenCredentials.find((credential) => credential.id === credentialId)?.name ??
     t("labels.notSelected", { ns: "common" });
  const workerName =
    props.workers.find((worker) => worker.id === workerId)?.name ?? t("labels.notSelected", { ns: "common" });

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

  async function handleToggleReceiving() {
    if (props.readOnly || saved === null || changingState) return;
    if (!enabled) {
      await startReceiving();
      return;
    }
    // M5.5.12：有 active Webhook Execution 时让用户选择如何处理当前调用；
    // 无 active 调用时直接停止，不弹选择框。
    if (props.adapter.running_execution_id != null) {
      setStopDialogOpen(true);
    } else {
      await performStop();
    }
  }

  useImperativeHandle(ref, () => ({
    toggleReceiving: () => void handleToggleReceiving(),
  }));

  function resolveTimeoutSeconds(): number | null {
    const minutes = presetMinutesFor(effectiveTimeoutSeconds);
    if (!effectiveCustom && minutes !== undefined) {
      return minutes * 60;
    }
    if (
      !Number.isInteger(effectiveTimeoutSeconds) ||
      effectiveTimeoutSeconds < 1 ||
      effectiveTimeoutSeconds > MAX_TIMEOUT_SECONDS
    ) {
      onError(t("webhook.settings.timeoutInvalid", { max: MAX_TIMEOUT_SECONDS }));
      return null;
    }
    return effectiveTimeoutSeconds;
  }

  async function saveConfiguration() {
    if (!canConfigure || !pathAcceptable) return;
    const timeoutSeconds = resolveTimeoutSeconds();
    if (timeoutSeconds === null) return;
    setSaving(true);
    setNotice(null);
    props.onError(null);
    try {
      const adapter = await api.updateAdapter(
        adapterId,
        workerId === null
          ? { timeout_seconds: timeoutSeconds }
          : { runtime_worker_id: workerId, timeout_seconds: timeoutSeconds },
      );
      const webhook = await api.putWebhook(adapterId, {
        enabled: false,
        public_id: publicId,
        credential_id: credentialId,
      });
      setSaved(webhook);
      props.onAdapterChange(adapter);
      setTimeoutOverride(null);
      setTimeoutCustomMode(false);
    } catch (error) {
       props.onError(errorMessage(error, publicId, (key, options) => t(key, options)));
    } finally {
      setSaving(false);
    }
  }

  /** 立即停止接收新请求（等待调用结束 / 直接结束两条路径都先执行这一步）。 */
  async function performStop(): Promise<boolean> {
    if (saved === null || changingState) return false;
    setChangingState(true);
    setNotice(null);
    props.onError(null);
    try {
      const webhook = await api.putWebhook(adapterId, {
        enabled: false,
        public_id: saved.public_id,
        credential_id: saved.credential_id,
      });
      setSaved(webhook);
      // A successful state transition must lock the UI conservatively while
      // the derived Adapter runtime state is refreshed. If that refresh fails,
      // polling can reconcile without exposing an unsafe edit window.
      props.onAdapterChange({ ...props.adapter, runtime_locked: true });
      props.onReceivingChange(false);
      const adapter = await api.getAdapter(adapterId);
      props.onAdapterChange(adapter);
      return true;
    } catch (error) {
       props.onError(errorMessage(error, saved.public_id, (key, options) => t(key, options)));
      return false;
    } finally {
      setChangingState(false);
    }
  }

  async function startReceiving() {
    if (saved === null || changingState) return;
    setChangingState(true);
    setNotice(null);
    props.onError(null);
    try {
      const webhook = await api.putWebhook(adapterId, {
        enabled: true,
        public_id: saved.public_id,
        credential_id: saved.credential_id,
      });
      setSaved(webhook);
      props.onAdapterChange({ ...props.adapter, runtime_locked: true });
      props.onReceivingChange(true);
      const adapter = await api.getAdapter(adapterId);
      props.onAdapterChange(adapter);
    } catch (error) {
       props.onError(errorMessage(error, saved.public_id, (key, options) => t(key, options)));
    } finally {
      setChangingState(false);
    }
  }

  /** 直接结束当前调用：复用已有 Execution cancel，不新造终止能力。 */
  async function cancelActiveCall() {
    const executionId = props.adapter.running_execution_id;
    if (executionId == null) return;
    setChangingState(true);
    props.onError(null);
    try {
      const execution = await api.cancelExecution(executionId);
      const adapter = await api.getAdapter(adapterId);
      props.onAdapterChange(adapter);
      // A pending Execution turns cancelled immediately; a running one gets
      // the cancel flag the Worker picks up on its next progress round trip.
      if (execution.status === "cancelled") {
         setNotice(t("webhook.settings.stoppedAndEnded"));
      }
    } catch (error) {
      // 已停止接收保持真实状态：cancel 失败绝不偷偷重新开启 Webhook。
      props.onError(
         t("webhook.settings.cancelFailed", { error: userErrorMessage(error) }),
      );
    } finally {
      setChangingState(false);
    }
  }

  async function stopAndEndCall() {
    setStopDialogOpen(false);
    if (await performStop()) {
      await cancelActiveCall();
    }
  }

  async function stopAndWaitForEnd() {
    setStopDialogOpen(false);
    await performStop();
  }

  async function copyUrl() {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(fullUrl);
       setNotice(t("webhook.settings.copy"));
    } catch {
       setCopyError(t("webhook.settings.copyFailed"));
    }
  }

  if (loading || saved === null) {
    return <div className="webhook-trigger-panel" data-testid="webhook-loading"><Spin /></div>;
  }

  return (
    <div className="webhook-trigger-panel" data-testid="webhook-run-settings">
       <Typography.Title level={5}>{t("webhook.settings.title")}</Typography.Title>
      {props.readOnly && (
        <Alert type="info" showIcon data-testid="webhook-read-only" message={t("webhook.reasons.readOnly")} />
      )}
      <Space direction="vertical" size="middle" className="webhook-form">
        <div className="settings-field">
           <span className="settings-field-label">{t("webhook.settings.path")}</span>
          {canConfigure ? (
            <>
              <Input
                data-testid="webhook-public-id"
                 aria-label={t("webhook.settings.pathAria")}
                value={publicId}
                onChange={(event) => { setPublicId(event.target.value); setNotice(null); }}
              />
              {!pathValid && !unchangedLegacyPath && (
                 <Alert type="error" showIcon data-testid="webhook-path-invalid" message={t("webhook.settings.pathInvalid")} />
              )}
              {unchangedLegacyPath && (
                <Alert
                  type="warning"
                  showIcon
                  data-testid="webhook-path-legacy"
                   message={t("webhook.settings.legacyPath")}
                />
              )}
              <Typography.Text type="secondary">
                 {t("webhook.settings.pathHint")}
              </Typography.Text>
            </>
          ) : (
            <LockedValue testId="webhook-path-locked">{publicId}</LockedValue>
          )}
        </div>
        <div className="settings-field">
           <span className="settings-field-label">{t("webhook.settings.fullUrl")}</span>
          <div className="webhook-url-control" data-testid="webhook-url-readonly">
            <Space.Compact style={{ width: "100%", maxWidth: 760 }}>
              <Input
                data-testid="webhook-url"
                 aria-label={t("webhook.settings.fullUrlAria")}
                aria-readonly="true"
                readOnly
                value={fullUrl}
                onFocus={(event) => event.target.select()}
              />
               <Button data-testid="webhook-copy" onClick={() => void copyUrl()}>{t("actions.copy", { ns: "common" })}</Button>
            </Space.Compact>
          </div>
        </div>
        <label className="settings-field">
           <span className="settings-field-label">{t("webhook.settings.credential")}</span>
          {canConfigure && props.canManageCredentials === true ? (
            <>
              <Select
                data-testid="webhook-credential"
                value={credentialId ?? undefined}
                 placeholder={t("webhook.settings.credentialPlaceholder")}
                options={tokenCredentials.map((credential) => ({ label: credential.name, value: credential.id }))}
                onChange={(value) => { setCredentialId(value); setNotice(null); }}
              />
              {tokenCredentials.length === 0 && (
                 <Alert type="warning" showIcon message={t("webhook.settings.noCredential")} />
              )}
            </>
          ) : (
            <LockedValue testId="webhook-credential-locked">{credentialName}</LockedValue>
          )}
          <Typography.Text type="secondary" className="settings-field-hint">
            {t("webhook.settings.credentialHint")}
          </Typography.Text>
        </label>
        <label className="settings-field">
           <span className="settings-field-label">{t("webhook.settings.worker")}</span>
          {canConfigure ? (
            <Select
              data-testid="webhook-runtime-worker"
              value={workerId ?? undefined}
               placeholder={t("webhook.settings.workerPlaceholder")}
              loading={props.workersLoading}
              disabled={props.workersLoading}
              options={compatibleWorkers.map((worker) => ({
                 label: t("worker.option", {
                   ns: "common",
                   name: worker.name,
                   status: worker.status === "online"
                     ? t("worker.online", { ns: "common" })
                     : t("worker.offline", { ns: "common" }),
                }),
                value: worker.id,
              }))}
              onChange={(value) => { setWorkerId(value); setNotice(null); }}
            />
          ) : (
            <LockedValue testId="webhook-worker-locked">{workerName}</LockedValue>
          )}
        </label>
        <div className="settings-field">
           <span className="settings-field-label">{t("webhook.settings.timeout")}</span>
          {canConfigure ? (
            <>
              <Radio.Group
                data-testid="webhook-timeout-preset"
                value={effectiveCustom ? "custom" : presetMinutesFor(effectiveTimeoutSeconds)}
                onChange={(event) => {
                  const value = event.target.value as number | "custom";
                  if (value === "custom") {
                    setTimeoutCustomMode(true);
                  } else {
                    setTimeoutOverride(value * 60);
                    setTimeoutCustomMode(false);
                  }
                }}
              >
                {TIMEOUT_PRESET_MINUTES.map((minutes) => (
                  <Radio key={minutes} value={minutes}>
                     {t("units.minutes", { value: minutes })}
                  </Radio>
                ))}
                 <Radio value="custom">{t("webhook.settings.custom")}</Radio>
              </Radio.Group>
              {effectiveCustom && (
                <Space.Compact block>
                  <InputNumber
                    data-testid="webhook-timeout-custom"
                    min={1}
                    max={MAX_TIMEOUT_SECONDS}
                    precision={0}
                    value={effectiveTimeoutSeconds}
                    onChange={(value) => setTimeoutOverride(value ?? null)}
                  />
                  <Typography.Text className="webhook-timeout-unit">
                    {t("webhook.settings.seconds")}
                  </Typography.Text>
                </Space.Compact>
              )}
              <Typography.Text type="secondary" className="settings-field-hint">
                 {t("webhook.settings.timeoutHint")}
              </Typography.Text>
            </>
          ) : (
             <LockedValue testId="webhook-timeout-locked">{formatTimeout(effectiveTimeoutSeconds, (key, options) => t(key, options))}</LockedValue>
          )}
        </div>
        {props.workersError !== null && <Alert type="error" showIcon message={props.workersError} />}
        {copyError !== null && <Alert type="warning" showIcon message={copyError} />}
        {notice !== null && <Alert type="success" showIcon data-testid="webhook-notice" message={notice} />}
        {canConfigure && (
          <Button
            data-testid="webhook-save"
            loading={saving}
            disabled={!pathAcceptable || !dirty}
            onClick={() => void saveConfiguration()}
           >{t("webhook.settings.save")}</Button>
        )}
        {!enabled && startBlockedReason !== null && (
          <Alert type="info" showIcon data-testid="webhook-start-blocked" message={startBlockedReason} />
        )}
      </Space>

      <Modal
        open={stopDialogOpen}
         title={t("webhook.settings.stopTitle")}
        width={520}
        destroyOnHidden
        onCancel={() => setStopDialogOpen(false)}
        footer={[
          <Button key="end" danger type="primary" data-testid="webhook-stop-end" onClick={() => void stopAndEndCall()}>
             {t("webhook.settings.endCall")}
          </Button>,
          <Button key="wait" data-testid="webhook-stop-wait" onClick={() => void stopAndWaitForEnd()}>
             {t("webhook.settings.waitCall")}
          </Button>,
          <Button key="cancel" data-testid="webhook-stop-cancel" onClick={() => setStopDialogOpen(false)}>
             {t("webhook.settings.cancelCall")}
          </Button>,
        ]}
      >
        <p data-testid="webhook-stop-dialog-text">
           {t("webhook.settings.stopDescription")}
        </p>
      </Modal>
    </div>
  );
});

export default WebhookTriggerPanel;
