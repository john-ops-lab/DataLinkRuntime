import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Checkbox, Drawer, Form, Input, Select, Space } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Credential } from "../types";
import type {
  EventSubscription,
  KubernetesSourceConfig,
  SubscriptionDraftState,
} from "../subscription-types";
import { subscriptionErrorMessage } from "../subscription-message";

interface Values {
  name: string;
  description: string;
  cluster_id: string;
  server: string;
  resource: "pods" | "deployments";
  all_namespaces: boolean;
  namespace?: string;
  label_selector?: string;
  field_selector?: string;
  credential_id?: number;
  ca_pem?: string;
  mode: "per_event" | "state_sync";
  limits: string;
  new_epoch?: boolean;
}

export default function KubernetesSubscriptionConfig({
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
  onClose: () => void;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
  onSave: (value: {
    name: string;
    description: string;
    config: KubernetesSourceConfig;
    new_epoch: boolean;
  }, test?: boolean) => Promise<void | EventSubscription>;
}) {
  const { t } = useTranslation("subscription");
  const [form] = Form.useForm<Values>();
  const allNamespaces = Form.useWatch("all_namespaces", form);
  const mode = Form.useWatch("mode", form);
  const config =
    subscription?.config.source_type === "kubernetes"
      ? subscription.config
      : undefined;
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [busy, setBusy] = useState(false);
  const advancedRef = useRef<HTMLDetailsElement>(null);
  const [feedback, setFeedback] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    onDraftStateChange?.({ dirty, busy });
  }, [dirty, busy, onDraftStateChange]);
  useEffect(
    () => () => onDraftStateChange?.({ dirty: false, busy: false }),
    [onDraftStateChange],
  );
  useEffect(() => {
    let active = true;
    if (canManage)
      void api
        .listAdapterCredentialOptions(adapterId)
        .then((rows) => {
          if (active) setCredentials(rows);
        })
        .catch((reason: unknown) => {
          if (active) setError(subscriptionErrorMessage(reason, "kubernetes"));
        });
    return () => {
      active = false;
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
          source_type: "kubernetes",
          cluster_id: value.cluster_id,
          server: value.server,
          group: value.resource === "pods" ? "" : "apps",
          version: "v1",
          resource: value.resource,
          namespace: value.all_namespaces
            ? null
            : value.namespace?.trim() || null,
          label_selector: value.label_selector || "",
          field_selector: value.field_selector || "",
          credential_id: value.credential_id ?? null,
          ca_pem: value.ca_pem?.trim() || null,
          mode: value.mode,
          limits: limits as Record<string, number>,
        },
      }, test);
      if (test) {
        setDirty(false);
        setFeedback(true);
        void saved;
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
        setError(subscriptionErrorMessage(reason, "kubernetes"));
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
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <p className="ux-muted">{t("kubernetes.rangeNote")}</p>
      <Form
        form={form}
        layout="vertical"
        disabled={busy}
        onValuesChange={() => { setDirty(true); setFeedback(false); }}
        initialValues={{
          name: subscription?.name ?? "",
          description: subscription?.description ?? "",
          cluster_id: config?.cluster_id ?? "",
          server: config?.server ?? "",
          resource: config?.resource ?? "pods",
          all_namespaces: config ? config.namespace === null : false,
          namespace: config?.namespace ?? "",
          label_selector: config?.label_selector ?? "",
          field_selector: config?.field_selector ?? "",
          credential_id: config?.credential_id ?? undefined,
          ca_pem: config?.ca_pem ?? "",
          mode: config?.mode ?? "per_event",
          limits: JSON.stringify(config?.limits ?? {}, null, 2),
        }}
      >
        <Form.Item
          name="cluster_id"
          label={t("kubernetes.cluster")}
          rules={[{ required: true, whitespace: true, message: t("required") }]}
        >
          <Input maxLength={256} />
        </Form.Item>
        <Form.Item
          name="server"
          label={t("kubernetes.server")}
          rules={[
            { required: true, message: t("required") },
            {
              validator: (_, value: string) => {
                try {
                  const url = new URL(value);
                  if (
                    url.protocol === "https:" &&
                    !Array.from(value).some(
                      (character) => character.charCodeAt(0) <= 32,
                    ) &&
                    !url.username &&
                    !url.password &&
                    url.pathname === "/" &&
                    !url.search &&
                    !url.hash
                  )
                    return Promise.resolve();
                } catch {
                  /* Show the same bounded local message. */
                }
                return Promise.reject(new Error(t("kubernetes.originInvalid")));
              },
            },
          ]}
        >
          <Input maxLength={2048} />
        </Form.Item>
        <Form.Item
          name="resource"
          label={t("kubernetes.resource")}
          rules={[{ required: true, message: t("required") }]}
        >
          <Select
            options={[
              { value: "pods", label: "Pod · core/v1" },
              { value: "deployments", label: "Deployment · apps/v1" },
            ]}
          />
        </Form.Item>
        <Form.Item name="all_namespaces" valuePropName="checked">
          <Checkbox>{t("kubernetes.allNamespaces")}</Checkbox>
        </Form.Item>
        {!allNamespaces ? (
          <Form.Item
            name="namespace"
            label={t("kubernetes.namespace")}
            rules={[
              { required: true, whitespace: true, message: t("required") },
            ]}
          >
            <Input maxLength={253} />
          </Form.Item>
        ) : (
          <Alert type="warning" message={t("kubernetes.allNamespacesNote")} />
        )}
        <Form.Item
          name="mode"
          label={t("kubernetes.mode")}
          rules={[{ required: true, message: t("required") }]}
        >
          <Select
            options={[
              { value: "per_event", label: t("kubernetes.perEvent") },
              { value: "state_sync", label: t("kubernetes.stateSync") },
            ]}
          />
        </Form.Item>
        <Alert
          type={mode === "state_sync" ? "warning" : "info"}
          showIcon
          message={t(
            mode === "state_sync"
              ? "kubernetes.stateSyncNote"
              : "kubernetes.perEventNote",
          )}
        />
        <Form.Item
          name="credential_id"
          label={t("credential")}
          extra={t("kubernetes.credentialNote")}
        >
          <Select
            allowClear
            disabled={!canManage || busy}
            options={credentials
              .filter((item) => item.type === "token")
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
        <Form.Item name="label_selector" label={t("kubernetes.labelSelector")}>
          <Input maxLength={4096} />
        </Form.Item>
        <Form.Item name="field_selector" label={t("kubernetes.fieldSelector")}>
          <Input maxLength={4096} />
        </Form.Item>
        <Form.Item name="ca_pem" label={t("ca")}>
          <Input.TextArea rows={3} maxLength={65536} />
        </Form.Item>
        <Form.Item name="limits" label={t("limits")}>
          <Input.TextArea rows={3} />
        </Form.Item>
        {subscription ? (
          <Form.Item name="new_epoch" valuePropName="checked">
            <Checkbox>{t("kubernetes.newEpoch")}</Checkbox>
          </Form.Item>
        ) : null}
        </details>
      </Form>
      {canTest && <Space direction="vertical"><Space>
        <Button loading={busy} onClick={() => void save(true)}>{t("ux.saveAndTest")}</Button>
        {feedback && <span role="status">{t("ux.testPassed")}</span>}
      </Space><span className="ux-muted">{t("ux.saveTestHint")}</span></Space>}
    </Drawer>
  );
}
