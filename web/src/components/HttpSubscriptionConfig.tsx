import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Drawer, Form, Input, Select, Space } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Credential } from "../types";
import type { EventSubscription, HttpSourceConfig, SubscriptionDraftState } from "../subscription-types";
import { httpIngressPath } from "../event-trigger";
import { subscriptionErrorMessage } from "../subscription-message";
import EventBatchModeFields from "./EventBatchModeFields";

interface Values {
  credential_id?: number;
  limits: string;
  mode: "per_event" | "business_batch";
}

export default function HttpSubscriptionConfig({
  subscription, adapterId, canManage, readOnly = false, canTest = false,
  sourceSelector, onDraftStateChange, onClose, onSave,
}: {
  subscription?: EventSubscription;
  adapterId: number;
  canManage: boolean;
  readOnly?: boolean;
  canTest?: boolean;
  sourceSelector?: ReactNode;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
  onClose: () => void;
  onSave: (value: {
    name: string; description: string; config: HttpSourceConfig; new_epoch: boolean;
  }, test?: boolean) => Promise<void | EventSubscription>;
}) {
  const { t } = useTranslation("subscription");
  const [form] = Form.useForm<Values>();
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [checked, setChecked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const advanced = useRef<HTMLDetailsElement>(null);
  const config = subscription?.config.source_type === "http" ? subscription.config : undefined;
  const path = subscription ? httpIngressPath(subscription) : null;
  useEffect(() => { onDraftStateChange?.({ dirty, busy }); }, [dirty, busy, onDraftStateChange]);
  useEffect(() => () => onDraftStateChange?.({ dirty: false, busy: false }), [onDraftStateChange]);
  useEffect(() => {
    let active = true;
    if (canManage) void api.listAdapterCredentialOptions(adapterId).then(rows => {
      if (active) setCredentials(rows);
    }).catch(reason => { if (active) setError(subscriptionErrorMessage(reason, "http")); });
    return () => { active = false; };
  }, [adapterId, canManage]);
  const options = credentials.filter(item => item.type === "token")
    .map(item => ({ value: item.id, label: `${item.name} (#${item.id})` }));
  if (config?.credential_id && !options.some(item => item.value === config.credential_id))
    options.push({ value: config.credential_id, label: `#${config.credential_id}` });

  function close() {
    if (!busy && (!dirty || window.confirm(t("confirm.discardChanges", { ns: "common" })))) onClose();
  }
  async function save(test = false) {
    if (readOnly || busy) return;
    setChecked(false);
    try {
      const values = await form.validateFields();
      let limits: unknown;
      try { limits = JSON.parse(values.limits || "{}"); } catch { limits = null; }
      if (!limits || typeof limits !== "object" || Array.isArray(limits) ||
        Object.entries(limits).some(([key, amount]) =>
          !["pending_count", "pending_bytes", "history_count", "history_bytes", "dedupe_count", "rate"].includes(key)
          || !Number.isSafeInteger(amount) || amount <= 0)) {
        if (advanced.current) advanced.current.open = true;
        form.setFields([{ name: "limits", errors: [t("limitsInvalid")] }]);
        return;
      }
      setBusy(true); setError(null);
      await onSave({
        name: subscription?.name ?? "", description: subscription?.description ?? "",
        config: { source_type: "http", credential_id: values.credential_id!, mode: values.mode ?? "per_event",
          ordering: "receipt_serial", limits: limits as Record<string, number> },
        new_epoch: false,
      }, test);
      if (test) { setDirty(false); setChecked(true); } else onClose();
    } catch (reason) {
      const fields = (reason as { errorFields?: { name: (string | number)[] }[] })?.errorFields;
      if (fields?.length) {
        if (fields.some(field => field.name[0] === "limits") && advanced.current) advanced.current.open = true;
        window.setTimeout(() => form.getFieldInstance(fields[0].name)?.focus?.(), 0);
      } else setError(subscriptionErrorMessage(reason, "http"));
    } finally { setBusy(false); }
  }
  return <Drawer open width="min(720px, 100vw)" title={t("ux.configureSource")}
    maskClosable={false} keyboard={!busy} closable={{ disabled: busy }} onClose={close}
    footer={<Space><Button disabled={busy} onClick={close}>{t("cancel")}</Button>
      <Button type="primary" disabled={readOnly} loading={busy} onClick={() => void save()}>{t("save")}</Button></Space>}>
    {sourceSelector}
    <p>{t("http.hint")}</p>
    <Alert type="info" showIcon message={t("http.saveNote")} />
    {error && <Alert type="error" showIcon message={error} />}
    <Form form={form} layout="vertical" disabled={readOnly || busy}
      onValuesChange={() => { setDirty(true); setChecked(false); }}
      initialValues={{ credential_id: config?.credential_id, mode: config?.mode ?? "per_event",
        limits: JSON.stringify(config?.limits ?? {}, null, 2) }}>
      <Form.Item name="credential_id" label={t("http.credential")} extra={t("http.credentialHint")}
        rules={[{ required: true, message: t("required") }]}>
        <Select options={options} />
      </Form.Item>
      {path && <p>{t("http.path")}: <code className="http-ingress-path">{path}</code></p>}
      <details className="event-form-advanced" ref={advanced}>
        <summary>{t("ux.advanced")}</summary>
        <EventBatchModeFields />
        <p>{t("http.capacityHint")}</p>
        <Form.Item name="limits" label={t("quota")} extra={t("http.capacityFields")}>
          <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} />
        </Form.Item>
      </details>
      <details className="event-form-advanced">
        <summary>{t("ux.helpLabel")}</summary>
        <p>{t("http.sendHint")}</p>
        <p>{t("http.retryHint")}</p>
      </details>
    </Form>
    {checked && <Alert type="success" showIcon message={t("http.checked")} />}
    {canTest && !readOnly && <Button disabled={busy} loading={busy} onClick={() => void save(true)}>{t("http.saveAndCheck")}</Button>}
  </Drawer>;
}
