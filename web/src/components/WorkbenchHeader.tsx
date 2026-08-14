/** Workbench Header：Adapter 上下文 + 版本切换 + 生产生命周期主操作（M3.2 §9）。 */

import { useState } from "react";
import { Alert, Button, Dropdown, Modal, Space, Tag } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import {
  hasLastProductionExecutionFailure,
  productionDisplayState,
  productionRunningVersionId,
  productionStateColor,
  productionStateLabel,
} from "../status";
import type { Adapter, VersionSummary, Worker } from "../types";
import ActionWithReason from "./ActionWithReason";

interface WorkbenchHeaderProps {
  adapter: Adapter;
  versions: VersionSummary[];
  selectedVersionId: number | null;
  selectedVersion: VersionSummary | null;
  isLatest: boolean;
  isPublished: boolean;
  dirty: boolean;
  busy: boolean;
  contentReady: boolean;
  productionWorker: Worker | null;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  productionWorkerRetestRequired: boolean;
  onSelectVersion: (versionId: number) => void;
  onSave: () => void;
  /** 发布走确认框（门禁信息 + Diff 入口），由 App 打开。 */
  onPublishRequest: () => void;
  onStartProduction: () => void;
  onStopProduction: (mode: "wait" | "terminate") => void;
  onOpenSettings: () => void;
}

function versionSeqOrId(
  versions: VersionSummary[],
  versionId: number | null | undefined,
): string | null {
  if (versionId === null || versionId === undefined) {
    return null;
  }
  const version = versions.find((candidate) => candidate.id === versionId);
  return version !== undefined ? `v${version.seq}` : `#${versionId}`;
}

