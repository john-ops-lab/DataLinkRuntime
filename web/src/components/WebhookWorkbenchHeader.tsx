/** Webhook Adapter header: identity, receive state and explicit actions. */

import { Button, Tag, Tooltip } from "antd";
import {
  SaveOutlined,
  SettingOutlined,
  StopOutlined,
  PlayCircleOutlined,
} from "@ant-design/icons";
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
  readOnly?: boolean;
}

export default function WebhookWorkbenchHeader(props: Props) {
  const { t } = useTranslation(["runtime", "common"]);
  const archived = !!props.adapter.archived_at;
  const readOnly = props.readOnly === true;
  const locked = props.adapter.runtime_locked === true || props.runtimeState.enabled;
  const saveReason = readOnly
    ? t("webhook.reasons.readOnly")
    : archived
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
        </div>
        <div className="workbench-meta-row" data-testid="workbench-meta" role="group" aria-label={t("webhook.contextAria")}>
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
      <div
        className="workbench-controls"
        data-testid="workbench-toolbar"
        role="toolbar"
        aria-label={t("webhook.actionsAria")}
      >
        <Tooltip title={t("actions.settings", { ns: "common" })} trigger={["hover", "focus"]}>
          <Button
            data-testid="adapter-settings"
            icon={<SettingOutlined aria-hidden="true" />}
            aria-label={t("actions.settings", { ns: "common" })}
            onClick={props.onOpenSettings}
          >
            {t("actions.settings", { ns: "common" })}
          </Button>
        </Tooltip>
        {readOnly ? (
          <Tag data-testid="adapter-read-only">{t("webhook.reasons.readOnly")}</Tag>
        ) : (
          <>
            <ActionWithReason label={t("actions.save", { ns: "common" })} reason={saveReason}>
              <Button
                type="primary"
                data-testid="save-version"
                icon={<SaveOutlined aria-hidden="true" />}
                aria-label={t("actions.save", { ns: "common" })}
                disabled={saveReason !== null}
                onClick={props.onSave}
              >
                {t("actions.save", { ns: "common" })}
              </Button>
            </ActionWithReason>
            <ActionWithReason label={props.runtimeState.enabled ? t("actions.stopReceiving", { ns: "common" }) : t("actions.startReceiving", { ns: "common" })} reason={receiveReason}>
              <Button
                danger={props.runtimeState.enabled}
                data-testid="header-webhook-toggle"
                icon={props.runtimeState.enabled ? <StopOutlined aria-hidden="true" /> : <PlayCircleOutlined aria-hidden="true" />}
                aria-label={props.runtimeState.enabled ? t("actions.stopReceiving", { ns: "common" }) : t("actions.startReceiving", { ns: "common" })}
                loading={props.runtimeState.changingState}
                disabled={!props.runtimeState.enabled && receiveReason !== null}
                onClick={props.onToggleReceiving}
              >
                {props.runtimeState.enabled ? t("actions.stopReceiving", { ns: "common" }) : t("actions.startReceiving", { ns: "common" })}
              </Button>
            </ActionWithReason>
          </>
        )}
      </div>
    </header>
  );
}
