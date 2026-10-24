import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Checkbox, Drawer, Form, Input, Select, Space } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Credential } from "../types";
import type { EventSubscription, SubscriptionDraftState, SubscriptionSourceConfig } from "../subscription-types";
import { subscriptionErrorMessage } from "../subscription-message";

type Source = "rabbitmq" | "rocketmq";
interface Values {
  cluster_id: string; server: string; vhost: string; queue: string; topic: string;
  group_id: string; credential_id?: number; ca_pem?: string; limits: string; new_epoch?: boolean;
}
export default function QueueSubscriptionConfig({ sourceType, subscription, adapterId, canManage,
  canTest = false, readOnly = false, sourceSelector, onDraftStateChange, onClose, onSave }: {
  sourceType: Source; subscription?: EventSubscription; adapterId: number; canManage: boolean;
  canTest?: boolean; readOnly?: boolean; sourceSelector?: ReactNode;
  onDraftStateChange?: (value: SubscriptionDraftState) => void; onClose: () => void;
  onSave: (value: { name: string; description: string; config: SubscriptionSourceConfig;
    new_epoch: boolean }, test?: boolean) => Promise<void | EventSubscription>;
}) {
  const { t } = useTranslation("subscription");
  const [form] = Form.useForm<Values>();
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [feedback, setFeedback] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const advanced = useRef<HTMLDetailsElement>(null);
  const server: string = Form.useWatch("server", form) ?? "";
  const tls = /^(amqps|grpcs):/.test(server);
  const rabbit = sourceType === "rabbitmq";
  const candidate = subscription?.config;
  const config = candidate && (candidate.source_type === "rabbitmq" || candidate.source_type === "rocketmq")
    && candidate.source_type === sourceType ? candidate : undefined;
  useEffect(() => { onDraftStateChange?.({ dirty, busy }); }, [dirty, busy, onDraftStateChange]);
  useEffect(() => () => onDraftStateChange?.({ dirty: false, busy: false }), [onDraftStateChange]);
  useEffect(() => {
    let current = true;
    if (canManage) void api.listAdapterCredentialOptions(adapterId).then(rows => {
      if (current) setCredentials(rows);
    }).catch(reason => { if (current) setError(subscriptionErrorMessage(reason)); });
    return () => { current = false; };
  }, [adapterId, canManage]);
  const close = () => { if (!busy && (!dirty || window.confirm(t("confirm.discardChanges", { ns: "common" })))) onClose(); };
  async function save(test = false) {
    if (readOnly || busy) return;
    setFeedback(false);
    try {
      const value = await form.validateFields();
      let limits: unknown;
      try { limits = JSON.parse(value.limits || "{}"); } catch { limits = null; }
      if (!limits || typeof limits !== "object" || Array.isArray(limits) ||
        Object.entries(limits).some(([key, amount]) => !["pending_count", "pending_bytes", "history_count", "history_bytes", "dedupe_count", "rate"].includes(key) || !Number.isSafeInteger(amount) || amount <= 0)) {
        if (advanced.current) advanced.current.open = true;
        form.setFields([{ name: "limits", errors: [t("limitsInvalid")] }]);
        return;
      }
      setBusy(true); setError(null);
      const common = { cluster_id: value.cluster_id, server: value.server,
        ca_pem: tls ? value.ca_pem?.trim() || null : null,
        mode: "per_event" as const, ordering: "receipt_serial" as const,
        limits: limits as Record<string, number> };
      const next: SubscriptionSourceConfig = rabbit
        ? { ...common, source_type: "rabbitmq", vhost: value.vhost, queue: value.queue,
            credential_id: value.credential_id! }
        : { ...common, source_type: "rocketmq", topic: value.topic, group_id: value.group_id,
            credential_id: value.credential_id ?? null };
      await onSave({ name: subscription?.name ?? "", description: subscription?.description ?? "",
        config: next, new_epoch: value.new_epoch === true }, test);
      if (test) { setDirty(false); setFeedback(true); form.setFieldValue("new_epoch", false); }
      else onClose();
    } catch (reason) {
      const fields = (reason as { errorFields?: { name: (string | number)[] }[] })?.errorFields;
      if (fields?.length) {
        if (fields.some(field => ["ca_pem", "limits", "new_epoch"].includes(String(field.name[0]))) && advanced.current) advanced.current.open = true;
        window.setTimeout(() => form.getFieldInstance(fields[0].name)?.focus?.(), 0);
      } else setError(subscriptionErrorMessage(reason, sourceType));
    } finally { setBusy(false); }
  }
  const required = [{ required: true, message: t("required") }];
  const options = credentials.filter(item => item.type === (rabbit ? "password" : "access_key"))
    .map(item => ({ value: item.id, label: `${item.name} (#${item.id})` }));
  if (config?.credential_id && !options.some(item => item.value === config.credential_id))
    options.push({ value: config.credential_id, label: `#${config.credential_id}` });
  return <Drawer open width="min(720px, 100vw)" title={t("ux.configureSource")} maskClosable={false}
    keyboard={!busy} closable={{ disabled: busy }} onClose={close}
    footer={<Space><Button disabled={busy} onClick={close}>{t("cancel")}</Button>
      <Button type="primary" disabled={readOnly} loading={busy} onClick={() => void save()}>{t("save")}</Button></Space>}>
    {sourceSelector}
    {error && <Alert type="error" showIcon message={error} />}
    <p>{t(`queues.${sourceType}Hint`)}</p>
    <Form form={form} layout="vertical" disabled={busy || readOnly}
      onValuesChange={() => { setDirty(true); setFeedback(false); }} initialValues={{
        cluster_id: config?.cluster_id ?? "", server: config?.server ?? "",
        vhost: config?.source_type === "rabbitmq" ? config.vhost : "/",
        queue: config?.source_type === "rabbitmq" ? config.queue : "",
        topic: config?.source_type === "rocketmq" ? config.topic : "",
        group_id: config?.source_type === "rocketmq" ? config.group_id : "",
        credential_id: config?.credential_id ?? undefined, ca_pem: config?.ca_pem ?? "",
        limits: JSON.stringify(config?.limits ?? {}, null, 2), new_epoch: false,
      }}>
      <Form.Item name="cluster_id" label={t("queues.identity")} extra={t("queues.identityHint")}
        rules={[...required, { pattern: /^[a-zA-Z0-9_.-]+$/, message: t("queues.identityInvalid") }]}>
        <Input maxLength={128} />
      </Form.Item>
      <Form.Item name="server" label={t("queues.server")} extra={t(`queues.${sourceType}ServerHint`)}
        rules={[...required, { validator: async (_, value: string) => {
          try {
            const url = new URL(value);
            if (!(rabbit ? ["amqp:", "amqps:"] : ["grpc:", "grpcs:"]).includes(url.protocol) ||
              !url.hostname || !url.port || Number(url.port) > 65535 || Number(url.port) < 1 ||
              url.username || url.password || url.search || url.hash || !["", "/"].includes(url.pathname) || /\s/.test(value)) throw new Error();
          } catch { throw new Error(t("queues.serverInvalid")); }
        } }]}><Input maxLength={512} placeholder={rabbit ? "amqps://broker.example:5671" : "grpcs://proxy.example:8081"} /></Form.Item>
      {rabbit ? <>
        <Form.Item name="vhost" label={t("queues.vhost")} rules={required}><Input maxLength={128} /></Form.Item>
        <Form.Item name="queue" label={t("queues.queue")} rules={required} extra={t("queues.queueHint")}><Input maxLength={255} /></Form.Item>
      </> : <>
        <Form.Item name="topic" label={t("queues.topic")} rules={required}><Input maxLength={127} /></Form.Item>
        <Form.Item name="group_id" label={t("queues.group")} rules={required} extra={t("queues.groupHint")}><Input maxLength={127} /></Form.Item>
      </>}
      <Form.Item name="credential_id" label={t(rabbit ? "credential" : "queues.accessKey")}
        rules={rabbit ? required : []} extra={t("credentialNote")}><Select disabled={!canManage || busy || readOnly} allowClear options={options} /></Form.Item>
      <p className="ux-muted">{t(`queues.${sourceType}StartHint`)}</p>
      <details ref={advanced} className="event-form-advanced"><summary>{t("ux.connectionOptions")}</summary>
        {tls && <Form.Item name="ca_pem" label={t("ca")}><Input.TextArea maxLength={65536} rows={3} /></Form.Item>}
        <Form.Item name="limits" label={t("limits")}><Input.TextArea rows={3} /></Form.Item>
        {subscription && <Form.Item name="new_epoch" valuePropName="checked"><Checkbox>{t("newEpoch")}</Checkbox></Form.Item>}
      </details>
    </Form>
    {canTest && <Space direction="vertical"><Button disabled={readOnly} loading={busy} onClick={() => void save(true)}>{t("ux.saveAndTest")}</Button>
      {feedback && <span role="status">{t("ux.testPassed")}</span>}<span className="ux-muted">{t("ux.saveTestHint")}</span></Space>}
  </Drawer>;
}
