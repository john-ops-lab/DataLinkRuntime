import { useEffect, useRef, useState } from "react";
import { Alert, Checkbox, Descriptions, Form, Input, Modal } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { subscriptionErrorMessage } from "../subscription-message";
import type {
  CompletionAbandonment,
  EventSubscription,
  SubscriptionActivation,
  SubscriptionDraftState,
  SyncBatchDetail,
} from "../subscription-types";

export default function SubscriptionAbandonDialog({
  sub: liveSub,
  batch: liveBatch,
  onClose,
  onDone,
  onDraftStateChange,
}: {
  sub: EventSubscription;
  batch: SyncBatchDetail;
  onClose: () => void;
  onDone: () => Promise<void>;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
}) {
  const { t } = useTranslation("subscription");
  const [snapshot] = useState(() => ({
    sub: structuredClone(liveSub),
    batch: structuredClone(liveBatch),
    requestId: crypto.randomUUID(),
  }));
  const [form] = Form.useForm<{ reason: string; confirmed: boolean }>();
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [references, setReferences] = useState<
    Record<number, SubscriptionActivation>
  >({});
  const frozen = useRef<CompletionAbandonment | null>(null);
  const { sub, batch, requestId } = snapshot;
  const control = batch.controls.find((item) => item.kind === "sync.completed");
  const originalActivationId = batch.activation_id;
  const effectiveActivationId = control?.effective_activation_id;
  useEffect(() => {
    let active = true;
    const ids = [
      ...new Set(
        [originalActivationId, effectiveActivationId].filter(
          (value): value is number => value != null,
        ),
      ),
    ];
    void Promise.all(ids.map((id) => api.subscriptionActivation(sub.id, id)))
      .then((rows) => {
        if (active)
          setReferences(
            Object.fromEntries(rows.map((value) => [value.id, value])),
          );
      })
      .catch((reason: unknown) => {
        if (active) setError(subscriptionErrorMessage(reason, "kubernetes"));
      });
    return () => {
      active = false;
    };
  }, [sub.id, originalActivationId, effectiveActivationId]);
  useEffect(() => {
    onDraftStateChange?.({ dirty, busy });
  }, [dirty, busy, onDraftStateChange]);
  useEffect(
    () => () => onDraftStateChange?.({ dirty: false, busy: false }),
    [onDraftStateChange],
  );
  async function submit() {
    try {
      if (!frozen.current) {
        const values = await form.validateFields();
        if (
          !control ||
          control.latest_delivery_id === null ||
          control.latest_delivery_ordinal === null ||
          batch.source_epoch === null
        )
          return;
        frozen.current = {
          expected_config_version: sub.config_version,
          source_epoch: batch.source_epoch,
          receipt_id: control.receipt_id,
          expected_latest_delivery_id: control.latest_delivery_id,
          expected_delivery_ordinal: control.latest_delivery_ordinal,
          expected_execution_id: control.execution_id,
          request_id: requestId,
          reason: values.reason.trim(),
          confirm_impact: true,
        };
      }
      setSubmitted(true);
      setBusy(true);
      setError(null);
      await api.abandonSubscriptionCompletion(sub.id, batch.id, frozen.current);
      await onDone();
      onClose();
    } catch (reason) {
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
    <Modal
      open
      title={t("batch.abandon")}
      width={760}
      maskClosable={false}
      confirmLoading={busy}
      onOk={() => void submit()}
      onCancel={() => {
        if (!busy) onClose();
      }}
      okText={t(submitted ? "batch.retryOriginal" : "batch.abandon")}
      okButtonProps={{ danger: true }}
      cancelButtonProps={{ disabled: busy }}
      cancelText={t(submitted ? "newRequest" : "cancel")}
    >
      <Alert
        type="warning"
        showIcon
        message={t("batch.unconfirmed")}
        description={t("batch.abandonImpact")}
      />
      <Descriptions
        size="small"
        column={1}
        items={[
          { key: "batch", label: t("batch.id"), children: `#${batch.id}` },
          {
            key: "scope",
            label: t("batch.scope"),
            children: (
              <code className="subscription-compare-value">
                {batch.scope_fingerprint}
              </code>
            ),
          },
          { key: "version", label: t("version"), children: sub.config_version },
          {
            key: "epoch",
            label: t("epoch"),
            children: batch.source_epoch ?? t("batch.historicalUnknown"),
          },
          {
            key: "receipt",
            label: t("batch.receipt"),
            children: control?.receipt_id ?? "—",
          },
          {
            key: "delivery",
            label: t("batch.latestDelivery"),
            children: `${control?.latest_delivery_id ?? "—"} · ${t("ordinal")} ${control?.latest_delivery_ordinal ?? "—"}`,
          },
          {
            key: "execution",
            label: t("execution"),
            children:
              control?.execution_id === null
                ? t("batch.notDispatched")
                : `#${control?.execution_id ?? "—"}`,
          },
          {
            key: "activation",
            label: t("batch.effectiveActivation"),
            children: control?.effective_activation_id ?? "—",
          },
          {
            key: "original-version",
            label: t("batch.originalVersion"),
            children: `${batch.config_version ?? t("batch.historicalUnknown")} · ${t("revision")} ${originalActivationId !== null ? (references[originalActivationId]?.revision_id ?? t("batch.historicalUnknown")) : t("batch.historicalUnknown")}`,
          },
          {
            key: "effective-version",
            label: t("batch.effectiveVersion"),
            children: `${effectiveActivationId != null ? (references[effectiveActivationId]?.config_version ?? t("batch.historicalUnknown")) : t("batch.historicalUnknown")} · ${t("revision")} ${control?.revision_override_id ?? (effectiveActivationId != null ? references[effectiveActivationId]?.revision_id : null) ?? t("batch.historicalUnknown")}`,
          },
          {
            key: "revision",
            label: t("repairRevision"),
            children: control?.revision_override_id ?? t("original"),
          },
          {
            key: "request",
            label: t("requestId"),
            children: <code>{requestId}</code>,
          },
        ]}
      />
      {error ? (
        <Alert type="error" showIcon role="alert" message={error} />
      ) : null}
      <Form
        form={form}
        layout="vertical"
        disabled={busy || submitted}
        onValuesChange={() => setDirty(true)}
      >
        <Form.Item
          name="reason"
          label={t("reason")}
          rules={[
            { required: true, whitespace: true, message: t("required") },
            {
              validator: (_, value?: string) =>
                new TextEncoder().encode(value ?? "").length <= 4096
                  ? Promise.resolve()
                  : Promise.reject(new Error(t("batch.reasonBytes"))),
            },
          ]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
        <Form.Item
          name="confirmed"
          valuePropName="checked"
          rules={[
            {
              validator: (_, value: boolean) =>
                value
                  ? Promise.resolve()
                  : Promise.reject(new Error(t("batch.confirmRequired"))),
            },
          ]}
        >
          <Checkbox>{t("batch.confirmImpact")}</Checkbox>
        </Form.Item>
      </Form>
      <p>{t("retryNote")}</p>
    </Modal>
  );
}
