/** Task runtime settings and real Manual/Schedule execution actions (M5.4.2). */

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Radio, Select, Space, Spin, Tag, Typography } from "antd";

import { ApiError, api } from "../api";
import { useExecutionWatcher } from "../hooks/useExecutionWatcher";
import { isTerminal, statusColor, statusLabel } from "../status";
import type { Adapter, AdapterSchedule, TaskRunMode, Worker } from "../types";
import { LogView, OutputView } from "./OutputView";

interface TaskRunSettingsPanelProps {
  adapter: Adapter;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  onAdapterChange: (adapter: Adapter) => void;
  onError: (message: string | null) => void;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

function parseInput(text: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: text.trim() === "" ? null : JSON.parse(text) };
  } catch {
    return { ok: false };
  }
}

function formatTime(value: string | null): string {
  return value === null ? "—" : new Date(value).toLocaleString();
}

export default function TaskRunSettingsPanel(props: TaskRunSettingsPanelProps) {
  const adapterId = props.adapter.id;
  const adapterRunMode = props.adapter.run_mode;
  const onAdapterChange = props.onAdapterChange;
  const onError = props.onError;
  const [workerOverride, setWorkerOverride] = useState<number | null | undefined>(undefined);
  const [runModeOverride, setRunModeOverride] = useState<TaskRunMode | undefined>(undefined);
  const [manualInput, setManualInput] = useState("{}");
  const [schedule, setSchedule] = useState<AdapterSchedule | null>(null);
  const [cron, setCron] = useState("*/5 * * * *");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [scheduleInput, setScheduleInput] = useState("{}");
  const [loadingSchedule, setLoadingSchedule] = useState(props.adapter.run_mode === "schedule");
  const [savingRuntime, setSavingRuntime] = useState(false);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const watcher = useExecutionWatcher((message) => onError(message));
  const watchRef = useRef(watcher.watch);
  const refreshedTerminalId = useRef<number | null>(null);

  const refreshAdapter = useCallback(async () => {
    const refreshed = await api.getAdapter(adapterId);
    onAdapterChange(refreshed);
    setWorkerOverride(undefined);
    setRunModeOverride(undefined);
  }, [adapterId, onAdapterChange]);

  useEffect(() => {
    watchRef.current = watcher.watch;
  });

  const loadSchedule = useCallback(async () => {
    setLoadingSchedule(true);
    try {
      const loaded = await api.getSchedule(adapterId);
      setSchedule(loaded);
      setCron(loaded.cron);
      setTimezone(loaded.timezone);
      setScheduleInput(JSON.stringify(loaded.input, null, 2));
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && error.code === "schedule_not_configured") {
        setSchedule(null);
      } else {
        onError(errorMessage(error));
      }
    } finally {
      setLoadingSchedule(false);
    }
  }, [adapterId, onError]);

  useEffect(() => {
    if (adapterRunMode !== "schedule") {
      return;
    }
    let cancelled = false;
    api.getSchedule(adapterId).then((loaded) => {
      if (cancelled) return;
      setSchedule(loaded);
      setCron(loaded.cron);
      setTimezone(loaded.timezone);
      setScheduleInput(JSON.stringify(loaded.input, null, 2));
    }).catch((error) => {
      if (
        !cancelled &&
        !(error instanceof ApiError && error.status === 404 && error.code === "schedule_not_configured")
      ) {
        onError(errorMessage(error));
      }
    }).finally(() => {
      if (!cancelled) setLoadingSchedule(false);
    });
    return () => {
      cancelled = true;
    };
  }, [adapterId, adapterRunMode, onError]);

  useEffect(() => {
    const executionId = props.adapter.running_execution_id;
    if (executionId == null || watcher.execution?.id === executionId) {
      return;
    }
    api.getExecution(executionId).then((execution) => watchRef.current(execution)).catch((error) => onError(errorMessage(error)));
  }, [props.adapter.running_execution_id, onError, watcher.execution?.id]);

  useEffect(() => {
    const execution = watcher.execution;
    if (
      execution === null ||
      !isTerminal(execution.status) ||
      refreshedTerminalId.current === execution.id
    ) {
      return;
    }
    refreshedTerminalId.current = execution.id;
    void refreshAdapter().catch((error) => onError(errorMessage(error)));
  }, [onError, refreshAdapter, watcher.execution]);

  const workerId =
    workerOverride === undefined ? (props.adapter.runtime_worker_id ?? null) : workerOverride;
  const runMode = runModeOverride ?? props.adapter.run_mode;

  async function saveRuntimeSettings() {
    if (savingRuntime || props.adapter.runtime_locked) {
      return;
    }
    setSavingRuntime(true);
    onError(null);
    try {
      const refreshed = await api.updateAdapter(adapterId, {
        runtime_worker_id: workerId,
        run_mode: runMode,
      });
      onAdapterChange(refreshed);
      setWorkerOverride(undefined);
      setRunModeOverride(undefined);
      if (refreshed.run_mode === "schedule") setLoadingSchedule(true);
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSavingRuntime(false);
    }
  }

  async function saveSchedule(enabled: boolean) {
    const parsed = parseInput(scheduleInput);
    if (!parsed.ok) {
      onError("Schedule Input 必须是合法 JSON");
      return;
    }
    setSavingSchedule(true);
    onError(null);
    try {
      const saved = await api.putSchedule(adapterId, {
        enabled,
        cron,
        timezone,
        input: parsed.value,
      });
      setSchedule(saved);
      await refreshAdapter();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSavingSchedule(false);
    }
  }

  async function runOnce() {
    const text = props.adapter.run_mode === "schedule" ? scheduleInput : manualInput;
    const parsed = parseInput(text);
    if (!parsed.ok) {
      onError("Input 必须是合法 JSON");
      return;
    }
    setSubmitting(true);
    onError(null);
    try {
      const execution = await api.createExecution(adapterId, { input: parsed.value });
      refreshedTerminalId.current = null;
      watcher.watch(execution);
      await refreshAdapter();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function stopExecution() {
    const executionId = watcher.execution?.id ?? props.adapter.running_execution_id;
    if (executionId == null) {
      return;
    }
    setCancelling(true);
    onError(null);
    try {
      const execution = await api.cancelExecution(executionId);
      watcher.watch(execution);
      if (isTerminal(execution.status)) {
        await refreshAdapter();
      }
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setCancelling(false);
    }
  }

  const execution = watcher.execution;
  const activeExecution =
    props.adapter.running_execution_id != null ||
    (execution !== null && !isTerminal(execution.status));
  const runtimeLocked = props.adapter.runtime_locked === true;
  const scheduleEnabled = schedule?.enabled === true;
  const scheduleFieldsLocked = scheduleEnabled || runtimeLocked;
  const compatibleWorkers = props.workers.filter((worker) =>
    worker.capabilities.includes(props.adapter.language),
  );
  const canRun =
    !props.adapter.archived_at &&
    props.adapter.latest_version_id !== null &&
    props.adapter.runtime_worker_id != null &&
    !activeExecution &&
    !submitting;

  return (
    <div className="task-run-settings" data-testid="task-run-settings">
      <section className="task-runtime-config">
        <Typography.Title level={5}>运行设置</Typography.Title>
        <Space direction="vertical" size="middle" className="schedule-form">
          <label className="settings-field">
            <span className="settings-field-label">运行节点</span>
            <Select
              data-testid="task-runtime-worker"
              value={workerId ?? undefined}
              placeholder="请选择支持当前语言的 Worker"
              loading={props.workersLoading}
              disabled={runtimeLocked || savingRuntime || props.workersLoading}
              onChange={(value: number) => setWorkerOverride(value)}
              options={compatibleWorkers.map((worker) => ({
                value: worker.id,
                label: `${worker.name}（${worker.status === "online" ? "在线" : "离线"}）`,
                disabled: worker.status !== "online",
              }))}
            />
          </label>
          {props.workersError !== null && <Alert type="error" showIcon message={props.workersError} />}
          <div className="settings-field">
            <span className="settings-field-label">运行方式</span>
            <Radio.Group
              data-testid="task-run-mode"
              value={runMode}
              disabled={runtimeLocked || savingRuntime}
              onChange={(event) => setRunModeOverride(event.target.value as TaskRunMode)}
            >
              <Radio value="manual">手动运行</Radio>
              <Radio value="schedule">定时运行</Radio>
            </Radio.Group>
          </div>
          {runtimeLocked && (
            <Alert
              type="warning"
              showIcon
              data-testid="task-runtime-locked"
              message="运行配置已锁定"
              description="定时启用或 Execution 活跃期间，不能修改代码、Worker、运行方式、Cron、依赖、运行参数或凭据。"
            />
          )}
          <Button
            type="primary"
            data-testid="save-task-runtime"
            loading={savingRuntime}
            disabled={runtimeLocked || workerId === null}
            onClick={() => void saveRuntimeSettings()}
          >
            保存运行设置
          </Button>
        </Space>
      </section>

      {props.adapter.run_mode === "schedule" && (
        <section className="task-schedule-config">
          <Typography.Title level={5}>定时配置</Typography.Title>
          {loadingSchedule ? <Spin /> : (
            <Space direction="vertical" size="middle" className="schedule-form">
              <label className="settings-field">
                <span className="settings-field-label">Cron（5 字段）</span>
                <Input data-testid="task-schedule-cron" value={cron} disabled={scheduleFieldsLocked} onChange={(event) => setCron(event.target.value)} />
              </label>
              <label className="settings-field">
                <span className="settings-field-label">Timezone（IANA）</span>
                <Input data-testid="task-schedule-timezone" value={timezone} disabled={scheduleFieldsLocked} onChange={(event) => setTimezone(event.target.value)} />
              </label>
              <label className="settings-field">
                <span className="settings-field-label">Input（JSON）</span>
                <Input.TextArea data-testid="task-schedule-input" rows={4} value={scheduleInput} disabled={scheduleFieldsLocked} onChange={(event) => setScheduleInput(event.target.value)} />
              </label>
              <div className="settings-field">
                <span className="settings-field-label">状态</span>
                <Space>
                  <Tag color={scheduleEnabled ? "green" : "default"}>{scheduleEnabled ? "定时运行中" : "已停用"}</Tag>
                  <span data-testid="task-schedule-next-run">下一次执行：{formatTime(schedule?.next_run_at ?? null)}</span>
                  <Button size="small" onClick={() => void loadSchedule()}>刷新</Button>
                </Space>
              </div>
              <Space>
                {!scheduleEnabled && (
                  <Button data-testid="save-task-schedule" disabled={runtimeLocked} onClick={() => void saveSchedule(false)}>保存配置</Button>
                )}
                <Button
                  type={scheduleEnabled ? "default" : "primary"}
                  danger={scheduleEnabled}
                  data-testid={scheduleEnabled ? "disable-task-schedule" : "enable-task-schedule"}
                  loading={savingSchedule}
                  disabled={activeExecution && !scheduleEnabled}
                  onClick={() => void saveSchedule(!scheduleEnabled)}
                >
                  {scheduleEnabled ? "停用定时" : "启用定时"}
                </Button>
              </Space>
            </Space>
          )}
        </section>
      )}

      <section className="task-run-once">
        <Typography.Title level={5}>{props.adapter.run_mode === "schedule" ? "立即运行一次" : "手动运行"}</Typography.Title>
        {props.adapter.run_mode === "manual" && (
          <label className="settings-field">
            <span className="settings-field-label">Input（JSON）</span>
            <Input.TextArea data-testid="task-manual-input" rows={4} value={manualInput} disabled={activeExecution} onChange={(event) => setManualInput(event.target.value)} />
          </label>
        )}
        <Space>
          {!activeExecution ? (
            <Button type="primary" data-testid="task-run-once" loading={submitting} disabled={!canRun} onClick={() => void runOnce()}>
              {props.adapter.run_mode === "schedule" ? "立即运行一次" : "运行一次"}
            </Button>
          ) : (
            <Button danger data-testid="task-stop-run" loading={cancelling} onClick={() => void stopExecution()}>停止运行</Button>
          )}
          {execution !== null && <Tag color={statusColor(execution.status)}>{statusLabel(execution.status)}</Tag>}
        </Space>
        {props.adapter.latest_version_id === null && <Alert type="info" showIcon message="请先保存 Revision，再运行任务。" />}
        {props.adapter.runtime_worker_id == null && <Alert type="info" showIcon message="请先保存运行节点。" />}
        {execution !== null && (
          <div className="execution-body">
            <Typography.Text>Execution #{execution.id} · Trigger: {execution.trigger}</Typography.Text>
            <OutputView execution={execution} />
            <Typography.Text strong>stdout</Typography.Text>
            <LogView content={watcher.liveStdout} truncated={execution.stdout_truncated} />
            <Typography.Text strong>stderr</Typography.Text>
            <LogView content={watcher.liveStderr} truncated={execution.stderr_truncated} />
          </div>
        )}
      </section>
    </div>
  );
}
