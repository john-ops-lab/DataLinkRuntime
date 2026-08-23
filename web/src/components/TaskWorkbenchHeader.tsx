/** Task Adapter header: identity, runtime context and type-specific actions. */

import { Button, Tag, Tooltip } from "antd";
import {
  CalendarOutlined,
  PlayCircleOutlined,
  SaveOutlined,
  SettingOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, Worker } from "../types";
import ActionWithReason from "./ActionWithReason";
import type { TaskRuntimeState } from "./TaskRunSettingsPanel";

interface TaskWorkbenchHeaderProps {
  adapter: Adapter;
  runtimeWorker: Worker | null;
  runtimeState: TaskRuntimeState;
  dirty: boolean;
  busy: boolean;
  contentReady: boolean;
  onSave: () => void;
  onOpenSettings: () => void;
  onRunOnce: () => void;
  onStopExecution: () => void;
  onToggleSchedule: () => void;
  readOnly?: boolean;
}

export default function TaskWorkbenchHeader(props: TaskWorkbenchHeaderProps) {
  const { t } = useTranslation(["runtime", "common"]);
  const archived = !!props.adapter.archived_at;
  const readOnly = props.readOnly === true;
  const runtimeLocked = props.adapter.runtime_locked === true;
  const activeExecution = props.runtimeState.activeExecution;
  const scheduleMode = props.adapter.run_mode === "schedule";
  const runtimeStatus = activeExecution
    ? t("task.status.running")
    : scheduleMode && props.runtimeState.scheduleEnabled
      ? t("task.status.scheduled")
      : t("task.status.stopped");
  const saveBlockedReason = readOnly
    ? t("task.reasons.readOnly")
    : archived
    ? t("task.reasons.deleted")
    : runtimeLocked
      ? scheduleMode && props.runtimeState.scheduleEnabled
        ? t("task.reasons.scheduleEnabledSave")
        : t("task.reasons.runningSave")
      : !props.contentReady
        ? t("task.reasons.contentNotReady")
        : props.busy
          ? t("task.reasons.busy")
          : null;
  // M5.5.9：存在未保存修改时不得直接运行，先保存。
  const runBlockedReason = props.dirty
    ? t("task.reasons.dirtyRun")
    : props.runtimeState.canRun
      ? null
      : activeExecution
        ? t("task.reasons.activeRun")
        : props.adapter.latest_version_id === null
          ? t("task.reasons.noVersion")
          : props.adapter.runtime_worker_id == null
            ? t("task.reasons.noWorker")
            : props.runtimeState.loading || props.busy
              ? t("task.reasons.processing")
              : t("task.reasons.unavailable");
  const scheduleToggleReason = props.runtimeState.scheduleEnabled
    ? null
    : activeExecution
      ? t("task.reasons.activeSchedule")
      : props.runtimeState.scheduleEnableBlockedReason;

  return (
    <header className="workbench-header" data-testid="workbench-header">
      <div className="workbench-context" data-testid="task-workbench-header">
        <div className="workbench-title-row">
          <h2 className="workbench-title" title={props.adapter.name}>{props.adapter.name}</h2>
        </div>
        <div className="workbench-meta-row" data-testid="workbench-meta" role="group" aria-label={t("task.contextAria")}>
          <Tag color="blue">{t("task.header.type")}</Tag>
          <span className="workbench-context-fact">{LANGUAGE_LABELS[props.adapter.language]}</span>
          <Tag color={activeExecution || props.runtimeState.scheduleEnabled ? "processing" : "default"}>{runtimeStatus}</Tag>
          <span className="workbench-context-fact" data-testid="header-runtime-worker">
            {t("task.header.worker", {
              name: props.runtimeWorker?.name ?? t("labels.notSelected", { ns: "common" }),
            })}
          </span>
        </div>
        {/* M5.5.9：运行中只保留低干扰提示，不再展示大块说明或“复制适配器”升级引导。 */}
        {runtimeLocked && (
          <p className="runtime-lock-hint" data-testid="task-active-execution">
            {t("task.lockHint")}
          </p>
        )}
      </div>
      <div
        className="workbench-controls"
        data-testid="workbench-toolbar"
        role="toolbar"
        aria-label={t("task.actionsAria")}
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
          <Tag data-testid="adapter-read-only">{t("task.reasons.readOnly")}</Tag>
        ) : (
          <>
            <ActionWithReason label={t("actions.save", { ns: "common" })} reason={saveBlockedReason}>
              <Button
                type="primary"
                data-testid="save-version"
                icon={<SaveOutlined aria-hidden="true" />}
                aria-label={t("actions.save", { ns: "common" })}
                disabled={saveBlockedReason !== null}
                onClick={props.onSave}
              >
                {t("actions.save", { ns: "common" })}
              </Button>
            </ActionWithReason>
            {scheduleMode ? (
              <>
                <ActionWithReason
                  label={props.runtimeState.scheduleEnabled ? t("actions.disableSchedule", { ns: "common" }) : t("actions.enableSchedule", { ns: "common" })}
                  reason={scheduleToggleReason}
                >
                  <Button
                    danger={props.runtimeState.scheduleEnabled}
                    data-testid="header-task-schedule-toggle"
                    icon={<CalendarOutlined aria-hidden="true" />}
                    aria-label={props.runtimeState.scheduleEnabled ? t("actions.disableSchedule", { ns: "common" }) : t("actions.enableSchedule", { ns: "common" })}
                    loading={props.runtimeState.loading}
                    disabled={scheduleToggleReason !== null}
                    onClick={props.onToggleSchedule}
                  >
                    {props.runtimeState.scheduleEnabled ? t("actions.disableSchedule", { ns: "common" }) : t("actions.enableSchedule", { ns: "common" })}
                  </Button>
                </ActionWithReason>
                {activeExecution ? (
                  <Button
                    danger
                    data-testid="header-task-stop"
                    icon={<StopOutlined aria-hidden="true" />}
                    aria-label={t("actions.stopCurrentExecution", { ns: "common" })}
                    loading={props.runtimeState.loading}
                    onClick={props.onStopExecution}
                  >
                    {t("actions.stopCurrentExecution", { ns: "common" })}
                  </Button>
                ) : (
                  <ActionWithReason label={t("actions.runImmediatelyOnce", { ns: "common" })} reason={runBlockedReason}>
                    <Button
                      data-testid="header-task-run-once"
                      icon={<PlayCircleOutlined aria-hidden="true" />}
                      aria-label={t("actions.runImmediatelyOnce", { ns: "common" })}
                      disabled={runBlockedReason !== null}
                      onClick={props.onRunOnce}
                    >
                      {t("actions.runImmediatelyOnce", { ns: "common" })}
                    </Button>
                  </ActionWithReason>
                )}
              </>
            ) : activeExecution ? (
              <Button
                danger
                data-testid="header-task-stop"
                icon={<StopOutlined aria-hidden="true" />}
                aria-label={t("actions.stop", { ns: "common" })}
                loading={props.runtimeState.loading}
                onClick={props.onStopExecution}
              >
                {t("actions.stop", { ns: "common" })}
              </Button>
            ) : (
              <ActionWithReason label={t("actions.runOnce", { ns: "common" })} reason={runBlockedReason}>
                <Button
                  data-testid="header-task-run-once"
                  icon={<PlayCircleOutlined aria-hidden="true" />}
                  aria-label={t("actions.runOnce", { ns: "common" })}
                  disabled={runBlockedReason !== null}
                  onClick={props.onRunOnce}
                >
                  {t("actions.runOnce", { ns: "common" })}
                </Button>
              </ActionWithReason>
            )}
          </>
        )}
      </div>
    </header>
  );
}
