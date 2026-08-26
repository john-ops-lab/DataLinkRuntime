/** Task 运行设置：运行参数与唯一 Input Object 配置（M5.5.11 / Issue #127 A2）。
 *
 * 运行方式只决定 Task 的触发配置；输入对象是一个独立的 Adapter 资源。
 * manual、schedule 与 schedule 的“立即运行一次”都读取同一个已保存
 * InputConfig，页面不再维护两套 JSON 编辑器，也不向 Execution API 传入 input。
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Alert, Button, Card, Form, Input, InputNumber, Radio, Select, Space, Spin, Tag, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, api } from "../api";
import { isTerminal } from "../status";
import type {
  Adapter,
  AdapterInputConfig,
  AdapterSchedule,
  Execution,
  InputSourceType,
  TaskRunMode,
  Worker,
} from "../types";
import { userErrorMessage } from "../user-message";

export interface TaskRuntimeState {
  scheduleEnabled: boolean;
  loading: boolean;
  activeExecution: boolean;
  canRun: boolean;
  scheduleEnableBlockedReason: string | null;
  /** Optional for callers that construct the state outside this component. */
  runBlockedReason?: string | null;
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
  readOnly?: boolean;
}

// M5.5.11 单次执行超时合同（秒为权威值）。
const DEFAULT_TIMEOUT_SECONDS = 300;
const MAX_TIMEOUT_SECONDS = 24 * 60 * 60; // 24 小时
const TIMEOUT_PRESET_MINUTES = [1, 5, 10, 30, 60] as const;

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "null";
}

function parseJson(text: string): { ok: true; value: unknown } | { ok: false } {
  if (text.trim() === "") {
    return { ok: false };
  }
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false };
  }
}

type InputErrorTranslationKey = "revisionConflict" | "sourceNotAvailable" | "notInitialized" | "managedFilesEmpty";

function localizedInputReason(
  codeOrReason: string | null,
  reason: unknown,
  translate: (key: InputErrorTranslationKey) => string,
): string | null {
  const key = codeOrReason === "input_config_revision_conflict"
    ? "revisionConflict"
    : codeOrReason === "input_source_not_available"
      ? "sourceNotAvailable"
      : codeOrReason === "input_config_not_initialized"
        ? "notInitialized"
        : codeOrReason === "managed_files_empty" || (codeOrReason === "input_invalid" && reason === "managed_files_empty")
          ? "managedFilesEmpty"
          : null;
  return key === null ? null : translate(key);
}

function formatTime(value: string | null, locale: "zh-CN" | "en"): string {
  return value === null
    ? "—"
    : new Date(value).toLocaleString(locale === "en" ? "en-US" : "zh-CN");
}

function presetMinutesFor(seconds: number): number | undefined {
  return TIMEOUT_PRESET_MINUTES.find((minutes) => minutes * 60 === seconds);
}