export default function WorkbenchHeader(props: WorkbenchHeaderProps) {
  const { adapter, versions, selectedVersion } = props;
  const [stopModalOpen, setStopModalOpen] = useState(false);

  const archived = !!adapter.archived_at;
  const productionState = adapter.production_state ?? "idle";
  const runningExecutionId = adapter.running_execution_id ?? null;
  const displayState = productionDisplayState(adapter);
  const stopping = displayState === "stopping";
  const runningIdle =
    productionState === "running" && runningExecutionId === null && displayState === "running";
  const runningVersionId = productionRunningVersionId(adapter);
  const contextVersion = selectedVersion ? `v${selectedVersion.seq}` : "未保存版本";

  // M5.1: Published != Production Version 提示：比较 published_version_id 与
  // production_version_id（而非 running_version_id）。
  const publishedProductionMismatch =
    adapter.published_version_id !== null &&
    adapter.published_version_id !== undefined &&
    adapter.production_version_id !== null &&
    adapter.production_version_id !== undefined &&
    adapter.published_version_id !== adapter.production_version_id;
  // M5.1: 最近一次生产执行失败只是 Execution 结果事实，不是生产入口的
  // 生命周期异常；入口保持开启，不提示用户再次 Stop → Start。
  const lastProductionFailure =
    productionState === "running" && hasLastProductionExecutionFailure(adapter);
  const lastFailureExecutionId = adapter.last_production_execution_id ?? null;
  const startRelevant =
    adapter.published_version_id !== null &&
    adapter.published_version_id !== undefined &&
    productionState !== "running" &&
    !archived &&
    !stopping;
  const productionWorkerStatusUnavailable =
    adapter.runtime_worker_id !== null &&
    adapter.runtime_worker_id !== undefined &&
    !props.workersLoading &&
    props.productionWorker === null;
  const productionWorkerOffline =
    props.productionWorker !== null && props.productionWorker.status !== "online";
  const compatibleOnlineWorkers = props.workers.filter(
    (worker) =>
      worker.status === "online" && worker.capabilities.includes(adapter.language),
  );
  const automaticWorkerUnavailable =
    (adapter.runtime_worker_id === null || adapter.runtime_worker_id === undefined) &&
    !props.workersLoading &&
    props.workersError === null &&
    compatibleOnlineWorkers.length !== 1;

  const saveBlockedReason = archived
    ? "Adapter 已归档，请先在设置中恢复"
    : !props.contentReady
      ? "版本内容尚未就绪，请等待加载完成或刷新后重试"
      : props.busy
        ? "其他操作正在进行，请等待完成"
        : null;
  const publishBlockedReason = archived
    ? "Adapter 已归档，请先在设置中恢复"
    : !props.contentReady
      ? "版本内容尚未就绪，请等待加载完成或刷新后重试"
      : props.selectedVersionId === null
        ? "当前没有已保存版本，请先保存为新版本"
        : props.busy
          ? "其他操作正在进行，请等待完成"
          : null;
  const startBlockedReason = archived
    ? "Adapter 已归档，请先在设置中恢复"
    : stopping
      ? `正在等待 Execution #${runningExecutionId ?? "—"} 完成，进入终态后才能重新启动`
      : props.busy
        ? "其他操作正在进行，请等待完成"
        : null;
  const stopBlockedReason = props.busy ? "其他操作正在进行，请等待完成" : null;

  const menuItems = versions.map((version) => ({
    key: String(version.id),
    label: (
      <span className="version-menu-item">
        v{version.seq}
        {version.id === adapter.latest_version_id && <Tag color="blue">Latest</Tag>}
        {version.id === adapter.published_version_id && <Tag color="green">Published</Tag>}
      </span>
    ),
  }));

  function handleStopMode(mode: "wait" | "terminate") {
    setStopModalOpen(false);
    props.onStopProduction(mode);
  }

  return (
    <header className="workbench-header" data-testid="workbench-header">
      <div className="workbench-context">
        <div className="workbench-title-row">
          <h2 className="workbench-title" title={adapter.name}>{adapter.name}</h2>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="workbench-context-fact">{LANGUAGE_LABELS[adapter.language]}</span>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="version-seq" data-testid="version-seq">{contextVersion}</span>
          {props.dirty && (
            <Tag color="warning" data-testid="dirty-indicator">
              未保存修改
            </Tag>
          )}
        </div>
        <div className="workbench-production-summary">
          <Tag color={productionStateColor(displayState)} data-testid="production-state">
            生产：{productionStateLabel(displayState)}
          </Tag>
          <span>
            发布目标：{versionSeqOrId(versions, adapter.published_version_id) ?? "未发布"}
          </span>
          <span>
            运行版本：{versionSeqOrId(versions, runningVersionId) ?? "无"}
          </span>
          {runningIdle && <span data-testid="production-execution-idle">执行：空闲</span>}
          {runningExecutionId !== null && (
            <span data-testid="running-execution">
              {stopping ? "等待完成" : "执行中"} #{runningExecutionId}
            </span>
          )}
          {props.isLatest && <span data-testid="latest-badge">当前为 Latest</span>}
          {props.isPublished && <span data-testid="published-badge">当前为 Published</span>}
        </div>
        {startRelevant && (productionWorkerStatusUnavailable || productionWorkerOffline) && (
          <Alert
            type="warning"
            showIcon
            data-testid="header-production-worker-warning"
            message={
              productionWorkerOffline
                ? `production Worker ${props.productionWorker?.name ?? ""} 最近状态为离线`
                : `暂时无法取得 production Worker #${adapter.runtime_worker_id ?? "—"} 的状态`
            }
            description={
              props.workersError !== null
                ? "Worker 列表加载失败；可刷新页面重试。Start 时后端仍会做最终在线判定。"
                : "可在设置中检查或更换 Worker；Start 时后端会按最新心跳再次判定。"
            }
          />
        )}
        {startRelevant && automaticWorkerUnavailable && (
          <Alert
            type="warning"
            showIcon
            data-testid="header-production-worker-selection-warning"
            message={
              compatibleOnlineWorkers.length === 0
                ? `当前没有有效在线且支持 ${LANGUAGE_LABELS[adapter.language]} 的 Worker`
                : `当前有 ${compatibleOnlineWorkers.length} 个可用 Worker，无法自动确定 production Worker`
            }
            description={
              compatibleOnlineWorkers.length === 0
                ? "请先恢复、启动或注册一个兼容 Worker；Start 时后端仍会按最新心跳最终复核。"
                : "请在 Adapter 设置中指定 production Worker；Start 时后端仍会按最新心跳最终复核。"
            }
          />
        )}
        {startRelevant && props.productionWorkerRetestRequired && (
          <Alert
            type="warning"
            showIcon
            data-testid="header-production-worker-retest"
            message="production Worker 已切换，建议先重新测试"
            description="请到“测试运行”用当前已发布版本完成测试。Start 时后端会按权威测试记录复核。"
          />
        )}
        {publishedProductionMismatch && (
          <Alert
            type="warning"
            showIcon
            data-testid="published-running-mismatch"
            message={`已发布版本（${versionSeqOrId(versions, adapter.published_version_id)}）与生产锁定版本（${versionSeqOrId(versions, adapter.production_version_id)}）不一致`}
            description="发布只更新生产目标，不会自动切换生产版本；请人工 Stop 后再 Start 以锁定新版本。"
          />
        )}
        {stopping && runningExecutionId !== null && (
          <Alert
            type="info"
            showIcon
            data-testid="production-stopping"
            message={`生产入口已关闭，等待 Execution #${runningExecutionId} 完成`}
            description="旧 Execution 进入终态并刷新后，才能启动新的生产执行。"
          />
        )}
        {lastProductionFailure && (
          <Alert
            type="warning"
            showIcon
            data-testid="production-last-failure"
            message={`最近一次生产执行${adapter.last_production_execution_status === "timeout" ? "超时" : "失败"}`}
            description={`请在执行记录中查看${lastFailureExecutionId === null ? "失败详情" : ` Execution #${lastFailureExecutionId}`}；这只是最近一次执行的结果，生产入口保持开启，无需 Stop → Start。`}
          />
        )}
        {archived && (
          <Alert
            type="info"
            showIcon
            data-testid="archived-notice"
            message="该 Adapter 已归档"
            description="保存、发布、测试与启动均已禁用；可在设置中恢复。"
          />
        )}
      </div>

      <div className="workbench-controls">
        <Dropdown
          menu={{
            items: menuItems,
            selectedKeys: props.selectedVersionId !== null ? [String(props.selectedVersionId)] : [],
            onClick: ({ key }) => props.onSelectVersion(Number(key)),
          }}
          trigger={["click"]}
          disabled={props.busy || versions.length === 0}
        >
          <Button data-testid="version-selector" disabled={props.busy || versions.length === 0}>
            {selectedVersion ? `v${selectedVersion.seq}` : "暂无版本"} ▾
          </Button>
        </Dropdown>
        <div className="workbench-lifecycle-actions">
          {!props.isPublished && (
            <ActionWithReason label="发布" reason={publishBlockedReason}>
              <Button
                data-testid="publish-version"
                disabled={publishBlockedReason !== null}
                onClick={props.onPublishRequest}
              >
                发布
              </Button>
            </ActionWithReason>
          )}
        {adapter.published_version_id !== null && productionState !== "running" && (
          <ActionWithReason label="启动生产" reason={startBlockedReason}>
            <Button
              data-testid="start-production"
              disabled={startBlockedReason !== null}
              onClick={props.onStartProduction}
            >
              启动生产
            </Button>
          </ActionWithReason>
        )}
        {productionState === "running" && (
          <ActionWithReason label="停止生产" reason={stopBlockedReason}>
            <Button
              danger
              data-testid="stop-production"
              disabled={stopBlockedReason !== null}
              onClick={() => setStopModalOpen(true)}
            >
              停止
            </Button>
          </ActionWithReason>
        )}
        </div>
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>
          设置
        </Button>
        <ActionWithReason label="保存新版本" reason={saveBlockedReason}>
          <Button
            type="primary"
            data-testid="save-version"
            disabled={saveBlockedReason !== null}
            onClick={props.onSave}
          >
            保存新版本
          </Button>
        </ActionWithReason>
      </div>

      <Modal
        title="停止生产"
        open={stopModalOpen}
        onCancel={() => setStopModalOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setStopModalOpen(false)}>取消</Button>
            <Button
              data-testid="stop-mode-wait"
              disabled={props.busy}
              onClick={() => handleStopMode("wait")}
            >
              等待完成
            </Button>
            <Button
              danger
              data-testid="stop-mode-terminate"
              disabled={props.busy}
              onClick={() => handleStopMode("terminate")}
            >
              立即终止
            </Button>
          </Space>
        }
      >
        <p>
          <strong>等待完成</strong>：关闭生产入口，正在运行的 Execution 自然结束后收尾。
        </p>
        <p>
          <strong>立即终止</strong>：关闭生产入口并取消活跃的 Execution（运行中的进程将被终止）。
        </p>
      </Modal>
    </header>
  );
}
