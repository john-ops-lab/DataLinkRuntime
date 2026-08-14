/** Webhook Adapter header without Publish/Production/Test lifecycle concepts. */

import { Alert, Button, Tag } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, VersionSummary } from "../types";
import ActionWithReason from "./ActionWithReason";

interface Props {
  adapter: Adapter;
  selectedVersion: VersionSummary | null;
  dirty: boolean;
  busy: boolean;
  contentReady: boolean;
  onSave: () => void;
  onOpenSettings: () => void;
}

export default function WebhookWorkbenchHeader(props: Props) {
  const archived = !!props.adapter.archived_at;
  const locked = props.adapter.runtime_locked === true;
  const reason = archived
    ? "Adapter 已删除，不能继续编辑"
    : locked
      ? "正在接收或存在运行中的调用，请先停止接收并等待当前调用完成"
      : !props.contentReady
        ? "版本内容尚未就绪"
        : props.busy
          ? "其他操作正在进行"
          : null;
  return (
    <header className="workbench-header" data-testid="workbench-header">
      <div className="workbench-context" data-testid="webhook-workbench-header">
        <div className="workbench-title-row">
          <h2 className="workbench-title" title={props.adapter.name}>{props.adapter.name}</h2>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="workbench-context-fact">Webhook Adapter</span>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="workbench-context-fact">{LANGUAGE_LABELS[props.adapter.language]}</span>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="version-seq">{props.selectedVersion === null ? "未保存 Revision" : `Revision ${props.selectedVersion.seq}`}</span>
          {props.dirty && <Tag color="warning" data-testid="dirty-indicator">未保存修改</Tag>}
          {locked && <Tag color="orange">运行配置已锁定</Tag>}
        </div>
        {props.adapter.running_execution_id != null && (
          <Alert type="info" showIcon message={`调用 #${props.adapter.running_execution_id} 正在运行`} description="停止接收不会终止当前调用；调用终态后运行配置自动解锁。" />
        )}
      </div>
      <div className="workbench-controls">
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>设置</Button>
        <ActionWithReason label="保存 Revision" reason={reason}>
          <Button type="primary" data-testid="save-version" disabled={reason !== null} onClick={props.onSave}>保存 Revision</Button>
        </ActionWithReason>
      </div>
    </header>
  );
}
