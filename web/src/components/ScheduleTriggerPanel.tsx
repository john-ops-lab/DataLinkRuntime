/** 触发器 Tab：M5.2 只实现 Schedule 区域（Webhook 留给 M5.3，不做假入口）。 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Input, Space, Spin, Switch, Typography } from "antd";

import { ApiError, api } from "../api";
import type { AdapterSchedule } from "../types";

const DEFAULT_CRON = "0 */2 * * *";

/** 浏览器当前 IANA 时区；异常环境退回 UTC。 */
function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function formatTime(value: string | null): string {
  if (value === null) {
    return "—";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** 后端校验错误 code → 稳定的中文说明。 */
function validationMessage(error: ApiError): string {
  if (error.code === "schedule_invalid_cron") {
    return "Cron 表达式无效：必须是 5 字段表达式（分 时 日 月 周），例如 0 */2 * * *";
  }
  if (error.code === "schedule_invalid_timezone") {
    return "时区无效：必须是 IANA 时区名称，例如 Asia/Shanghai 或 UTC";
  }
  if (error.code === "execution_input_too_large") {
    return "Input 超出大字段上限，保存未生效";
  }
  return `${error.message} (${error.code})`;
}

interface ScheduleTriggerPanelProps {
  adapterId: number;
  productionState: "idle" | "running" | "stopped";
  /** 已归档 Adapter 只读：可查看配置但禁用编辑与保存（与服务端 409 adapter_archived 对齐）。 */
  archived: boolean;
}

export default function ScheduleTriggerPanel(props: ScheduleTriggerPanelProps) {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [enabled, setEnabled] = useState(false);
  const [cron, setCron] = useState(DEFAULT_CRON);
  const [timezone, setTimezone] = useState(browserTimezone);
  const [inputText, setInputText] = useState("null");
  const [saved, setSaved] = useState<AdapterSchedule | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const schedule = await api.getSchedule(props.adapterId);
      setEnabled(schedule.enabled);
      setCron(schedule.cron);
      setTimezone(schedule.timezone);
      setInputText(JSON.stringify(schedule.input, null, 2));
      setSaved(schedule);
    } catch (error) {
      // 未配置是合法初始状态：用默认值初始化表单，不当作错误展示。
      if (error instanceof ApiError && error.code === "schedule_not_configured") {
        setSaved(null);
        return;
      }
      setLoadError(error instanceof ApiError ? `${error.message} (${error.code})` : "请求失败");
    } finally {
      setLoading(false);
    }
  }, [props.adapterId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载时拉取 Schedule 配置的初始加载是有意的异步同步
    void load();
  }, [load]);

  async function handleSave() {
    if (saving) {
      return;
    }
    let input: unknown;
    if (inputText.trim() === "") {
      input = null;
    } else {
      try {
        input = JSON.parse(inputText);
      } catch {
        setSaveError("Input 必须是合法 JSON（留空表示 JSON null）");
        return;
      }
    }
    setSaving(true);
    setSaveError(null);
    setNotice(null);
    try {
      const stored = await api.putSchedule(props.adapterId, {
        enabled,
        cron,
        timezone,
        input,
      });
      setEnabled(stored.enabled);
      setCron(stored.cron);
      setTimezone(stored.timezone);
      setInputText(JSON.stringify(stored.input, null, 2));
      setSaved(stored);
      setNotice("Schedule 已保存；下次执行时间已按新的未来计划点重基准。");
    } catch (error) {
      setSaveError(error instanceof ApiError ? validationMessage(error) : "请求失败");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="schedule-trigger-panel" data-testid="schedule-loading">
        <Spin />
      </div>
    );
  }

  return (
    <div className="schedule-trigger-panel">
      <Typography.Title level={5}>Schedule（定时触发）</Typography.Title>
      <Typography.Paragraph type="secondary">
        一个 Adapter 最多一个 Schedule：5 字段 Cron + IANA 时区。保存后从下一个未来计划点开始，
        不回放历史计划；生产入口关闭或到点条件不满足时跳过，恢复后最多补最近一次。
      </Typography.Paragraph>
      {loadError !== null && (
        <Alert type="error" showIcon message={loadError} data-testid="schedule-load-error" />
      )}

      {props.archived && (
        <Alert
          type="warning"
          showIcon
          message="Adapter 已归档，Schedule 为只读：可查看配置，但无法编辑或保存。"
          data-testid="schedule-archived-hint"
        />
      )}

      {enabled && props.productionState !== "running" && (
        <Alert
          type="info"
          showIcon
          message="Schedule 已配置；生产入口关闭期间不会执行。点击 Start 后从下一个未来计划点开始。"
          data-testid="schedule-production-closed-hint"
        />
      )}

      <Space direction="vertical" size="middle" className="schedule-form">
        <label className="settings-field">
          <span className="settings-field-label">启用</span>
          <Switch
            data-testid="schedule-enabled"
            checked={enabled}
            disabled={props.archived}
            onChange={(value) => {
              setEnabled(value);
              setNotice(null);
            }}
          />
        </label>
        <label className="settings-field">
          <span className="settings-field-label">Cron（5 字段：分 时 日 月 周）</span>
          <Input
            data-testid="schedule-cron"
            value={cron}
            placeholder="0 */2 * * *"
            disabled={props.archived}
            onChange={(event) => {
              setCron(event.target.value);
              setNotice(null);
            }}
          />
        </label>
        <label className="settings-field">
          <span className="settings-field-label">Timezone（IANA）</span>
          <Input
            data-testid="schedule-timezone"
            value={timezone}
            placeholder="Asia/Shanghai"
            disabled={props.archived}
            onChange={(event) => {
              setTimezone(event.target.value);
              setNotice(null);
            }}
          />
        </label>
        <label className="settings-field">
          <span className="settings-field-label">Input（JSON，留空为 null）</span>
          <Input.TextArea
            data-testid="schedule-input"
            rows={4}
            value={inputText}
            disabled={props.archived}
            onChange={(event) => {
              setInputText(event.target.value);
              setNotice(null);
            }}
          />
        </label>
        <div className="settings-field">
          <span className="settings-field-label">下次执行时间</span>
          <Space>
            <span data-testid="schedule-next-run">
              {enabled ? formatTime(saved?.next_run_at ?? null) : "已禁用，不计划执行"}
            </span>
            {/* next_run_at 会被调度器推进，手动刷新避免展示过期的游标。 */}
            <Button
              size="small"
              data-testid="schedule-refresh"
              onClick={() => void load()}
            >
              刷新
            </Button>
          </Space>
        </div>
        {saveError !== null && (
          <Alert type="error" showIcon message={saveError} data-testid="schedule-error" />
        )}
        {notice !== null && (
          <Alert type="success" showIcon message={notice} data-testid="schedule-notice" />
        )}
        <div>
          <Button
            type="primary"
            data-testid="schedule-save"
            loading={saving}
            disabled={props.archived}
            onClick={() => void handleSave()}
          >
            保存
          </Button>
        </div>
      </Space>
    </div>
  );
}