const TaskRunSettingsPanel = forwardRef<TaskRunSettingsHandle, TaskRunSettingsPanelProps>(function TaskRunSettingsPanel(props, ref) {
  const { i18n, t } = useTranslation(["runtime", "common", "adapter"]);
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const adapterId = props.adapter.id;
  const readOnly = props.readOnly === true;
  const onAdapterChange = props.onAdapterChange;
  const onError = props.onError;
  const onRuntimeStateChange = props.onRuntimeStateChange;
  const [workerOverride, setWorkerOverride] = useState<number | null | undefined>(undefined);
  const [runModeOverride, setRunModeOverride] = useState<TaskRunMode | undefined>(undefined);
  // M5.5.11: 表单内超时值（秒）；null = 跟随 Adapter 保存值。
  const [timeoutOverride, setTimeoutOverride] = useState<number | null>(null);
  const [timeoutCustomMode, setTimeoutCustomMode] = useState(false);
  const [schedule, setSchedule] = useState<AdapterSchedule | null>(null);
  const [cron, setCron] = useState("*/5 * * * *");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [loadingSchedule, setLoadingSchedule] = useState(props.adapter.run_mode === "schedule");
  // 用户是否实际修改过定时字段（cron/timezone）。未修改时统一保存
  // 不得用表单值整体 PUT，避免把线上真实 Schedule 冲掉。
  const [scheduleTouched, setScheduleTouched] = useState(false);
  // 初始 Schedule GET 非 404 失败时置位：此时表单仍是默认值，禁止 PUT。
  const [scheduleLoadFailed, setScheduleLoadFailed] = useState(false);
  const [savingRuntime, setSavingRuntime] = useState(false);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  // Input Object is one resource with one saved revision and one draft. These
  // states intentionally do not depend on runMode, so changing manual/schedule
  // cannot reset an unsaved JSON draft.
  const [inputConfig, setInputConfig] = useState<AdapterInputConfig | null>(null);
  const [inputSourceDraft, setInputSourceDraft] = useState<InputSourceType>("none");
  const [inputJsonDraft, setInputJsonDraft] = useState("null");
  const [loadingInput, setLoadingInput] = useState(true);
  const [inputLoadFailed, setInputLoadFailed] = useState(false);
  const [savingInput, setSavingInput] = useState(false);
  const [inputValidationError, setInputValidationError] = useState<string | null>(null);

  // 保存流程（PATCH + PUT）完成后递增，使保存前发出的 Schedule GET 响应
  // 成为陈旧信号，不能覆盖刚保存成功的表单值。
  const scheduleLoadEpoch = useRef(0);
  const inputLoadEpoch = useRef(0);
  const runMode = runModeOverride ?? props.adapter.run_mode;

  const refreshAdapter = useCallback(async () => {
    const refreshed = await api.getAdapter(adapterId);
    onAdapterChange(refreshed);
    setWorkerOverride(undefined);
    setRunModeOverride(undefined);
    setTimeoutOverride(null);
    setTimeoutCustomMode(false);
  }, [adapterId, onAdapterChange]);

  const loadInputConfig = useCallback(async () => {
    const epoch = inputLoadEpoch.current + 1;
    inputLoadEpoch.current = epoch;
    setLoadingInput(true);
    setInputLoadFailed(false);
    try {
      const loaded = await api.getInputConfig(adapterId);
      if (inputLoadEpoch.current !== epoch) {
        return;
      }
      setInputConfig(loaded);
      setInputSourceDraft(loaded.source_type);
      setInputJsonDraft(loaded.source_type === "json" ? formatJson(loaded.json_value) : "null");
      setInputValidationError(null);
    } catch (error) {
      if (inputLoadEpoch.current === epoch) {
        setInputLoadFailed(true);
        onError(errorMessage(error));
      }
    } finally {
      if (inputLoadEpoch.current === epoch) {
        setLoadingInput(false);
      }
    }
  }, [adapterId, onError]);

  useEffect(() => {
    void loadInputConfig();
    return () => {
      inputLoadEpoch.current += 1;
    };
  }, [loadInputConfig]);

  const loadSchedule = useCallback(async () => {
    setLoadingSchedule(true);
    try {
      const loaded = await api.getSchedule(adapterId);
      setSchedule(loaded);
      setCron(loaded.cron);
      setTimezone(loaded.timezone);
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
      onError(t("task.settings.invalidTimeout", { max: MAX_TIMEOUT_SECONDS }));
      return null;
    }
    return effectiveTimeoutSeconds;
  }

  function localizedInputError(error: unknown): string {
    if (error instanceof ApiError) {
      const localizedReason = localizedInputReason(
        error.code,
        error.params.reason,
        (key) => t(`task.input.errors.${key}`),
      );
      if (localizedReason !== null) {
        return userErrorMessage(error, localizedReason);
      }
    }
    return errorMessage(error);
  }

  async function saveInputObject() {
    if (
      readOnly ||
      savingInput ||
      loadingInput ||
      inputConfig === null ||
      inputSourceDraft === "managed_files" ||
      inputSourceDraft === "remote_files" ||
      props.adapter.runtime_locked === true ||
      schedule?.enabled === true
    ) {
      return;
    }

    const payload = inputSourceDraft === "none"
      ? { expected_revision: inputConfig.revision, source_type: "none" as const }
      : (() => {
          const parsed = parseJson(inputJsonDraft);
          if (!parsed.ok) {
            return null;
          }
          return {
            expected_revision: inputConfig.revision,
            source_type: "json" as const,
            json_value: parsed.value,
          };
        })();
    if (payload === null) {
      const message = t("task.input.invalidJson");
      setInputValidationError(message);
      onError(message);
      return;
    }

    setSavingInput(true);
    setInputValidationError(null);
    onError(null);
    try {
      const saved = await api.putInputConfig(adapterId, payload);
      setInputConfig(saved);
      setInputSourceDraft(saved.source_type);
      setInputJsonDraft(saved.source_type === "json" ? formatJson(saved.json_value) : "null");
      setInputValidationError(null);
    } catch (error) {
      // Keep both the last valid server resource and the user's draft intact.
      // In particular, a 409 must not silently replace the stale page's text.
      const message = localizedInputError(error);
      setInputValidationError(message);
      onError(message);
    } finally {
      setSavingInput(false);
    }
  }

  async function saveRunConfig() {
    if (readOnly || savingRuntime || props.adapter.runtime_locked) {
      return;
    }
    if (workerId === null) {
      onError(t("task.settings.chooseWorker"));
      return;
    }
    const timeoutSeconds = resolveTimeoutSeconds();
    if (timeoutSeconds === null) {
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
          });
          // 保存成功后的值立即落回表单，并作废任何在途的 Schedule GET。
          scheduleLoadEpoch.current += 1;
          setSchedule(saved);
          setCron(saved.cron);
          setTimezone(saved.timezone);
          setScheduleTouched(false);
        } else if (scheduleUnknown) {
          onError(t("task.settings.savedScheduleNotLoaded"));
        }
      }
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSavingRuntime(false);
    }
  }

  async function saveSchedule(enabled: boolean) {
    if (readOnly) {
      return;
    }
    if (enabled && activeExecution) {
      onError(t("task.reasons.activeSchedule"));
      return;
    }
    if (enabled && scheduleEnableBlockedReason !== null) {
      onError(scheduleEnableBlockedReason);
      return;
    }
    setSavingSchedule(true);
    onError(null);
    try {
      // InputConfig is the only input editor. Schedule PUT keeps the legacy
      // mirror server-owned by omitting its old input field entirely.
      const saved = await api.putSchedule(adapterId, {
        enabled,
        cron,
        timezone,
      });
      setSchedule(saved);
      setScheduleTouched(false);
      await refreshAdapter();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSavingSchedule(false);
    }
  }

  async function runOnce() {
    if (!canRun) {
      onError(runBlockedReason ?? t("task.reasons.unavailable"));
      return;
    }
    setSubmitting(true);
    onError(null);
    try {
      // Control resolves the saved InputConfig. The new Web never sends an
      // input override, including for schedule-mode run-now.
      const execution = await api.createExecution(adapterId);
      props.onExecutionStarted(execution);
      await refreshAdapter();
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function stopExecution() {
    if (readOnly) {
      return;
    }
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

  const inputDirty = inputConfig !== null && (
    inputSourceDraft !== inputConfig.source_type ||
    (inputSourceDraft === "json" && inputJsonDraft !== formatJson(inputConfig.json_value))
  );
  const inputInvalidReason = inputConfig?.invalid_reason ?? null;
  const inputInvalidMessage = localizedInputReason(
    inputInvalidReason,
    undefined,
    (key) => t(`task.input.errors.${key}`),
  );
  const inputBlockedReason = (() => {
    if (loadingInput) return t("task.input.loading");
    if (inputLoadFailed || inputConfig === null) return t("task.input.loadFailed");
    if (inputDirty) return t("task.input.saveBeforeRun");
    if (!inputConfig.valid_for_run) {
      if (inputInvalidMessage !== null) return inputInvalidMessage;
      return t("task.input.invalidConfig");
    }
    return null;
  })();

  const scheduleEnableBlockedReason = (() => {
    if (loadingSchedule) return t("task.reasons.loadingSchedule");
    if (scheduleLoadFailed) return t("task.reasons.scheduleLoadFailed");
    if (inputBlockedReason !== null) return inputBlockedReason;
    if (props.adapter.latest_version_id === null) return t("task.reasons.saveVersionFirst");
    if (props.adapter.runtime_worker_id == null) return t("task.reasons.saveWorkerFirst");
    if (props.dirty) return t("task.reasons.saveChangesFirst");
    if (runtimeConfigDirty) return t("task.reasons.runtimeDirty");
    if (scheduleConfigMissing) return t("task.reasons.saveRuntimeFirst");
    return null;
  })();

  const compatibleWorkers = props.workers.filter((worker) =>
    worker.capabilities.includes(props.adapter.language),
  );
  const canRun =
    !readOnly &&
    !props.dirty &&
    inputBlockedReason === null &&
    !props.adapter.archived_at &&
    props.adapter.latest_version_id !== null &&
    props.adapter.runtime_worker_id != null &&
    !activeExecution &&
    !submitting;
  const runBlockedReason = (() => {
    if (readOnly) return t("task.reasons.readOnly");
    if (props.dirty) return t("task.reasons.dirtyRun");
    if (props.adapter.archived_at) return t("task.reasons.deleted");
    if (inputBlockedReason !== null) return inputBlockedReason;
    if (props.adapter.latest_version_id === null) return t("task.reasons.noVersion");
    if (props.adapter.runtime_worker_id == null) return t("task.reasons.noWorker");
    if (activeExecution) return t("task.reasons.activeRun");
    if (submitting) return t("task.reasons.processing");
    // 防御性兜底：未来若新增 canRun 条件但遗漏对应文案，命令式入口仍保持阻断。
    return canRun ? null : t("task.reasons.unavailable");
  })();

  const inputEditingLocked =
    readOnly || runtimeLocked || scheduleEnabled || savingInput || loadingInput || inputLoadFailed || inputConfig === null;
  const sourceCards: {
    sourceType: InputSourceType;
    title: string;
    description: string;
    status: string;
    disabledReason: string | null;
  }[] = [
    {
      sourceType: "none",
      title: t("input.sources.none.title", { ns: "adapter" }),
      description: t("input.sources.none.description", { ns: "adapter" }),
      status: t("input.sources.none.status", { ns: "adapter" }),
      disabledReason: inputEditingLocked ? t("task.input.locked") : null,
    },
    {
      sourceType: "json",
      title: t("input.sources.json.title", { ns: "adapter" }),
      description: t("input.sources.json.description", { ns: "adapter" }),
      status: t("input.sources.json.status", { ns: "adapter" }),
      disabledReason: inputEditingLocked ? t("task.input.locked") : null,
    },
    {
      sourceType: "managed_files",
      title: t("input.sources.managedFiles.title", { ns: "adapter" }),
      description: t("input.sources.managedFiles.description", { ns: "adapter" }),
      status: t("input.sources.managedFiles.disabled", { ns: "adapter" }),
      disabledReason: t("input.sources.managedFiles.disabled", { ns: "adapter" }),
    },
    {
      sourceType: "remote_files",
      title: t("input.sources.remoteFiles.title", { ns: "adapter" }),
      description: t("input.sources.remoteFiles.description", { ns: "adapter" }),
      status: t("input.sources.remoteFiles.disabled", { ns: "adapter" }),
      disabledReason: t("input.sources.remoteFiles.disabled", { ns: "adapter" }),
    },
  ];

  function selectInputSource(sourceType: InputSourceType, disabledReason: string | null): void {
    if (disabledReason !== null) {
      return;
    }
    setInputSourceDraft(sourceType);
    setInputValidationError(null);
    onError(null);
  }

  useEffect(() => {
    onRuntimeStateChange({
      scheduleEnabled,
      loading: loadingInput || loadingSchedule || savingRuntime || savingInput || savingSchedule || submitting || cancelling,
      activeExecution,
      canRun,
      scheduleEnableBlockedReason,
      runBlockedReason,
    });
  }, [
    activeExecution,
    canRun,
    cancelling,
    loadingInput,
    loadingSchedule,
    onRuntimeStateChange,
    runBlockedReason,
    savingInput,
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
        <Typography.Title level={5}>{t("task.settings.title")}</Typography.Title>
        <Space direction="vertical" size="middle" className="schedule-form">
          <label className="settings-field">
            <span className="settings-field-label">{t("task.settings.worker")}</span>
            <Select
              data-testid="task-runtime-worker"
              value={workerId ?? undefined}
              placeholder={t("task.settings.workerPlaceholder")}
              loading={props.workersLoading}
              disabled={readOnly || runtimeLocked || savingRuntime || props.workersLoading}
              onChange={(value: number) => setWorkerOverride(value)}
              options={compatibleWorkers.map((worker) => ({
                value: worker.id,
                label: t("worker.option", {
                  ns: "common",
                  name: worker.name,
                  status: worker.status === "online"
                    ? t("worker.online", { ns: "common" })
                    : t("worker.offline", { ns: "common" }),
                }),
                disabled: worker.status !== "online",
              }))}
            />
          </label>
          {props.workersError !== null && <Alert type="error" showIcon message={props.workersError} />}
          <div className="settings-field">
            <span className="settings-field-label">{t("task.settings.mode")}</span>
            <Radio.Group
              data-testid="task-run-mode"
              value={runMode}
              disabled={readOnly || runtimeLocked || savingRuntime}
              onChange={(event) => {
                const nextRunMode = event.target.value as TaskRunMode;
                setRunModeOverride(nextRunMode);
                // 初始 manual 模式尚未读取 Schedule；切换时先进入加载态，
                // 防止 effect 发起 GET 前保存默认值覆盖已有的停用配置。
                setLoadingSchedule(nextRunMode === "schedule");
              }}
            >
              <Radio value="manual">{t("task.settings.manual")}</Radio>
              <Radio value="schedule">{t("task.settings.schedule")}</Radio>
            </Radio.Group>
          </div>
          <div className="settings-field">
            <span className="settings-field-label">{t("task.settings.timeout")}</span>
            <Radio.Group
              data-testid="task-timeout-preset"
              value={effectiveCustom ? "custom" : presetMinutesFor(effectiveTimeoutSeconds)}
              disabled={readOnly || runtimeLocked || savingRuntime}
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
                  {t("units.minutes", { value: minutes })}
                </Radio>
              ))}
              <Radio value="custom">{t("task.settings.custom")}</Radio>
            </Radio.Group>
            {effectiveCustom && (
              <InputNumber
                data-testid="task-timeout-custom"
                min={1}
                max={MAX_TIMEOUT_SECONDS}
                precision={0}
                value={effectiveTimeoutSeconds}
                addonAfter={t("task.settings.seconds")}
                disabled={readOnly || runtimeLocked || savingRuntime}
                onChange={(value) => setTimeoutOverride(value ?? null)}
              />
            )}
            <Typography.Text type="secondary" className="settings-field-hint">
              {t("task.settings.timeoutHint")}
            </Typography.Text>
          </div>
          {runtimeLocked && (
            <Alert
              type="warning"
              showIcon
              data-testid="task-runtime-locked"
              message={t("task.settings.locked")}
              description={t("task.settings.lockedDescription")}
            />
          )}
        </Space>
      </section>

      {scheduleVisible && (
        <section className="task-schedule-config">
          <Typography.Title level={5}>{t("task.settings.scheduleTitle")}</Typography.Title>
          {loadingSchedule ? <Spin /> : (
            <Space direction="vertical" size="middle" className="schedule-form">
              <label className="settings-field">
                <span className="settings-field-label">{t("task.settings.cron")}</span>
                <Input data-testid="task-schedule-cron" value={cron} disabled={readOnly || scheduleFieldsLocked} onChange={(event) => { setCron(event.target.value); setScheduleTouched(true); }} />
              </label>
              <label className="settings-field">
                <span className="settings-field-label">{t("task.settings.timezone")}</span>
                <Input data-testid="task-schedule-timezone" value={timezone} disabled={readOnly || scheduleFieldsLocked} onChange={(event) => { setTimezone(event.target.value); setScheduleTouched(true); }} />
              </label>
              <div className="settings-field">
                <span className="settings-field-label">{t("task.settings.scheduleStatus")}</span>
                <Space>
                  <Tag color={scheduleEnabled ? "green" : "default"}>{scheduleEnabled ? t("task.settings.scheduleRunning") : t("task.settings.disabled")}</Tag>
                  <span data-testid="task-schedule-next-run">{t("task.settings.nextRun", { time: formatTime(schedule?.next_run_at ?? null, locale) })}</span>
                  <Button size="small" onClick={() => void loadSchedule()}>{t("actions.refresh", { ns: "common" })}</Button>
                </Space>
              </div>
            </Space>
          )}
        </section>
      )}

      <section className="task-input-config" data-testid="task-input-config">
        <div className="task-input-heading">
          <div>
            <Typography.Title level={5}>{t("input.objectTitle", { ns: "adapter" })}</Typography.Title>
            <Typography.Text type="secondary">{t("task.input.description")}</Typography.Text>
          </div>
          <div className="task-input-state" data-testid="task-input-state" aria-live="polite">
            <Tag color={inputDirty ? "gold" : "green"}>
              {inputDirty ? t("task.input.draft") : t("task.input.saved")}
            </Tag>
            <span data-testid="task-input-revision">
              {inputConfig === null ? t("task.input.revisionUnknown") : t("task.input.revision", { value: inputConfig.revision })}
            </span>
          </div>
        </div>

        <div className="task-input-source-grid" role="radiogroup" aria-label={t("task.input.sourceGroup")}>
          {sourceCards.map((card) => {
            const selected = inputSourceDraft === card.sourceType;
            const cardContent = (
              <Card
                key={card.sourceType}
                size="small"
                className={`task-input-source-card${selected ? " is-selected" : ""}${card.disabledReason !== null ? " is-disabled" : ""}`}
                data-testid={`task-input-source-${card.sourceType}`}
                role="radio"
                aria-checked={selected}
                aria-disabled={card.disabledReason !== null}
                aria-label={`${card.title}: ${card.status}`}
                tabIndex={0}
                hoverable={card.disabledReason === null}
                onClick={() => selectInputSource(card.sourceType, card.disabledReason)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    selectInputSource(card.sourceType, card.disabledReason);
                  }
                }}
              >
                <div className="task-input-source-card-title">{card.title}</div>
                <Typography.Text type="secondary">{card.description}</Typography.Text>
                <Tag color={selected ? "blue" : card.disabledReason === null ? "default" : "gold"}>
                  {card.status}
                </Tag>
              </Card>
            );
            return card.disabledReason === null ? cardContent : (
              <Tooltip key={card.sourceType} title={card.disabledReason} trigger={["hover", "focus"]}>
                {cardContent}
              </Tooltip>
            );
          })}
        </div>

        <Form layout="vertical" className="task-input-form">
          {inputSourceDraft === "json" && (
            <Form.Item
              label={t("task.input.jsonLabel")}
              validateStatus={inputValidationError !== null ? "error" : undefined}
              help={inputValidationError ?? t("task.input.jsonHint")}
            >
              <Input.TextArea
                data-testid="task-input-json"
                aria-label={t("task.input.jsonLabel")}
                aria-invalid={inputValidationError !== null}
                rows={8}
                value={inputJsonDraft}
                disabled={inputEditingLocked}
                placeholder={t("task.input.jsonPlaceholder")}
                onChange={(event) => {
                  setInputJsonDraft(event.target.value);
                  setInputValidationError(null);
                }}
              />
            </Form.Item>
          )}
          {inputConfig !== null && !inputConfig.valid_for_run && inputSourceDraft === inputConfig.source_type && !inputDirty && (
            <Alert
              type="warning"
              showIcon
              data-testid="task-input-invalid"
              message={inputInvalidMessage ?? t("task.input.invalidConfig")}
            />
          )}
          <div className="task-input-actions">
            <Button
              type="primary"
              htmlType="button"
              data-testid="save-task-input"
              loading={savingInput}
              disabled={inputEditingLocked || inputConfig === null}
              onClick={() => void saveInputObject()}
            >
              {t("task.input.save")}
            </Button>
            {inputDirty && <Typography.Text type="warning">{t("task.input.unsavedHint")}</Typography.Text>}
          </div>
        </Form>
      </section>

      <Button
        type="primary"
        className="task-run-config-save"
        data-testid="save-task-runtime"
        loading={savingRuntime}
        disabled={readOnly || runtimeLocked || workerId === null}
        onClick={() => void saveRunConfig()}
      >
        {t("task.settings.save")}
      </Button>
    </div>
  );
});

export default TaskRunSettingsPanel;
