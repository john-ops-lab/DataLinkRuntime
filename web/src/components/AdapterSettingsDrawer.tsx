/** Low-frequency Adapter metadata, clone and soft-delete actions. */

import { Alert, Button, Divider, Drawer, Input, Space } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter } from "../types";
import ActionWithReason from "./ActionWithReason";

interface Props {
  open: boolean;
  adapter: Adapter | null;
  name: string;
  description: string;
  busy: boolean;
  contentReady: boolean;
  onClose: () => void;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onUpdate: () => void;
  onDelete: () => void;
  onClone: () => void;
}

export default function AdapterSettingsDrawer(props: Props) {
  const adapter = props.adapter;
  const archived = !!adapter?.archived_at;
  return (
    <Drawer
      title={`${adapter?.adapter_type === "webhook" ? "Webhook" : "任务"}适配器设置`}
      width={400}
      open={props.open}
      destroyOnHidden
      onClose={props.onClose}
    >
      {adapter !== null && (
        <div className="settings-form">
          <label className="settings-field">
            <span className="settings-field-label">名称</span>
            <Input data-testid="adapter-name" value={props.name} disabled={props.busy || archived} onChange={(event) => props.onNameChange(event.target.value)} />
          </label>
          <label className="settings-field">
            <span className="settings-field-label">开发语言</span>
            <Input data-testid="adapter-language" value={LANGUAGE_LABELS[adapter.language]} disabled />
          </label>
          <label className="settings-field">
            <span className="settings-field-label">描述</span>
            <Input data-testid="adapter-description" value={props.description} disabled={props.busy || archived} onChange={(event) => props.onDescriptionChange(event.target.value)} />
          </label>
          <Button type="primary" data-testid="update-details" disabled={props.busy || !props.contentReady || archived} onClick={props.onUpdate}>更新信息</Button>
          <Divider />
          {archived ? (
            <Alert
              type="info"
              showIcon
              data-testid="archived-settings-readonly"
              message="已删除适配器仅支持查看"
              description="该适配器已从活跃列表移除。"
            />
          ) : (
            <Space direction="vertical" className="settings-lifecycle-actions">
              <Button data-testid="clone-adapter" disabled={props.busy} onClick={props.onClone}>复制适配器</Button>
              <ActionWithReason
                label="删除适配器"
                reason={adapter.runtime_locked === true ? "请先停止适配器，再删除" : props.busy ? "其他操作正在进行" : null}
              >
                <Button danger data-testid="delete-adapter" disabled={props.busy || adapter.runtime_locked === true} onClick={props.onDelete}>删除适配器</Button>
              </ActionWithReason>
            </Space>
          )}
          {adapter.runtime_locked === true && (
            <Alert type="warning" showIcon message={adapter.adapter_type === "webhook" ? "接收中或调用活跃期间不能删除适配器。" : "定时启用或执行活跃期间不能删除适配器。"} />
          )}
          {!archived && <p className="settings-danger-hint">删除后适配器会从活跃列表消失；运行历史按平台保留策略处理。</p>}
        </div>
      )}
    </Drawer>
  );
}
