import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Checkbox, Drawer, Form, Input, Select, Space } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Credential } from "../types";
import type {
  EventSubscription,
  KafkaSourceConfig,
  SubscriptionDraftState,
} from "../subscription-types";
import { subscriptionErrorMessage } from "../subscription-message";

interface Values {
  name: string;
  description: string;
  cluster_id: string;
  bootstrap: string;
  topics: string;
  group_id: string;
  initial_position: "earliest" | "latest";
  security_protocol: KafkaSourceConfig["security_protocol"];
  sasl_mechanism?: KafkaSourceConfig["sasl_mechanism"];
  credential_id?: number;
  ca_pem?: string;
  limits: string;
  new_epoch?: boolean;
}

export default function KafkaSubscriptionConfig({
  subscription,
  adapterId,
  canManage,
  sourceSelector,
  canTest = false,
  onClose,
  onSave,
  onDraftStateChange,
}: {
  subscription?: EventSubscription;
  adapterId: number;
  canManage: boolean;
  sourceSelector?: ReactNode;
  canTest?: boolean;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
  onClose: () => void;
  onSave: (value: {
    name: string;
    description: string;
    config: KafkaSourceConfig;
    new_epoch: boolean;
  }, test?: boolean) => Promise<void | EventSubscription>;
}) {
  const { t } = useTranslation("subscription");
  const [form] = Form.useForm<Values>();
  const security = Form.useWatch("security_protocol", form);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [busy, setBusy] = useState(false);
  const advancedRef = useRef<HTMLDetailsElement>(null);
  const [feedback, setFeedback] = useState(false);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    onDraftStateChange?.({ dirty, busy });
  }, [dirty, busy, onDraftStateChange]);
  useEffect(
    () => () => onDraftStateChange?.({ dirty: false, busy: false }),
    [onDraftStateChange],
  );
  const [error, setError] = useState<string | null>(null);
  const config =
    subscription?.config.source_type === "kafka"
      ? subscription.config
      : undefined;
  useEffect(() => {
    let current = true;
    if (canManage)
      void api
        .listAdapterCredentialOptions(adapterId)
        .then((rows) => {
          if (current) setCredentials(rows);
        })
        .catch((reason: unknown) => {
          if (current) setError(subscriptionErrorMessage(reason));
        });
    return () => {
      current = false;
    };
  }, [adapterId, canManage]);
  async function save(test = false) {
    setFeedback(false);
    try {
      const value = await form.validateFields();
      let limits: unknown;
      try {
        limits = JSON.parse(value.limits || "{}");
      } catch {
        limits = null;
      }
      if (
        !limits ||
        typeof limits !== "object" ||
        Array.isArray(limits) ||
        Object.entries(limits).some(
          ([key, amount]) =>
            ![
              "pending_count",
              "pending_bytes",
              "history_count",
              "history_bytes",
              "dedupe_count",
              "rate",
            ].includes(key) ||
            !Number.isSafeInteger(amount) ||
            Number(amount) < 1,
        )
      ) {
        form.setFields([{ name: "limits", errors: [t("limitsInvalid")] }]);
        if (advancedRef.current) advancedRef.current.open = true;
        form.getFieldInstance("limits")?.focus?.();
        return;
      }
      setBusy(true);
      setError(null);
      const saved = await onSave({
        name: subscription?.name ?? "",
        description: subscription?.description ?? "",
        new_epoch: value.new_epoch === true,
        config: {
          source_type: "kafka",
          cluster_id: value.cluster_id,
          bootstrap_servers: value.bootstrap
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean),
          topics: value.topics
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean),
          group_id: value.group_id?.trim() || null,
          initial_position: value.initial_position,
          security_protocol: value.security_protocol,
          sasl_mechanism:
            value.security_protocol === "SASL_SSL"
              ? value.sasl_mechanism
              : null,
          credential_id:
            value.security_protocol === "SASL_SSL"
              ? (value.credential_id ?? null)
              : null,
          ca_pem:
            value.security_protocol === "PLAINTEXT"
              ? null
              : value.ca_pem?.trim() || null,
          mode: "per_event",
          ordering: "partition_serial",
          limits: limits as Record<string, number>,
        },
      }, test);
      if (test) {
        setDirty(false);
        setFeedback(true);
        if (saved?.config.source_type === "kafka") form.setFieldValue("group_id", saved.config.group_id ?? "");
        form.setFieldValue("new_epoch", false);
      } else onClose();
    } catch (reason) {
      const fields = (reason as { errorFields?: { name: (string | number)[] }[] })?.errorFields;
      if (fields?.length) {
        if (fields.some(field => ["limits", "sasl_mechanism", "group_id", "ca_pem", "label_selector", "field_selector"].includes(String(field.name[0])))) {
          if (advancedRef.current) advancedRef.current.open = true;
        }
        window.setTimeout(() => {
          if (typeof HTMLElement.prototype.scrollIntoView === "function") form.scrollToField(fields[0].name, { focus: true });
          else form.getFieldInstance(fields[0].name)?.focus?.();
        }, 0);
      }
      if (
        !(
          typeof reason === "object" &&
          reason !== null &&
          "errorFields" in reason
        )
      )
        setError(subscriptionErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Drawer open width="min(720px, 100vw)" title={t("ux.configureSource")}
      maskClosable={false} keyboard={!busy} closable={{ disabled: busy }}
      onClose={() => { if (!busy && (!dirty || window.confirm(t("confirm.discardChanges", { ns: "common" })))) onClose(); }}
      footer={<Space><Button autoInsertSpace={false} disabled={busy} onClick={() => { if (!dirty || window.confirm(t("confirm.discardChanges", { ns: "common" }))) onClose(); }}>{t("cancel")}</Button>
        <Button autoInsertSpace={false} type="primary" loading={busy} onClick={() => void save()}>{t("save")}</Button></Space>}
    >
      {sourceSelector}
      {error && <Alert type="error" showIcon message={error} />}
      <Form
        form={form}
        onValuesChange={() => { setDirty(true); setFeedback(false); }}
        layout="vertical"
        disabled={busy}
        initialValues={{
          name: subscription?.name ?? "",
          description: subscription?.description ?? "",
          cluster_id: config?.cluster_id ?? "",
          bootstrap: config?.bootstrap_servers.join("\n") ?? "",
          topics: config?.topics.join("\n") ?? "",
          group_id: config?.group_id ?? "",
          initial_position: config?.initial_position,
          security_protocol: config?.security_protocol ?? "SASL_SSL",
          sasl_mechanism: config?.sasl_mechanism ?? "SCRAM-SHA-256",
          credential_id: config?.credential_id ?? undefined,
          ca_pem: config?.ca_pem ?? "",
          limits: JSON.stringify(config?.limits ?? {}, null, 2),
        }}
      >
        <Form.Item
          name="cluster_id"
          label={t("cluster")}
          extra={t("ux.kafkaIdentityHint")}
          rules={[{ required: true, message: t("required") }]}
        >
          <Input maxLength={256} />
        </Form.Item>
        <Form.Item
          name="bootstrap"
          label={t("bootstrap")}
          rules={[{ required: true, message: t("required") }]}
        >
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item
          name="topics"
          label={t("topics")}
          rules={[{ required: true, message: t("required") }]}
        >
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item
          name="initial_position"
          label={t("initial")}
          rules={[{ required: true, message: t("required") }]}
        >
          <Select
            options={[
              { value: "earliest", label: t("earliest") },
              { value: "latest", label: t("latest") },
            ]}
          />
        </Form.Item>
        <Form.Item name="security_protocol" label={t("security")}>
          <Select
            options={["SASL_SSL", "SSL", "PLAINTEXT"].map((value) => ({
              value,
              label: t(`ux.security.${value}`),
            }))}
          />
        </Form.Item>
        <Form.Item
          name="credential_id"
          label={t("credential")}
          extra={t("credentialNote")}
          rules={
            security === "SASL_SSL"
              ? [{ required: true, message: t("required") }]
              : []
          }
        >
          <Select
            allowClear
            disabled={!canManage || busy}
            options={credentials
              .filter((item) => item.type === "password")
              .map((item) => ({
                value: item.id,
                label: `${item.name} (#${item.id})`,
              }))
              .concat(
                config?.credential_id &&
                  !credentials.some((item) => item.id === config.credential_id)
                  ? [
                      {
                        value: config.credential_id,
                        label: `#${config.credential_id}`,
                      },
                    ]
                  : [],
              )}
          />
        </Form.Item>
        <details ref={advancedRef} className="event-form-advanced"><summary>{t("ux.connectionOptions")}</summary>
        <Form.Item name="group_id" label={t("group")}>
          <Input maxLength={256} />
        </Form.Item>
        {security === "SASL_SSL" && (
          <Form.Item
            name="sasl_mechanism"
            label={t("mechanism")}
            rules={[{ required: true, message: t("required") }]}
          >
            <Select
              options={["SCRAM-SHA-256", "SCRAM-SHA-512"].map((value) => ({
                value,
                label: value,
              }))}
            />
          </Form.Item>
        )}
        {security !== "PLAINTEXT" && (
          <Form.Item name="ca_pem" label={t("ca")}>
            <Input.TextArea rows={3} maxLength={65536} />
          </Form.Item>
        )}
        <Form.Item name="limits" label={t("limits")}>
          <Input.TextArea rows={3} />
        </Form.Item>
        {subscription && (
          <Form.Item name="new_epoch" valuePropName="checked">
            <Checkbox>{t("newEpoch")}</Checkbox>
          </Form.Item>
        )}
        </details>
      </Form>
      {canTest && <Space direction="vertical"><Space>
        <Button loading={busy} onClick={() => void save(true)}>{t("ux.saveAndTest")}</Button>
        {feedback && <span role="status">{t("ux.testPassed")}</span>}
      </Space><span className="ux-muted">{t("ux.saveTestHint")}</span></Space>}
    </Drawer>
  );
}
