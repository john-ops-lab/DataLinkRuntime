import { useState, type ReactNode } from "react";
import { Alert, Button, Collapse, Space, Tooltip } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Adapter } from "../types";
import type { EventSubscription, SubscriptionCapabilities, SubscriptionDraftState } from "../subscription-types";
import { implementedSourceTypes, subscriptionDrained } from "../subscription-types";
import { eventNeedsAttention, eventSourceLabel, eventStatusKey, eventWaitingLabel, httpIngressPath } from "../event-trigger";
import { subscriptionErrorMessage } from "../subscription-message";
import SubscriptionActionDialog from "./SubscriptionActionDialog";
import SubscriptionConfigDialog from "./SubscriptionConfigDialog";
import "./subscriptions.css";

export function EventReceivingButton({ adapter, sub, capabilities, dirty, busy, readOnly, statusPending, onChanged, onDraftStateChange }: {
  adapter: Adapter; sub: EventSubscription | null; capabilities: SubscriptionCapabilities;
  dirty: boolean; busy: boolean; readOnly: boolean; onChanged: () => Promise<void>;
  statusPending?: boolean;
  onDraftStateChange: (value: SubscriptionDraftState) => void;
}) {
  const { t } = useTranslation("subscription");
  const [action, setAction] = useState<"enable" | "disable" | null>(null);
  const receiving = sub?.desired_state === "enabled";
  const reason = busy ? t("ux.loading") : receiving ? null : statusPending ? t("ux.loading") : !sub ? t("ux.configureFirst")
    : dirty ? t("ux.saveFirst")
    : adapter.latest_version_id === null ? t("ux.codeFirst")
    : adapter.runtime_worker_id == null ? t("ux.workerFirst")
    : adapter.runtime_locked ? t("ux.settleFirst")
    : !capabilities.enabled || !sub.available || !capabilities.available_sources.includes(sub.source_type) ? t("unavailable")
    : !subscriptionDrained(sub) ? t("ux.settleFirst") : null;
  if (readOnly) return null;
  return <>
    <Tooltip title={reason}><span className="action-with-reason" aria-label={reason ?? undefined}>
      <Button type="primary" danger={receiving} disabled={reason !== null}
        data-testid="header-event-toggle" onClick={() => setAction(receiving ? "disable" : "enable")}>
        {t(receiving ? "ux.stopReceiving" : "ux.startReceiving")}
      </Button>
    </span></Tooltip>
    {action && sub && <SubscriptionActionDialog action={action} sub={sub}
      onDraftStateChange={onDraftStateChange} onClose={() => setAction(null)}
      onDone={async () => { setAction(null); await onChanged(); }} />}
  </>;
}

