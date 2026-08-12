/** Adapter 设置 Drawer：低频的元数据编辑与删除退出主工作区（M3.1 §9.4）。 */

import { Button, Divider, Drawer, Input, Space } from "antd";

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
  // --- M3.2 生产生命周期 ---------------------------------------------------
  onUnpublish: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onClone: () => void;
}

export default function AdapterSettingsDrawer(props: AdapterSettingsDrawerProps) {
  const adapter = props.adapter;
  const archived = adapter !== null && !!adapter.archived_at;
  const productionRunning = (adapter?.production_state ?? "idle") === "running";
  return (
    <Drawer
      title="Adapter 设置"
      width={400}
      open={props.open}
      destroyOnHidden
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
            disabled={props.busy || !props.contentReady || archived}
            onClick={props.onUpdate}
          >
            更新信息
          </Button>

          <Divider />

          <h4 className="settings-section-title">生产生命周期</h4>
          <Space direction="vertical" className="settings-lifecycle-actions">
            <Button
              data-testid="unpublish-adapter"
              disabled={
                props.busy ||
                adapter === null ||
                adapter.published_version_id === null ||
                productionRunning
              }
              onClick={props.onUnpublish}
            >
              取消发布
            </Button>
            {archived ? (
              <Button data-testid="restore-adapter" disabled={props.busy} onClick={props.onRestore}>
                恢复 Adapter
              </Button>
            ) : (
              <Button
                data-testid="archive-adapter"
                disabled={props.busy || productionRunning}
                onClick={props.onArchive}
              >
                归档 Adapter
              </Button>
            )}
            <Button data-testid="clone-adapter" disabled={props.busy} onClick={props.onClone}>
              复制 Adapter
            </Button>
          </Space>
          <p className="settings-field-hint">
            取消发布需先停止生产；归档后保存/发布/测试/启动均被禁用，可随时恢复；
            复制会以当前工作副本为新 Adapter 的 v1（未发布、未启动）。
          </p>

          <Divider />

          <p className="settings-danger-hint">
            无执行记录时可删除，将移除该 Adapter 及其全部版本，操作不可恢复；已有
            Execution 的 Adapter 为保留执行历史不可删除。
          </p>
          <Button
            danger
            data-testid="delete-adapter"
            disabled={props.busy}
            onClick={props.onDelete}
          >
            删除 Adapter
          </Button>
        </div>
      )}
    </Drawer>
  );
}
