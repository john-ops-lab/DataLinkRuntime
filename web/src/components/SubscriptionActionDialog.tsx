import { useEffect, useRef, useState } from "react";
import { Alert, Form, Input, Modal, Select, Table } from "antd";
import { useTranslation } from "react-i18next";
import { ApiError, api } from "../api";
import type {
  EventSubscription,
  SourceCheckpoint,
  SubscriptionDisposition,
  SubscriptionDraftState,
  SubscriptionEvent,
  SubscriptionActivation,
  SubscriptionReplayContext,
} from "../subscription-types";
import { subscriptionDrained } from "../subscription-types";
import { userErrorMessage } from "../user-message";

export type SubscriptionActionKind =
  | "enable"
  | "disable"
  | "resume"
  | "delete"
  | "reset"
  | "replay"
  | "skip"
  | "skipUnaccepted"
  | "repairEnter"
  | "repairExit"
  | "repairSave";
interface Values {
  reason?: string;
  replayMode?: "original" | "current" | "repair";
  previewToken?: string;
  initial?: "earliest" | "latest";
  code?: string;
  requirements?: string;
}

export default function SubscriptionActionDialog({
  action,
  sub: liveSub,
  event,
  checkpoint,
  initialCode,
  initialRequirements,
  onClose,
  onDone,
  onDraftStateChange,
}: {
  action: SubscriptionActionKind;
  sub: EventSubscription;
  event?: SubscriptionEvent;
  checkpoint?: SourceCheckpoint;
  initialCode?: string;
  initialRequirements?: string;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const { t } = useTranslation("subscription");
  const [sub] = useState(() => structuredClone(liveSub));
  const [form] = Form.useForm<Values>();
  const replayMode = Form.useWatch("replayMode", form);
  const [original, setOriginal] = useState<SubscriptionActivation | null>(null);
  const [context, setContext] = useState<SubscriptionReplayContext | null>(
    null,
  );
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [requestId] = useState(() => crypto.randomUUID());
  const version = sub.config_version;
  const frozen = useRef<Values | null>(null);
  const [submitted, setSubmitted] = useState(false);
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
  const audited = ["replay", "skip", "skipUnaccepted", "reset"].includes(
    action,
  );
  const notes: Partial<Record<SubscriptionActionKind, string>> = {
    enable: "enableNote",
    resume: "resumeNote",
    disable: "disableNote",
    delete: "deleteNote",
    replay: "replayNote",
    skip: "skipNote",
    skipUnaccepted: "skipNote",
    reset: "resetNote",
    repairEnter: "repairNote",
    repairSave: "repairNote",
    repairExit: "repairNote",
  };
  async function submit() {
    try {
      const values = frozen.current ?? (await form.validateFields());
      if (
        action === "replay" &&
        values.replayMode === "current" &&
        !values.previewToken
      ) {
        if (
          !context ||
          context.current.config_version !== version ||
          context.current.source_epoch !== sub.source_epoch
        )
          throw new ApiError(
            409,
            "subscription_config_conflict",
            "Replay preview unavailable",
          );
        values.previewToken = context.preview_token;
      }
      frozen.current = structuredClone(values);
      setSubmitted(true);
      setBusy(true);
      setError(null);
      const body: SubscriptionDisposition = {
        expected_config_version: version,
        request_id: requestId,
        reason: values.reason?.trim() ?? "",
      };
      if (action === "replay" && event) {
        if (values.replayMode === "repair" && sub.repair_revision_id)
          body.revision_id = sub.repair_revision_id;
        if (values.replayMode === "current") {
          body.use_current_configuration = true;
          body.expected_preview_token = values.previewToken;
        }
        await api.replaySubscriptionEvent(sub.id, event.id, body);
      } else if (action === "skip" && event)
        await api.skipSubscriptionEvent(sub.id, event.id, body);
      else if (
        action === "skipUnaccepted" &&
        checkpoint?.blocked_unaccepted?.source_event_id
      )
        await api.skipUnacceptedSubscriptionEvent(sub.id, {
          ...body,
          shard: checkpoint.shard,
          source_event_id: checkpoint.blocked_unaccepted.source_event_id,
        });
      else if (action === "reset" && values.initial)
        await api.resetSubscription(sub.id, {
          ...body,
          config: { ...sub.config, initial_position: values.initial },
        });
      else if (action === "delete")
        await api.deleteSubscription(sub.id, version);
      else if (action === "repairSave")
        await api.saveSubscriptionRepair(sub.id, {
          expected_config_version: version,
          code: values.code ?? "",
          requirements: values.requirements ?? "",
        });
      else if (
        ["enable", "disable", "resume", "repairEnter", "repairExit"].includes(
          action,
        )
      ) {
        const route =
          action === "repairEnter"
            ? "repair/enter"
            : action === "repairExit"
              ? "repair/exit"
              : (action as "enable" | "disable" | "resume");
        await api.subscriptionAction(sub.id, route, version);
      } else throw new Error(t("operationFailed"));
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
        setError(
          reason instanceof ApiError
            ? `${t(`errors.${reason.code}`, { defaultValue: t("operationFailed") })} (${reason.code})`
            : userErrorMessage(reason),
        );
    } finally {
      setBusy(false);
    }
  }
  const canCurrent =
    subscriptionDrained(sub) ||
    (sub.desired_state === "disabled" &&
      sub.connection_state === "stopped" &&
      sub.delivery_state !== "repair" &&
      !(sub.event_counts.pending || sub.event_counts.processing) &&
      (sub.event_counts.failed ?? 0) <= (event?.state === "failed" ? 1 : 0));
  useEffect(() => {
    if (action !== "replay" || !event) return;
    let active = true;
    const timer = window.setTimeout(() => {
      void api
        .subscriptionActivation(sub.id, event.activation_id)
        .then((value) => {
          if (active) setOriginal(value);
        })
        .catch((reason: unknown) => {
          if (active) setPreviewError(userErrorMessage(reason));
        });
      if (canCurrent)
        void api
          .subscriptionReplayContext(sub.id, event.id)
          .then((value) => {
            if (active) {
              setContext(value);
              setOriginal(value.original);
            }
          })
          .catch((reason: unknown) => {
            if (active) setPreviewError(userErrorMessage(reason));
          });
    }, 0);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [action, canCurrent, event, sub.id]);
  const previewMatches =
    context?.current.config_version === version &&
    context?.current.source_epoch === sub.source_epoch;
  const compared =
    replayMode === "repair" && original
      ? { ...original, revision_id: sub.repair_revision_id }
      : context?.current;
  const referenceText = (
    value: Pick<
      SubscriptionActivation,
      "source_credential_id" | "credential_refs"
    >,
  ) =>
    JSON.stringify({
      source_credential_id: value.source_credential_id,
      credential_refs: value.credential_refs,
    });
  const comparison = original
    ? [
        {
          key: "revision",
          label: t("revision"),
          original: `#${original.revision_id}`,
          selected: compared ? `#${compared.revision_id}` : "—",
        },
        {
          key: "worker",
          label: t("worker"),
          original: `#${original.worker_id}`,
          selected: compared ? `#${compared.worker_id}` : "—",
        },
        {
          key: "config",
          label: t("version"),
          original: String(original.config_version),
          selected: compared ? String(compared.config_version) : "—",
        },
        {
          key: "epoch",
          label: t("epoch"),
          original: original.source_epoch,
          selected: compared?.source_epoch ?? "—",
        },
        {
          key: "refs",
          label: t("credentialRefs"),
          original: referenceText(original),
          selected: compared ? referenceText(compared) : "—",
        },
      ]
    : [];
  return (
    <Modal
      open
      width={action === "repairSave" || action === "replay" ? 820 : 620}
      title={t(action)}
      maskClosable={false}
      confirmLoading={busy}
      onOk={() => void submit()}
      onCancel={() => {
        if (!busy) onClose();
      }}
      cancelButtonProps={{ disabled: busy }}
      okButtonProps={{
        danger: ["skip", "skipUnaccepted", "reset", "delete"].includes(action),
      }}
      okText={t(action)}
      cancelText={submitted ? t("newRequest") : t("cancel")}
    >
      <Alert
        type={
          ["skip", "skipUnaccepted", "reset", "delete"].includes(action)
            ? "warning"
            : "info"
        }
        showIcon
        message={t(notes[action] ?? "operationFailed")}
      />
      <p>
        {t("version")}: {version} · {t("activation")}:{" "}
        {sub.activation_id ?? "—"}
        {event ? ` · ${t("event")}: ${event.source_event_id}` : ""}
      </p>
      {checkpoint && (
        <p>
          {t("shard")}: {checkpoint.shard} · {t("event")}:{" "}
          <code>{checkpoint.blocked_unaccepted?.source_event_id}</code>
        </p>
      )}
      {error && <Alert type="error" showIcon role="alert" message={error} />}
      <Form
        form={form}
        onValuesChange={() => setDirty(true)}
        layout="vertical"
        disabled={busy || submitted}
        initialValues={{
          replayMode: sub.delivery_state === "repair" ? undefined : "original",
          code: initialCode ?? "",
          requirements: initialRequirements ?? "",
        }}
      >
        {action === "replay" && (
          <Form.Item
            name="replayMode"
            label={t("revision")}
            rules={[{ required: true, message: t("required") }]}
          >
            <Select
              options={[
                {
                  value: "original",
                  label: t("original"),
                  disabled: sub.delivery_state === "repair",
                },
                {
                  value: "current",
                  label: t("current"),
                  disabled: !canCurrent || !previewMatches,
                },
                ...(sub.repair_revision_id && sub.delivery_state === "repair"
                  ? [
                      {
                        value: "repair",
                        label: `${t("repairRevision")} #${sub.repair_revision_id}`,
                      },
                    ]
                  : []),
              ]}
            />
          </Form.Item>
        )}
        {action === "replay" && (
          <>
            {previewError && (
              <Alert type="warning" showIcon message={previewError} />
            )}
            <p>{t("previewNote")}</p>
            <Table
              rowKey="key"
              size="small"
              pagination={false}
              scroll={{ x: 620 }}
              dataSource={comparison}
              columns={[
                { dataIndex: "label", title: t("filter") },
                {
                  dataIndex: "original",
                  title: t("original"),
                  render: (value: string) => (
                    <code className="subscription-compare-value">{value}</code>
                  ),
                },
                {
                  dataIndex: "selected",
                  title: t(
                    replayMode === "repair" ? "repairRevision" : "current",
                  ),
                  render: (value: string) => (
                    <code className="subscription-compare-value">{value}</code>
                  ),
                },
              ]}
            />
          </>
        )}
        {action === "reset" && (
          <Form.Item
            name="initial"
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
        )}
        {action === "repairSave" && (
          <>
            <Form.Item
              name="code"
              label={t("code")}
              rules={[
                { required: true, whitespace: true, message: t("required") },
              ]}
            >
              <Input.TextArea rows={16} spellCheck={false} />
            </Form.Item>
            <Form.Item name="requirements" label={t("requirements")}>
              <Input.TextArea rows={3} spellCheck={false} />
            </Form.Item>
          </>
        )}
        {audited && (
          <>
            <p>
              {t("requestId")}: <code>{requestId}</code>
            </p>
            <Form.Item
              name="reason"
              label={t("reason")}
              rules={[
                { required: true, whitespace: true, message: t("required") },
              ]}
            >
              <Input.TextArea rows={3} maxLength={4096} />
            </Form.Item>
            <p>{t("retryNote")}</p>
          </>
        )}
      </Form>
    </Modal>
  );
}
