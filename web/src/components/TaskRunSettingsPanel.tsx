/** Task 运行设置：单页动态表单（M5.5.11）。
 *
 * 本页只负责配置，不承担“运行一次”操作（“运行一次”仅在右上角全局操作区，
 * 并受 #55 的未保存门禁约束）。固定结构为“运行节点 + 运行方式”，切换
 * “手动运行 / 定时运行”后立即呈现对应字段；单次执行超时是 Adapter 级配置，
 * 手动与定时共用（默认 300 秒，1/5/10/30/60 分钟预设 + 自定义，最大 24 小时，
 * 不提供“无限制”），页面底部统一保存运行配置。
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Alert, Button, Input, InputNumber, Radio, Select, Space, Spin, Tag, Typography } from "antd";

import { ApiError, api } from "../api";
import { isTerminal } from "../status";
import type { Adapter, AdapterSchedule, Execution, TaskRunMode, Worker } from "../types";
import { userErrorMessage } from "../user-message";

export interface TaskRuntimeState {
  scheduleEnabled: boolean;
  loading: boolean;
  activeExecution: boolean;
  canRun: boolean;
  scheduleEnableBlockedReason: string | null;
}

export interface TaskRunSettingsHandle {
  runOnce: () => void;
  stopExecution: () => void;
  toggleSchedule: () => void;
}

interface TaskRunSettingsPanelProps {
  adapter: Adapter;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  execution: Execution | null;
  /** M5.5.9：编辑页存在未保存修改时禁止启动运行。 */
  dirty: boolean;
  onAdapterChange: (adapter: Adapter) => void;
  onExecutionStarted: (execution: Execution) => void;
  onRuntimeStateChange: (state: TaskRuntimeState) => void;
  onError: (message: string | null) => void;
}

// M5.5.11 单次执行超时合同（秒为权威值）。
const DEFAULT_TIMEOUT_SECONDS = 300;
const MAX_TIMEOUT_SECONDS = 24 * 60 * 60; // 24 小时
const TIMEOUT_PRESET_MINUTES = [1, 5, 10, 30, 60] as const;

const TIMEOUT_HINT =
  "一次运行超过该时间后，系统将自动停止任务并标记为“超时”。";

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
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

function presetMinutesFor(seconds: number): number | undefined {
  return TIMEOUT_PRESET_MINUTES.find((minutes) => minutes * 60 === seconds);
}

