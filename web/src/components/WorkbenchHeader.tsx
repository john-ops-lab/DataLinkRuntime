/** Workbench Header：Adapter 上下文 + 版本切换 + 生产生命周期主操作（M3.2 §9）。 */

import { useState } from "react";
import { Alert, Button, Dropdown, Modal, Space, Tag } from "antd";

import {
  productionDisplayState,
  productionStateColor,
  productionStateLabel,
} from "../status";
import type { Adapter, VersionSummary } from "../types";

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

  // Published != Running 显著提示：只有两个指针都存在且不一致时才显示。
  const publishedRunningMismatch =
    adapter.published_version_id !== null &&
    adapter.published_version_id !== undefined &&
    adapter.running_version_id !== null &&
    adapter.running_version_id !== undefined &&
    adapter.published_version_id !== adapter.running_version_id;

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
          <h2 className="workbench-title">{adapter.name}</h2>
          {selectedVersion && (
            <span className="version-seq" data-testid="version-seq">
              v{selectedVersion.seq}
            </span>
          )}
          {props.isLatest && (
            <Tag color="blue" data-testid="latest-badge">
              Latest
            </Tag>
          )}
          {props.isPublished && (
            <Tag color="green" data-testid="published-badge">
              Published
            </Tag>
          )}
          <Tag color={productionStateColor(displayState)} data-testid="production-state">
            生产：{productionStateLabel(displayState)}
          </Tag>
          {runningExecutionId !== null && (
            <Tag color="processing" data-testid="running-execution">
              执行进行中 #{runningExecutionId}
            </Tag>
          )}
          {props.dirty && (
            <Tag color="warning" data-testid="dirty-indicator">
              未保存修改
            </Tag>
          )}
        </div>
        {adapter.description && <p className="workbench-description">{adapter.description}</p>}
        {publishedRunningMismatch && (
          <Alert
            type="warning"
            showIcon
            data-testid="published-running-mismatch"
            message={`已发布版本（${versionSeqOrId(versions, adapter.published_version_id)}）与生产运行版本（${versionSeqOrId(versions, adapter.running_version_id)}）不一致`}
            description="发布新版本不会自动切换正在运行的生产执行；如需生效请先停止再启动。"
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
        <Button
          data-testid="publish-version"
          disabled={props.selectedVersionId === null || props.busy || !props.contentReady || archived}
          onClick={props.onPublishRequest}
        >
          发布
        </Button>
        {adapter.published_version_id !== null && productionState !== "running" && (
          <Button
            type="primary"
            data-testid="start-production"
            disabled={props.busy || archived}
            onClick={props.onStartProduction}
          >
            启动生产
          </Button>
        )}
        {productionState === "running" && (
          <Button
            danger
            data-testid="stop-production"
            disabled={props.busy}
            onClick={() => setStopModalOpen(true)}
          >
            停止
          </Button>
        )}
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>
          设置
        </Button>
        <Button
          type="primary"
          data-testid="save-version"
          disabled={props.busy || !props.contentReady || archived}
          onClick={props.onSave}
        >
          保存新版本
        </Button>
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
