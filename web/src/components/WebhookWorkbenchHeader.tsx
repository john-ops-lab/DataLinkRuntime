/** Webhook Adapter header: identity, receive state and explicit actions. */

import { Alert, Button, Tag } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, Worker } from "../types";
import ActionWithReason from "./ActionWithReason";
import type { WebhookRuntimeState } from "./WebhookTriggerPanel";

interface Props {
  adapter: Adapter;
  runtimeWorker: Worker | null;
  runtimeState: WebhookRuntimeState;
  dirty: boolean;
  busy: boolean;
  contentReady: boolean;
  onSave: () => void;
  onOpenSettings: () => void;
  onClone: () => void;
  onToggleReceiving: () => void;
}

export default function WebhookWorkbenchHeader(props: Props) {
  const archived = !!props.adapter.archived_at;
  const locked = props.adapter.runtime_locked === true || props.runtimeState.enabled;
  const saveReason = archived
    ? "Adapter 已删除，不能继续编辑"
    : locked
      ? "正在接收或存在运行中的调用，请先停止接收并等待当前调用完成"
      : !props.contentReady
        ? "内容尚未就绪"
        : props.busy
          ? "其他操作正在进行"
          : null;
  const receiveReason = props.runtimeState.enabled
    ? null
    : !props.runtimeState.loaded
      ? "Webhook 运行设置正在加载"
      : props.runtimeState.startBlockedReason;

  return (
    <header className="workbench-header" data-testid="workbench-header">
      <div className="workbench-context" data-testid="webhook-workbench-header">
        <div className="workbench-title-row">
          <h2 className="workbench-title" title={props.adapter.name}>{props.adapter.name}</h2>
          <Tag color="cyan">Webhook</Tag>
          <span className="workbench-context-fact">{LANGUAGE_LABELS[props.adapter.language]}</span>
          <Tag color={props.runtimeState.enabled ? "processing" : "default"}>
            {props.runtimeState.enabled ? "接收中" : props.adapter.running_execution_id != null ? "调用中" : "已停止"}
          </Tag>
          <span className="workbench-context-fact" data-testid="header-runtime-worker">
            运行节点：{props.runtimeWorker?.name ?? "未选择"}
          </span>
          {props.dirty && <Tag color="warning" data-testid="dirty-indicator">未保存修改</Tag>}
        </div>
        {locked && (
          <Alert
            type="warning"
            showIcon
            message="Adapter 正在运行，编辑与运行配置已锁定"
            description="Adapter 正在运行。运行期间不能修改代码或运行配置。如需升级，请复制为新的 Adapter，完成修改和测试后停止当前 Adapter，再启动新 Adapter。"
            action={<Button size="small" data-testid="header-clone-adapter" onClick={props.onClone}>复制 Adapter</Button>}
          />
        )}
      </div>
      <div className="workbench-controls">
        <Button data-testid="adapter-settings" onClick={props.onOpenSettings}>设置</Button>
        <ActionWithReason label="保存" reason={saveReason}>
          <Button type="primary" data-testid="save-version" disabled={saveReason !== null} onClick={props.onSave}>保存</Button>
        </ActionWithReason>
        <ActionWithReason label={props.runtimeState.enabled ? "停止接收" : "开启接收"} reason={receiveReason}>
          <Button
            danger={props.runtimeState.enabled}
            data-testid="header-webhook-toggle"
            loading={props.runtimeState.changingState}
            disabled={!props.runtimeState.enabled && receiveReason !== null}
            onClick={props.onToggleReceiving}
          >
            {props.runtimeState.enabled ? "停止接收" : "开启接收"}
          </Button>
        </ActionWithReason>
      </div>
    </header>
  );
}