const TaskRunSettingsPanel = forwardRef<TaskRunSettingsHandle, TaskRunSettingsPanelProps>(function TaskRunSettingsPanel(props, ref) {
  const adapterId = props.adapter.id;
  const onAdapterChange = props.onAdapterChange;
  const onError = props.onError;
  const onRuntimeStateChange = props.onRuntimeStateChange;
  const [workerOverride, setWorkerOverride] = useState<number | null | undefined>(undefined);
  const [runModeOverride, setRunModeOverride] = useState<TaskRunMode | undefined>(undefined);
  // M5.5.11: 表单内超时值（秒）；null = 跟随 Adapter 保存值。
  const [timeoutOverride, setTimeoutOverride] = useState<number | null>(null);
  const [timeoutCustomMode, setTimeoutCustomMode] = useState(false);
  const [manualInput, setManualInput] = useState("{}");
  const [schedule, setSchedule] = useState<AdapterSchedule | null>(null);
  const [cron, setCron] = useState("*/5 * * * *");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [scheduleInput, setScheduleInput] = useState("{}");
  const [loadingSchedule, setLoadingSchedule] = useState(props.adapter.run_mode === "schedule");
  // 用户是否实际修改过定时字段（cron/timezone/input）。未修改时统一保存
  // 不得用表单值整体 PUT，避免把线上真实 Schedule 冲掉。
  const [scheduleTouched, setScheduleTouched] = useState(false);
  // 初始 Schedule GET 非 404 失败时置位：此时表单仍是默认值，禁止 PUT。
  const [scheduleLoadFailed, setScheduleLoadFailed] = useState(false);
  const [savingRuntime, setSavingRuntime] = useState(false);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  // 保存流程（PATCH + PUT）完成后递增，使保存前发出的 Schedule GET 响应
  // 成为陈旧信号，不能覆盖刚保存成功的表单值。
  const scheduleLoadEpoch = useRef(0);
  const runMode = runModeOverride ?? props.adapter.run_mode;

  const refreshAdapter = useCallback(async () => {
    const refreshed = await api.getAdapter(adapterId);
    onAdapterChange(refreshed);
    setWorkerOverride(undefined);
    setRunModeOverride(undefined);
    setTimeoutOverride(null);
    setTimeoutCustomMode(false);
  }, [adapterId, onAdapterChange]);

  const loadSchedule = useCallback(async () => {
    setLoadingSchedule(true);
    try {
      const loaded = await api.getSchedule(adapterId);
      setSchedule(loaded);
      setCron(loaded.cron);
      setTimezone(loaded.timezone);
      setScheduleInput(JSON.stringify(loaded.input, null, 2));
      setScheduleLoadFailed(false);
      setScheduleTouched(false);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && error.code === "schedule_not_configured") {
        setSchedule(null);
        setScheduleLoadFailed(false);
      } else {
        // 加载失败时表单仍是默认值：标记后统一保存会跳过 Schedule PUT。
        setScheduleLoadFailed(true);
        onError(errorMessage(error));
      }
    } finally {
      setLoadingSchedule(false);
    }
  }, [adapterId, onError]);

  useEffect(() => {
    if (runMode !== "schedule") {
      return;
    }
    let cancelled = false;
    const epoch = scheduleLoadEpoch.current;
    api.getSchedule(adapterId).then((loaded) => {
      if (cancelled || scheduleLoadEpoch.current !== epoch) return;
      setSchedule(loaded);
      setCron(loaded.cron);
      setTimezone(loaded.timezone);
      setScheduleInput(JSON.stringify(loaded.input, null, 2));
      setScheduleLoadFailed(false);
      setScheduleTouched(false);
    }).catch((error) => {
      // 404 = Schedule 尚未配置：保持空状态即可。保存流程里 PATCH 先于 PUT，
      // 迟到的 404 响应是陈旧信号，必须忽略，不能覆盖刚保存成功的 Schedule。
      if (!cancelled && !(error instanceof ApiError && error.status === 404)) {
        setScheduleLoadFailed(true);
        onError(errorMessage(error));
      }
    }).finally(() => {
      if (!cancelled) setLoadingSchedule(false);
    });
    return () => {
      cancelled = true;
    };
  }, [adapterId, onError, runMode]);

  const workerId =
    workerOverride === undefined ? (props.adapter.runtime_worker_id ?? null) : workerOverride;

  // M5.5.11: 表单显示值 = 表单覆盖 ?? Adapter 权威值 ?? 默认 300 秒。
  const effectiveTimeoutSeconds =
    timeoutOverride ?? props.adapter.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS;
  const effectiveCustom =
    timeoutCustomMode || presetMinutesFor(effectiveTimeoutSeconds) === undefined;

  function resolveTimeoutSeconds(): number | null {
    const minutes = presetMinutesFor(effectiveTimeoutSeconds);
    if (!effectiveCustom && minutes !== undefined) {
      return minutes * 60;
    }
    if (!Number.isInteger(effectiveTimeoutSeconds) || effectiveTimeoutSeconds < 1 || effectiveTimeoutSeconds > MAX_TIMEOUT_SECONDS) {
      onError(`单次执行超时必须是 1–${MAX_TIMEOUT_SECONDS} 秒（最大 24 小时）。`);
      return null;
    }
    return effectiveTimeoutSeconds;
  }

  async function saveRunConfig() {
    if (savingRuntime || props.adapter.runtime_locked) {
      return;
    }
    if (workerId === null) {
      onError("请先选择运行节点。");
      return;
    }
    const timeoutSeconds = resolveTimeoutSeconds();
    if (timeoutSeconds === null) {
      return;
    }
    const scheduleDraft =
      runMode === "schedule"
        ? parseInput(scheduleInput)
        : null;
    if (scheduleDraft !== null && !scheduleDraft.ok) {
      onError("Schedule Input 必须是合法 JSON");
      return;
    }
    setSavingRuntime(true);
    onError(null);
    try {
      const refreshed = await api.updateAdapter(adapterId, {
        runtime_worker_id: workerId,
        run_mode: runMode,
        timeout_seconds: timeoutSeconds,
      });
      onAdapterChange(refreshed);
      setWorkerOverride(undefined);
      setRunModeOverride(undefined);
      setTimeoutOverride(null);
      setTimeoutCustomMode(false);
      if (refreshed.run_mode === "schedule") {
        // 只在两种情况下整体 PUT Schedule：用户实际修改过定时字段，或确认
        // 从未配置过（GET 已返回 404）。加载中/加载失败时表单仍是默认值，
        // 此时 PUT 会静默冲掉线上真实配置，必须跳过并明确提示。
        const scheduleUnknown =
          schedule === null && (loadingSchedule || scheduleLoadFailed);
        const shouldPutSchedule = scheduleTouched || (schedule === null && !scheduleUnknown);
        if (shouldPutSchedule) {
          const saved = await api.putSchedule(adapterId, {
            enabled: schedule?.enabled === true,
            cron,
            timezone,
            input: scheduleDraft?.ok === true ? scheduleDraft.value : null,
          });
          // 保存成功后的值立即落回表单，并作废任何在途的 Schedule GET。
          scheduleLoadEpoch.current += 1;
          setSchedule(saved);
          setCron(saved.cron);
          setTimezone(saved.timezone);
          setScheduleInput(JSON.stringify(saved.input, null, 2));
          setScheduleTouched(false);
        } else if (scheduleUnknown) {
          onError("运行配置已保存；但 Schedule 尚未加载完成（或加载失败），定时字段未保存，请刷新后重试。");
        }
      }
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSavingRuntime(false);
    }
  }

  async function saveSchedule(enabled: boolean) {
    if (enabled && activeExecution) {
      onError("当前执行仍在运行，请等待终态或停止当前执行后再启用定时");
      return;
    }
    if (enabled && scheduleEnableBlockedReason !== null) {
      onError(scheduleEnableBlockedReason);
      return;
    }
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
    // M5.5.9：未保存修改时不得启动运行，先保存。
    if (props.dirty) {
      onError("请先保存当前修改，再运行。");
      return;
    }
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
      props.onExecutionStarted(execution);
      await refreshAdapter();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function stopExecution() {
    const watchedExecutionId =
      props.execution !== null &&
      props.execution.adapter_id === adapterId &&
      !isTerminal(props.execution.status)
        ? props.execution.id
        : null;
    const executionId = props.adapter.running_execution_id ?? watchedExecutionId;
    if (executionId == null) {
      return;
    }
    setCancelling(true);
    onError(null);
    try {
      const execution = await api.cancelExecution(executionId);
      props.onExecutionStarted(execution);
      if (isTerminal(execution.status)) {
        await refreshAdapter();
      }
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setCancelling(false);
    }
  }

  const execution = props.execution;
  const activeExecution =
    props.adapter.running_execution_id != null ||
    (execution !== null && !isTerminal(execution.status));
  const runtimeLocked = props.adapter.runtime_locked === true;
  const scheduleEnabled = schedule?.enabled === true;
  const scheduleFieldsLocked = scheduleEnabled || runtimeLocked;
  const scheduleConfigMissing =
    runMode === "schedule" && schedule === null && !loadingSchedule && !scheduleLoadFailed;
  const runtimeConfigDirty =
    runMode !== props.adapter.run_mode ||
    workerId !== (props.adapter.runtime_worker_id ?? null) ||
    effectiveTimeoutSeconds !== (props.adapter.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS) ||
    scheduleTouched;
  const scheduleEnableBlockedReason = (() => {
    if (loadingSchedule) return "定时配置正在加载，请稍候。";
    if (scheduleLoadFailed) return "定时配置加载失败，请刷新后重试。";
    if (props.adapter.latest_version_id === null) return "请先保存适配器，再启用定时。";
    if (props.adapter.runtime_worker_id == null) return "请先保存运行节点，再启用定时。";
    if (props.dirty) return "请先保存当前修改，再启用定时。";
    if (runtimeConfigDirty) return "运行设置有未保存修改，请先保存运行配置。";
    if (scheduleConfigMissing) return "请先保存运行配置，再启用定时。";
    return null;
  })();
  const compatibleWorkers = props.workers.filter((worker) =>
    worker.capabilities.includes(props.adapter.language),
  );
  const canRun =
    !props.dirty &&
    !props.adapter.archived_at &&
    props.adapter.latest_version_id !== null &&
    props.adapter.runtime_worker_id != null &&
    !activeExecution &&
    !submitting;

  useEffect(() => {
    onRuntimeStateChange({
      scheduleEnabled,
      loading: loadingSchedule || savingRuntime || savingSchedule || submitting || cancelling,
      activeExecution,
      canRun,
      scheduleEnableBlockedReason,
    });
  }, [
    activeExecution,
    canRun,
    cancelling,
    loadingSchedule,
    onRuntimeStateChange,
    savingRuntime,
    savingSchedule,
    scheduleEnableBlockedReason,
    scheduleEnabled,
    submitting,
  ]);

  useImperativeHandle(ref, () => ({
    runOnce: () => void runOnce(),
    stopExecution: () => void stopExecution(),
    toggleSchedule: () => void saveSchedule(!scheduleEnabled),
  }));

  const scheduleVisible = runMode === "schedule";

  return (
    <div className="task-run-settings" data-testid="task-run-settings">
      <section className="task-runtime-config">
        <Typography.Title level={5}>任务运行设置</Typography.Title>
        <Space direction="vertical" size="middle" className="schedule-form">
          <label className="settings-field">
            <span className="settings-field-label">运行节点</span>
            <Select
              data-testid="task-runtime-worker"
              value={workerId ?? undefined}
              placeholder="请选择支持当前语言的运行节点"
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
              onChange={(event) => {
                const nextRunMode = event.target.value as TaskRunMode;
                setRunModeOverride(nextRunMode);
                // 初始 manual 模式尚未读取 Schedule；切换时先进入加载态，
                // 防止 effect 发起 GET 前保存默认值覆盖已有的停用配置。
                setLoadingSchedule(nextRunMode === "schedule");
              }}
            >
              <Radio value="manual">手动运行</Radio>
              <Radio value="schedule">定时运行</Radio>
            </Radio.Group>
          </div>
          <div className="settings-field">
            <span className="settings-field-label">单次执行超时（一次运行的最长时间）</span>
            <Radio.Group
              data-testid="task-timeout-preset"
              value={effectiveCustom ? "custom" : presetMinutesFor(effectiveTimeoutSeconds)}
              disabled={runtimeLocked || savingRuntime}
              onChange={(event) => {
                const value = event.target.value as number | "custom";
                if (value === "custom") {
                  setTimeoutCustomMode(true);
                } else {
                  setTimeoutOverride(value * 60);
                  setTimeoutCustomMode(false);
                }
              }}
            >
              {TIMEOUT_PRESET_MINUTES.map((minutes) => (
                <Radio key={minutes} value={minutes}>
                  {minutes} 分钟
                </Radio>
              ))}
              <Radio value="custom">自定义</Radio>
            </Radio.Group>
            {effectiveCustom && (
              <InputNumber
                data-testid="task-timeout-custom"
                min={1}
                max={MAX_TIMEOUT_SECONDS}
                precision={0}
                value={effectiveTimeoutSeconds}
                addonAfter="秒"
                disabled={runtimeLocked || savingRuntime}
                onChange={(value) => setTimeoutOverride(value ?? null)}
              />
            )}
            <Typography.Text type="secondary" className="settings-field-hint">
              {TIMEOUT_HINT}
            </Typography.Text>
          </div>
          {runtimeLocked && (
            <Alert
              type="warning"
              showIcon
              data-testid="task-runtime-locked"
              message="运行配置已锁定"
              description="定时启用或执行活跃期间，不能修改代码、运行节点、运行方式、单次执行超时、Cron、依赖、运行参数或凭据。"
            />
          )}
        </Space>
      </section>

      {scheduleVisible && (
        <section className="task-schedule-config">
          <Typography.Title level={5}>定时配置</Typography.Title>
          {loadingSchedule ? <Spin /> : (
            <Space direction="vertical" size="middle" className="schedule-form">
              <label className="settings-field">
                <span className="settings-field-label">Cron（5 字段）</span>
                <Input data-testid="task-schedule-cron" value={cron} disabled={scheduleFieldsLocked} onChange={(event) => { setCron(event.target.value); setScheduleTouched(true); }} />
              </label>
              <label className="settings-field">
                <span className="settings-field-label">时区（IANA）</span>
                <Input data-testid="task-schedule-timezone" value={timezone} disabled={scheduleFieldsLocked} onChange={(event) => { setTimezone(event.target.value); setScheduleTouched(true); }} />
              </label>
              <label className="settings-field">
                <span className="settings-field-label">输入（JSON）</span>
                <Input.TextArea data-testid="task-schedule-input" rows={4} value={scheduleInput} disabled={scheduleFieldsLocked} onChange={(event) => { setScheduleInput(event.target.value); setScheduleTouched(true); }} />
              </label>
              <div className="settings-field">
                <span className="settings-field-label">定时状态</span>
                <Space>
                  <Tag color={scheduleEnabled ? "green" : "default"}>{scheduleEnabled ? "定时运行中" : "已停用"}</Tag>
                  <span data-testid="task-schedule-next-run">下一次执行：{formatTime(schedule?.next_run_at ?? null)}</span>
                  <Button size="small" onClick={() => void loadSchedule()}>刷新</Button>
                </Space>
              </div>
            </Space>
          )}
        </section>
      )}

      {runMode === "manual" && (
        <section className="task-manual-config">
          <Typography.Title level={5}>手动运行配置</Typography.Title>
          <label className="settings-field">
            <span className="settings-field-label">输入（JSON）</span>
            <Input.TextArea data-testid="task-manual-input" rows={4} value={manualInput} disabled={activeExecution} onChange={(event) => setManualInput(event.target.value)} />
          </label>
        </section>
      )}

      <Button
        type="primary"
        className="task-run-config-save"
        data-testid="save-task-runtime"
        loading={savingRuntime}
        disabled={runtimeLocked || workerId === null}
        onClick={() => void saveRunConfig()}
      >
        保存运行配置
      </Button>
    </div>
  );
});

export default TaskRunSettingsPanel;
