import { useEffect, useState } from "react";
import { Alert, Checkbox, Form, Input, Modal, Select } from "antd";
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
  onClose,
  onSave,
  onDraftStateChange,
}: {
  subscription?: EventSubscription;
  adapterId: number;
  canManage: boolean;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
  onClose: () => void;
  onSave: (value: {
    name: string;
    description: string;
    config: KafkaSourceConfig;
    new_epoch: boolean;
  }) => Promise<void>;
}) {
  const { t } = useTranslation("subscription");
  const [form] = Form.useForm<Values>();
  const security = Form.useWatch("security_protocol", form);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [busy, setBusy] = useState(false);
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
  async function save() {
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
        return;
      }
      setBusy(true);
      setError(null);
      await onSave({
        name: value.name || "",
        description: value.description || "",
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
      });
      onClose();
    } catch (reason) {
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
    <Modal
      open
      title={t(subscription ? "configure" : "create")}
      width={680}
      onCancel={() => {
        if (
          !busy &&
          (!form.isFieldsTouched() ||
            window.confirm(t("confirm.discardChanges", { ns: "common" })))
        )
          onClose();
      }}
      onOk={() => void save()}
      confirmLoading={busy}
      maskClosable={false}
      cancelButtonProps={{ disabled: busy }}
      okText={t("save")}
      cancelText={t("cancel")}
    >
      {error && <Alert type="error" showIcon message={error} />}
      <Alert type="info" showIcon message={t("rangeNote")} />
      <Form
        form={form}
        onValuesChange={() => setDirty(true)}
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
        <Form.Item name="name" label={t("name")}>
          <Input maxLength={128} />
        </Form.Item>
        <Form.Item name="description" label={t("description")}>
          <Input.TextArea rows={2} maxLength={4096} />
        </Form.Item>
        <Form.Item
          name="cluster_id"
          label={t("cluster")}
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
        <Form.Item name="group_id" label={t("group")}>
          <Input maxLength={256} />
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
              label: value,
            }))}
          />
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
      </Form>
    </Modal>
  );
}
