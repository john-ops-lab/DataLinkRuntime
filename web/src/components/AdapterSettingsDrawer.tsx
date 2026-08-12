/** Adapter 设置 Drawer：低频的元数据编辑与删除退出主工作区（M3.1 §9.4）。 */

import { Button, Divider, Drawer, Input } from "antd";

import type { Adapter } from "../types";

interface AdapterSettingsDrawerProps {
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
}

export default function AdapterSettingsDrawer(props: AdapterSettingsDrawerProps) {
  return (
    <Drawer
      title="Adapter 设置"
      width={400}
      open={props.open}
      destroyOnClose
      onClose={props.onClose}
    >
      {props.adapter !== null && (
        <div className="settings-form">
          <label className="settings-field">
            <span className="settings-field-label">名称</span>
            <Input
              data-testid="adapter-name"
              value={props.name}
              disabled={props.busy}
              onChange={(event) => props.onNameChange(event.target.value)}
            />
          </label>
          <label className="settings-field">
            <span className="settings-field-label">描述</span>
            <Input
              data-testid="adapter-description"
              placeholder="描述"
              value={props.description}
              disabled={props.busy}
              onChange={(event) => props.onDescriptionChange(event.target.value)}
            />
          </label>
          <Button
            type="primary"
            data-testid="update-details"
            disabled={props.busy || !props.contentReady}
            onClick={props.onUpdate}
          >
            更新信息
          </Button>

          <Divider />

          <p className="settings-danger-hint">
            删除将移除该 Adapter 及其全部版本，操作不可恢复。
          </p>
          <Button danger data-testid="delete-adapter" disabled={props.busy} onClick={props.onDelete}>
            删除 Adapter
          </Button>
        </div>
      )}
    </Drawer>
  );
}
