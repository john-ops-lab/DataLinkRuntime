/** Workbench Header：Adapter 上下文 + 版本切换 + 主操作（M3.1 §9）。 */

import { Button, Dropdown, Tag } from "antd";

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
  onPublish: () => void;
  onOpenSettings: () => void;
}

export default function WorkbenchHeader(props: WorkbenchHeaderProps) {
  const { adapter, versions, selectedVersion } = props;

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
          {props.dirty && (
            <Tag color="warning" data-testid="dirty-indicator">
              未保存修改
            </Tag>
          )}
        </div>
        {adapter.description && <p className="workbench-description">{adapter.description}</p>}
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
          disabled={props.selectedVersionId === null || props.busy || !props.contentReady}
          onClick={props.onPublish}
        >
          发布
        </Button>
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>
          设置
        </Button>
        <Button
          type="primary"
          data-testid="save-version"
          disabled={props.busy || !props.contentReady}
          onClick={props.onSave}
        >
          保存新版本
        </Button>
      </div>
    </header>
  );
}