export default function EventTriggerSettings({ adapter, sub, capabilities, readOnly, canManage, runtimeDirty, runtimeFields, initialConfigure, onChanged, onRecords, onDraftStateChange }: {
  adapter: Adapter; sub: EventSubscription | null; capabilities: SubscriptionCapabilities;
  readOnly: boolean; canManage: boolean; runtimeDirty: boolean;
  initialConfigure?: boolean;
  runtimeFields?: ReactNode;
  onChanged: () => Promise<void>; onRecords: () => void;
  onDraftStateChange: (value: SubscriptionDraftState) => void;
}) {
  const { t } = useTranslation("subscription");
  const [configuring, setConfiguring] = useState(initialConfigure === true && !sub);
  const [configSnapshot, setConfigSnapshot] = useState<EventSubscription>();
  const [deleting, setDeleting] = useState(false);
  const [testing, setTesting] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const available = implementedSourceTypes.filter(source => capabilities.available_sources.includes(source));
  const canConfigure = !readOnly && !runtimeDirty && !adapter.runtime_locked && (sub ? subscriptionDrained(sub) : adapter.run_mode === "manual");
  async function test() {
    if (!sub) return;
    setTesting(true); setError(null); setFeedback(null);
    try { await api.testSubscriptionConnection(sub.id, sub.config_version); setFeedback(t(sub.source_type === "http" ? "http.checked" : "ux.testPassed")); }
    catch (reason) { setError(subscriptionErrorMessage(reason, sub.source_type)); }
    finally { setTesting(false); }
  }
  return <section className="event-trigger-settings" data-testid="event-trigger-settings">
    <p className="ux-muted">{t("ux.eventHint")}</p>
    {(!capabilities.enabled || available.length === 0) && <Alert type="info" message={t("unavailable")} />}
    <div className="event-source-row"><strong>{t("ux.source")}</strong>
      <span>{sub ? eventSourceLabel(sub) : t("ux.notConfigured")}</span>
      {!readOnly && <Button type="link" disabled={!canConfigure || !capabilities.enabled || available.length === 0 || (sub != null && !available.some(source => source === sub.source_type))}
        onClick={() => { setConfigSnapshot(sub ? structuredClone(sub) : undefined); setConfiguring(true); }}>{t(sub ? "ux.changeSource" : "ux.configureSource")}</Button>}
    </div>
    {sub?.source_type === "http" && httpIngressPath(sub) && <div className="http-ingress-address">
      <strong>{t("http.path")}</strong><code className="http-ingress-path">{httpIngressPath(sub)}</code>
      <p className="ux-muted">{t("http.pathHint")}</p>
    </div>}
    {runtimeFields}
    <p className="ux-muted">{t("ux.eventInput")}</p>
    {sub?.config.mode === "business_batch" && <p className="ux-muted">{t("businessBatch.receivingHint")}</p>}
    {runtimeDirty && <Alert type="info" message={t("ux.saveSettingsFirst")} />}
    {error && <Alert type="error" message={error} />}
    {feedback && <Alert type="success" showIcon message={feedback} />}
    {sub && <div className="event-status-strip" role="status">
      <span>{t(`ux.status.${eventStatusKey(sub)}`)}</span>
      <span>{t("ux.pendingCount", { count: sub.quota.pending_count ?? 0 })}</span>
      <span>{t("ux.processingCount", { count: sub.event_counts.processing ?? 0 })}</span>
      <span>{t("ux.failedCount", { count: sub.event_counts.failed ?? 0 })}</span>
      <span>{t("ux.oldestWaiting", { duration: eventWaitingLabel(sub, t) })}</span>
      <Button type="link" onClick={onRecords}>{t(eventNeedsAttention(sub) ? "ux.viewProblem" : "ux.viewRecords")}</Button>
    </div>}
    <Space wrap><Button loading={testing} disabled={readOnly || !sub || !capabilities.enabled || testing} onClick={() => void test()}>{t(sub?.source_type === "http" ? "http.check" : "test")}</Button>
      <Button onClick={() => setFeedback(t(sub?.source_type === "http" ? "http.help" : "ux.help"))}>{t("ux.helpLabel")}</Button></Space>
    {sub && <Collapse items={[{ key: "advanced", label: t("ux.advanced"), children: <>
      <p>{t("ux.unbindHint")}</p><Button danger disabled={readOnly || !subscriptionDrained(sub)} onClick={() => setDeleting(true)}>{t("ux.unbind")}</Button>
    </> }]} />}
    {configuring && <SubscriptionConfigDialog subscription={configSnapshot} availableSources={available}
      adapterId={adapter.id} canManage={canManage} canTest onDraftStateChange={onDraftStateChange}
      onClose={() => setConfiguring(false)} onSave={async ({ name, description, config, new_epoch }, test) => {
        const saved = configSnapshot
          ? await api.updateSubscription(configSnapshot.id, { name, description, config, new_epoch, expected_config_version: configSnapshot.config_version })
          : await api.createSubscription({ adapter_id: adapter.id, name: adapter.name, description: adapter.description, config });
        setConfigSnapshot(structuredClone(saved));
        await onChanged();
        if (test) {
          try { await api.testSubscriptionConnection(saved.id, saved.config_version); }
          catch (reason) { throw new Error(`${t("ux.savedTestFailed")} ${subscriptionErrorMessage(reason, saved.source_type)}`); }
        } else setConfiguring(false);
        return saved;
      }} />}
    {deleting && sub && <SubscriptionActionDialog action="delete" sub={sub}
      onDraftStateChange={onDraftStateChange} onClose={() => setDeleting(false)}
      onDone={async () => { setDeleting(false); await onChanged(); }} />}
  </section>;
}
