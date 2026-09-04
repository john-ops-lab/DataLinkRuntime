import {
  Alert,
  Badge,
  Button,
  Card,
  Descriptions,
  Space,
  Spin,
  Tag,
  Typography,
  type DescriptionsProps,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import {
  systemStatusBadgeStatus,
  type ControlHealthPayload,
  type HealthStatus,
  type SystemStatusLevel,
} from "../system-status";
import type { Worker } from "../types";
import WorkerStatus from "./WorkerStatus";

interface SystemStatusPanelProps {
  level: SystemStatusLevel;
  health: HealthStatus;
  healthPayload: ControlHealthPayload | null;
  healthCheckedAt: string | null;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  refreshing: boolean;
  onRefresh: () => Promise<void>;
}

function formatTime(value: string, locale: "zh-CN" | "en"): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale === "en" ? "en-US" : "zh-CN");
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = "B";
  for (const nextUnit of units) {
    amount /= 1024;
    unit = nextUnit;
    if (amount < 1024 || nextUnit === units[units.length - 1]) {
      break;
    }
  }
  return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${unit}`;
}

function formatDuration(value: number | null | undefined, suffix: string): string {
  return value === null || value === undefined ? "—" : `${value} ${suffix}`;
}

function statusColor(status: string | undefined): "green" | "gold" | "red" | "default" {
  if (status === "ok" || status === "ready" || status === "passed") {
    return "green";
  }
  if (status === "degraded" || status === "waiting_for_worker") {
    return "gold";
  }
  if (status === "error" || status === "failed" || status === "unavailable") {
    return "red";
  }
  return "default";
}

export default function SystemStatusPanel(props: SystemStatusPanelProps) {
  const { i18n, t } = useTranslation(["settings", "common"]);
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const rabbitmq = props.healthPayload?.rabbitmq;
  const broker = rabbitmq?.broker;
  const outbox = props.healthPayload?.outbox;

  function stateTag(value: string | undefined) {
    const state = value ?? "unknown";
    const knownStates: Record<string, string> = {
      ok: t("systemStatus.states.ok"),
      ready: t("systemStatus.states.ready"),
      degraded: t("systemStatus.states.degraded"),
      disabled: t("systemStatus.states.disabled"),
      waiting_for_worker: t("systemStatus.states.waitingForWorker"),
      unavailable: t("systemStatus.states.unavailable"),
      failed: t("systemStatus.states.failed"),
      unknown: t("systemStatus.states.unknown"),
    };
    return <Tag color={statusColor(value)}>{knownStates[state] ?? state}</Tag>;
  }

  function booleanTag(value: boolean | undefined) {
    if (value === undefined) {
      return <Tag>{t("systemStatus.states.unknown")}</Tag>;
    }
    return value
      ? <Tag color="green">{t("systemStatus.states.normal")}</Tag>
      : <Tag color="red">{t("systemStatus.states.abnormal")}</Tag>;
  }

  function errorCode(value: string | null | undefined) {
    return value ? <Typography.Text code>{value}</Typography.Text> : "—";
  }

  const controlItems: DescriptionsProps["items"] = props.healthPayload === null
    ? []
    : [
        {
          key: "service",
          label: t("systemStatus.service"),
          children: props.healthPayload.service ?? "dlr-control",
        },
        {
          key: "status",
          label: t("systemStatus.controlStatus"),
          children: stateTag(props.healthPayload.status),
        },
        {
          key: "database",
          label: t("systemStatus.database"),
          children: booleanTag(props.healthPayload.database),
        },
        {
          key: "checkedAt",
          label: t("systemStatus.checkedAt"),
          children: props.healthCheckedAt === null
            ? t("systemStatus.notChecked")
            : formatTime(props.healthCheckedAt, locale),
        },
      ];

  const runtimeItems: DescriptionsProps["items"] = props.healthPayload === null
    ? []
    : [
        {
          key: "rabbitmq",
          label: t("systemStatus.rabbitmq"),
          children: stateTag(rabbitmq?.status),
        },
        {
          key: "ingress",
          label: t("systemStatus.ingress"),
          children: stateTag(rabbitmq?.ingress?.status),
        },
        {
          key: "repair",
          label: t("systemStatus.repair"),
          children: stateTag(rabbitmq?.repair?.status),
        },
        {
          key: "rabbitError",
          label: t("systemStatus.lastErrorCode"),
          children: errorCode(
            rabbitmq?.last_error_code
              ?? rabbitmq?.ingress?.last_error_code
              ?? rabbitmq?.repair?.last_error_code,
          ),
        },
        {
          key: "queueMessages",
          label: t("systemStatus.queueMessageHeadroom"),
          children: t("systemStatus.headroomValue", {
            available: broker?.headroom_messages ?? "—",
            limit: broker?.queue_max_length ?? "—",
          }),
        },
        {
          key: "queueBytes",
          label: t("systemStatus.queueByteHeadroom"),
          children: t("systemStatus.headroomValue", {
            available: formatBytes(broker?.headroom_bytes),
            limit: formatBytes(broker?.queue_max_bytes),
          }),
        },
        {
          key: "brokerAlerts",
          label: t("systemStatus.brokerAlerts"),
          children: broker?.alerts?.length ?? 0,
        },
        {
          key: "outbox",
          label: t("systemStatus.outbox"),
          children: stateTag(outbox?.status),
        },
        {
          key: "pendingCount",
          label: t("systemStatus.outboxPendingCount"),
          children: outbox?.pending_count ?? "—",
        },
        {
          key: "pendingBytes",
          label: t("systemStatus.outboxPendingBytes"),
          children: formatBytes(outbox?.pending_bytes),
        },
        {
          key: "oldestAge",
          label: t("systemStatus.outboxOldestAge"),
          children: formatDuration(
            outbox?.oldest_age_seconds ?? outbox?.pending_oldest_age_seconds,
            t("systemStatus.seconds"),
          ),
        },
        {
          key: "outboxError",
          label: t("systemStatus.outboxErrorCode"),
          children: errorCode(outbox?.error_code),
        },
      ];

  return (
    <div className="system-status-panel" data-testid="system-status-panel">
      <div className={`system-status-overview system-status-overview-${props.level}`}>
        <div className="system-status-overview-copy" aria-live="polite">
          <Badge
            status={systemStatusBadgeStatus(props.level)}
            text={<strong>{t(`systemStatus.summary.${props.level}`, { ns: "common" })}</strong>}
          />
          <Typography.Text type="secondary">{t("systemStatus.snapshotHint")}</Typography.Text>
        </div>
        <Button
          icon={<ReloadOutlined aria-hidden="true" />}
          loading={props.refreshing}
          disabled={props.refreshing}
          data-testid="system-status-refresh"
          onClick={() => void props.onRefresh()}
        >
          {t("systemStatus.refresh")}
        </Button>
      </div>

      <div className="system-status-grid">
        <Card
          size="small"
          className="system-status-card"
          title={t("systemStatus.controlTitle")}
          extra={<Badge status={systemStatusBadgeStatus(
            props.health === "ok"
              ? "normal"
              : props.health === "degraded"
                ? "warning"
                : props.health === "unreachable"
                  ? "error"
                  : "checking",
          )} />}
        >
          {props.health === "loading" ? (
            <Space role="status" aria-label={t("systemStatus.loading")}>
              <Spin size="small" />
              <Typography.Text type="secondary">{t("systemStatus.loading")}</Typography.Text>
            </Space>
          ) : props.healthPayload === null ? (
            <Alert type="error" showIcon message={t("systemStatus.controlUnavailable")} />
          ) : (
            <Descriptions size="small" column={1} items={controlItems} />
          )}
        </Card>

        <Card size="small" className="system-status-card" title={t("systemStatus.runtimeTitle")}>
          {props.health === "loading" ? (
            <Space role="status" aria-label={t("systemStatus.loading")}>
              <Spin size="small" />
              <Typography.Text type="secondary">{t("systemStatus.loading")}</Typography.Text>
            </Space>
          ) : props.healthPayload === null ? (
            <Typography.Text type="secondary">{t("systemStatus.noRuntimeFacts")}</Typography.Text>
          ) : (
            <Descriptions size="small" column={1} items={runtimeItems} />
          )}
        </Card>

        <Card size="small" className="system-status-card system-status-workers" title={t("systemStatus.workersTitle")}>
          <WorkerStatus
            workers={props.workers}
            loading={props.workersLoading}
            error={props.workersError}
          />
        </Card>
      </div>
    </div>
  );
}
