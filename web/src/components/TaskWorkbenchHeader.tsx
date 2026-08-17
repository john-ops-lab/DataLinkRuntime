/** Task Adapter header: identity, runtime context and type-specific actions. */

import { Button, Tag } from "antd";

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
}

export default function TaskWorkbenchHeader(props: TaskWorkbenchHeaderProps) {
  const archived = !!props.adapter.archived_at;
  const runtimeLocked = props.adapter.runtime_locked === true;
  const activeExecution = props.runtimeState.activeExecution;
  const scheduleMode = props.adapter.run_mode === "schedule";
  const runtimeStatus = activeExecution
    ? "运行中"
    : scheduleMode && props.runtimeState.scheduleEnabled
      ? "定时已启用"
      : "已停止";
  const saveBlockedReason = archived
    ? "适配器已删除，不能继续编辑"
    : runtimeLocked
      ? scheduleMode && props.runtimeState.scheduleEnabled
        ? "定时已启用，请先停用定时后再保存"
        : "适配器正在运行，请先停止当前运行后再保存"
      : !props.contentReady
        ? "版本内容尚未就绪，请等待加载完成或刷新后重试"
        : props.busy
          ? "其他操作正在进行，请等待完成"
          : null;
  // M5.5.9：存在未保存修改时不得直接运行，先保存。
  const runBlockedReason = props.dirty
    ? "请先保存当前修改，再运行。"
    : props.runtimeState.canRun
      ? null
      : activeExecution
        ? "已有执行正在运行"
        : props.adapter.latest_version_id === null
          ? "请先保存适配器"
          : props.adapter.runtime_worker_id == null
            ? "请先在运行设置中选择并保存运行节点"
            : props.runtimeState.loading || props.busy
              ? "运行操作正在处理中"
              : "当前状态暂不可运行";
  const scheduleToggleReason = props.runtimeState.scheduleEnabled
    ? null
    : activeExecution
      ? "当前执行仍在运行，请等待终态或停止当前执行后再启用定时"
      : props.runtimeState.scheduleEnableBlockedReason;

  return (
    <header className="workbench-header" data-testid="workbench-header">
      <div className="workbench-context" data-testid="task-workbench-header">
        <div className="workbench-title-row">
          <h2 className="workbench-title" title={props.adapter.name}>{props.adapter.name}</h2>
          <Tag color="blue">任务</Tag>
          <span className="workbench-context-fact">{LANGUAGE_LABELS[props.adapter.language]}</span>
          <Tag color={activeExecution || props.runtimeState.scheduleEnabled ? "processing" : "default"}>{runtimeStatus}</Tag>
          <span className="workbench-context-fact" data-testid="header-runtime-worker">
            运行节点：{props.runtimeWorker?.name ?? "未选择"}
          </span>
        </div>
        {/* M5.5.9：运行中只保留低干扰提示，不再展示大块说明或“复制适配器”升级引导。 */}
        {runtimeLocked && (
          <p className="runtime-lock-hint" data-testid="task-active-execution">
            适配器正在运行，编辑与运行配置已锁定。
          </p>
        )}
      </div>
      <div className="workbench-controls">
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>设置</Button>
        <ActionWithReason label="保存" reason={saveBlockedReason}>
          <Button type="primary" data-testid="save-version" disabled={saveBlockedReason !== null} onClick={props.onSave}>
            保存
          </Button>
        </ActionWithReason>
        {scheduleMode ? (
          <>
            <ActionWithReason
              label={props.runtimeState.scheduleEnabled ? "停用定时" : "启用定时"}
              reason={scheduleToggleReason}
            >
              <Button
                danger={props.runtimeState.scheduleEnabled}
                data-testid="header-task-schedule-toggle"
                loading={props.runtimeState.loading}
                disabled={scheduleToggleReason !== null}
                onClick={props.onToggleSchedule}
              >
                {props.runtimeState.scheduleEnabled ? "停用定时" : "启用定时"}
              </Button>
            </ActionWithReason>
            {activeExecution ? (
              <Button danger data-testid="header-task-stop" loading={props.runtimeState.loading} onClick={props.onStopExecution}>
                停止当前执行
              </Button>
            ) : (
              <ActionWithReason label="立即运行一次" reason={runBlockedReason}>
                <Button data-testid="header-task-run-once" disabled={runBlockedReason !== null} onClick={props.onRunOnce}>
                  立即运行一次
                </Button>
              </ActionWithReason>
            )}
          </>
        ) : activeExecution ? (
          <Button danger data-testid="header-task-stop" loading={props.runtimeState.loading} onClick={props.onStopExecution}>
            停止运行
          </Button>
        ) : (
          <ActionWithReason label="运行一次" reason={runBlockedReason}>
            <Button data-testid="header-task-run-once" disabled={runBlockedReason !== null} onClick={props.onRunOnce}>
              运行一次
            </Button>
          </ActionWithReason>
        )}
      </div>
    </header>
  );
}
