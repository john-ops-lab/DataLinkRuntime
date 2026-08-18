/** Webhook Adapter header: identity, receive state and explicit actions. */

import { Button, Tag } from "antd";
import { useTranslation } from "react-i18next";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, Worker } from "../types";
import ActionWithReason from "./ActionWithReason";
import type { WebhookRuntimeState } from "./WebhookTriggerPanel";

interface Props {
  adapter: Adapter;
  runtimeWorker: Worker | null;
  runtimeState: WebhookRuntimeState;
  busy: boolean;
  contentReady: boolean;
  onSave: () => void;
  onOpenSettings: () => void;
  onToggleReceiving: () => void;
}

export default function WebhookWorkbenchHeader(props: Props) {
  const { t } = useTranslation(["runtime", "common"]);
  const archived = !!props.adapter.archived_at;
  const locked = props.adapter.runtime_locked === true || props.runtimeState.enabled;
  const saveReason = archived
    ? t("webhook.reasons.deleted")
    : locked
      ? t("webhook.reasons.lockedSave")
      : !props.contentReady
        ? t("webhook.reasons.contentNotReady")
        : props.busy
          ? t("webhook.reasons.busy")
          : null;
  const receiveReason = props.runtimeState.enabled
    ? null
    : !props.runtimeState.loaded
      ? t("webhook.reasons.loading")
      : props.runtimeState.runtimeLocked
        ? t("webhook.reasons.activeCall")
        : props.runtimeState.startBlockedReason;

  return (
    <header className="workbench-header" data-testid="workbench-header">
      <div className="workbench-context" data-testid="webhook-workbench-header">
        <div className="workbench-title-row">
          <h2 className="workbench-title" title={props.adapter.name}>{props.adapter.name}</h2>
          <Tag color="cyan">{t("webhook.header.type")}</Tag>
          <span className="workbench-context-fact">{LANGUAGE_LABELS[props.adapter.language]}</span>
          <Tag color={props.runtimeState.enabled ? "processing" : "default"}>
            {props.runtimeState.enabled
              ? t("webhook.status.receiving")
              : props.adapter.running_execution_id != null
                ? t("webhook.status.calling")
                : t("webhook.status.stopped")}
          </Tag>
          <span className="workbench-context-fact" data-testid="header-runtime-worker">
            {t("webhook.header.worker", {
              name: props.runtimeWorker?.name ?? t("labels.notSelected", { ns: "common" }),
            })}
          </span>
        </div>
        {/* M5.5.9/M5.5.12：只保留一行低干扰提示，不再展示大块说明、“复制适配器”
            升级引导或重复的黄色锁定 Alert。接收中与“已停止但仍有活跃调用”共用一行。 */}
        {locked && (
          <p className="runtime-lock-hint" data-testid="webhook-active-execution">
            {props.runtimeState.enabled
              ? t("webhook.lockReceiving")
              : t("webhook.lockCalling")}
          </p>
        )}
      </div>
      <div className="workbench-controls">
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>{t("actions.settings", { ns: "common" })}</Button>
        <ActionWithReason label={t("actions.save", { ns: "common" })} reason={saveReason}>
          <Button type="primary" data-testid="save-version" disabled={saveReason !== null} onClick={props.onSave}>{t("actions.save", { ns: "common" })}</Button>
        </ActionWithReason>
        <ActionWithReason label={props.runtimeState.enabled ? t("actions.stopReceiving", { ns: "common" }) : t("actions.startReceiving", { ns: "common" })} reason={receiveReason}>
          <Button
            danger={props.runtimeState.enabled}
            data-testid="header-webhook-toggle"
            loading={props.runtimeState.changingState}
            disabled={!props.runtimeState.enabled && receiveReason !== null}
            onClick={props.onToggleReceiving}
          >
            {props.runtimeState.enabled ? t("actions.stopReceiving", { ns: "common" }) : t("actions.startReceiving", { ns: "common" })}
          </Button>
        </ActionWithReason>
      </div>
    </header>
  );
}
