/** Task Adapter header: Revision context plus Save/settings, without Publish/Production UX. */

import { Alert, Button, Tag } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, VersionSummary } from "../types";
import ActionWithReason from "./ActionWithReason";

interface TaskWorkbenchHeaderProps {
  adapter: Adapter;
  selectedVersion: VersionSummary | null;
  dirty: boolean;
  busy: boolean;
  contentReady: boolean;
  onSave: () => void;
  onOpenSettings: () => void;
}

export default function TaskWorkbenchHeader(props: TaskWorkbenchHeaderProps) {
  const archived = !!props.adapter.archived_at;
  const runtimeLocked = props.adapter.runtime_locked === true;
  const saveBlockedReason = archived
    ? "Adapter 已删除，不能继续编辑"
    : runtimeLocked
      ? "定时已启用或存在运行中的 Execution，请先停用定时并等待/停止当前运行"
      : !props.contentReady
        ? "版本内容尚未就绪，请等待加载完成或刷新后重试"
        : props.busy
          ? "其他操作正在进行，请等待完成"
          : null;

  return (
    <header className="workbench-header" data-testid="task-workbench-header">
      <div className="workbench-context">
        <div className="workbench-title-row">
          <h2 className="workbench-title" title={props.adapter.name}>{props.adapter.name}</h2>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="workbench-context-fact">任务型 Adapter</span>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="workbench-context-fact">{LANGUAGE_LABELS[props.adapter.language]}</span>
          <span className="workbench-context-separator" aria-hidden="true">·</span>
          <span className="version-seq" data-testid="task-revision">
            {props.selectedVersion === null ? "未保存 Revision" : `Revision ${props.selectedVersion.seq}`}
          </span>
          {props.dirty && <Tag color="warning">未保存修改</Tag>}
          {runtimeLocked && <Tag color="orange">运行配置已锁定</Tag>}
        </div>
        {props.adapter.running_execution_id != null && (
          <Alert
            type="info"
            showIcon
            data-testid="task-active-execution"
            message={`Execution #${props.adapter.running_execution_id} 正在运行`}
            description="代码、依赖、运行参数、凭据、运行节点与运行方式将在 Execution 进入终态后解锁。"
          />
        )}
      </div>
      <div className="workbench-controls">
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>设置</Button>
        <ActionWithReason label="保存 Revision" reason={saveBlockedReason}>
          <Button
            type="primary"
            data-testid="save-version"
            disabled={saveBlockedReason !== null}
            onClick={props.onSave}
          >
            保存 Revision
          </Button>
        </ActionWithReason>
      </div>
    </header>
  );
}
