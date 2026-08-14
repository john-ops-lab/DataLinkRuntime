/** Adapter 设置 Drawer：低频的元数据编辑与删除退出主工作区（M3.1 §9.4）。 */

import { Alert, Button, Divider, Drawer, Input, Select, Space, Tag } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import { isProductionStopping } from "../status";
import type { Adapter, Worker } from "../types";

interface AdapterSettingsDrawerProps {
  open: boolean;
  adapter: Adapter | null;
  name: string;
  description: string;
  runtimeWorkerId: number | null;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  productionWorkerRetestRequired: boolean;
  busy: boolean;
  contentReady: boolean;
  onClose: () => void;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onRuntimeWorkerChange: (value: number | null) => void;
  onUpdate: () => void;
  onRuntimeWorkerUpdate: () => void;
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
  const productionStopping = adapter !== null && isProductionStopping(adapter);
  const workerChanged =
    adapter !== null && props.runtimeWorkerId !== (adapter.runtime_worker_id ?? null);
  const selectedWorker = props.workers.find(
    (worker) => worker.id === props.runtimeWorkerId,
  );
  const selectedWorkerCompatible =
    selectedWorker === undefined ||
    adapter === null ||
    selectedWorker.capabilities.includes(adapter.language);
  if (adapter?.adapter_type === "task") {
    return (
      <Drawer
        title="Task Adapter 设置"
        width={400}
        open={props.open}
        destroyOnHidden
        onClose={props.onClose}
      >
        <div className="settings-form">
          <label className="settings-field">
            <span className="settings-field-label">名称</span>
            <Input data-testid="adapter-name" value={props.name} disabled={props.busy} onChange={(event) => props.onNameChange(event.target.value)} />
          </label>
          <label className="settings-field">
            <span className="settings-field-label">开发语言</span>
            <Input data-testid="adapter-language" value={LANGUAGE_LABELS[adapter.language]} disabled />
          </label>
          <label className="settings-field">
            <span className="settings-field-label">描述</span>
            <Input data-testid="adapter-description" value={props.description} disabled={props.busy} onChange={(event) => props.onDescriptionChange(event.target.value)} />
          </label>
          <Button type="primary" data-testid="update-details" disabled={props.busy || !props.contentReady || archived} onClick={props.onUpdate}>
            更新信息
          </Button>
          <Divider />
          <Space direction="vertical" className="settings-lifecycle-actions">
            <Button data-testid="clone-adapter" disabled={props.busy} onClick={props.onClone}>复制 Adapter</Button>
            <Button danger data-testid="delete-adapter" disabled={props.busy || adapter.runtime_locked === true} onClick={props.onDelete}>删除 Adapter</Button>
          </Space>
          {adapter.runtime_locked === true && (
            <Alert type="warning" showIcon message="定时启用或 Execution 活跃期间不能删除 Adapter。" />
          )}
          <p className="settings-danger-hint">删除会将 Adapter 标记为只读并保留 Revision 与 Execution 历史。</p>
        </div>
      </Drawer>
    );
  }
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
            <span className="settings-field-label">开发语言</span>
            <Input
              data-testid="adapter-language"
              value={LANGUAGE_LABELS[props.adapter.language]}
              disabled
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
          <label className="settings-field">
            <span className="settings-field-label">生产 Worker</span>
            <Select
              data-testid="production-worker"
              value={props.runtimeWorkerId ?? undefined}
              placeholder="请选择生产 Worker"
              allowClear
              loading={props.workersLoading}
              disabled={props.busy || props.workersLoading || archived || productionRunning}
              optionLabelProp="label"
              onChange={(value: number | undefined) =>
                props.onRuntimeWorkerChange(value ?? null)
              }
              options={props.workers.map((worker) => ({
                value: worker.id,
                label: `${worker.name}（${worker.capabilities
                  .map((capability) => LANGUAGE_LABELS[capability as keyof typeof LANGUAGE_LABELS])
                  .filter(Boolean)
                  .join(" / ")}）`,
                disabled: !worker.capabilities.includes(props.adapter?.language ?? "python"),
                worker,
              }))}
              optionRender={(option) => {
                const worker = option.data.worker as Worker;
                return (
                  <Space>
                    <span>{worker.name}</span>
                    <Tag color={worker.status === "online" ? "green" : "red"}>
                      {worker.status === "online" ? "在线" : "离线"}
                    </Tag>
                    <span>
                      {worker.capabilities
                        .map(
                          (capability) =>
                            LANGUAGE_LABELS[capability as keyof typeof LANGUAGE_LABELS] ?? capability,
                        )
                        .join(" / ")}
                    </span>
                    {!worker.capabilities.includes(props.adapter?.language ?? "python") && (
                      <Tag color="orange">不支持 {LANGUAGE_LABELS[props.adapter?.language ?? "python"]}</Tag>
                    )}
                  </Space>
                );
              }}
            />
          </label>
          {props.workersError !== null && (
            <Alert
              type="error"
              showIcon
              data-testid="production-worker-error"
              message="Worker 列表加载失败"
              description={props.workersError}
            />
          )}
          {!props.workersLoading && props.workersError === null && props.workers.length === 0 && (
            <Alert type="warning" showIcon message="暂无已注册 Worker" />
          )}
          {selectedWorker?.status !== "online" && selectedWorker !== undefined && (
            <Alert
              type="warning"
              showIcon
              data-testid="production-worker-offline"
              message={`Worker ${selectedWorker.name} 当前离线`}
              description="离线 Worker 不能运行测试或启动生产。"
            />
          )}
          {!selectedWorkerCompatible && selectedWorker !== undefined && adapter !== null && (
            <Alert
              type="error"
              showIcon
              data-testid="production-worker-incompatible"
              message={`Worker ${selectedWorker.name} 不支持 ${LANGUAGE_LABELS[adapter.language]}`}
              description="请选择具备对应 Runtime capability 的 Worker。"
            />
          )}
          {productionRunning && (
            <Alert
              type="warning"
              showIcon
              data-testid="production-worker-locked"
              message="生产运行期间不可切换 Worker"
              description="请先 Stop 生产后再修改生产 Worker。"
            />
          )}
          {(workerChanged || props.productionWorkerRetestRequired) && (
            <Alert
              type="warning"
              showIcon
              data-testid="production-worker-retest"
              message="切换生产 Worker 后需重新测试"
              description="保存设置后，已发布版本必须先在新的生产 Worker 上测试成功，才能再次启动生产。"
            />
          )}
          <p className="settings-field-hint">
            测试与生产使用同一个 Worker；切换 Worker 后必须重新测试当前已发布版本。
          </p>
          <Button
            type="primary"
            data-testid="update-production-worker"
            disabled={
              props.busy ||
              props.workersLoading ||
              archived ||
              !workerChanged ||
              !selectedWorkerCompatible
            }
            onClick={props.onRuntimeWorkerUpdate}
          >
            保存生产 Worker
          </Button>
          <Space direction="vertical" className="settings-lifecycle-actions">
            <Button
              data-testid="unpublish-adapter"
              disabled={
                props.busy ||
                adapter === null ||
                adapter.published_version_id === null ||
                productionRunning ||
                productionStopping
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
                disabled={props.busy || productionRunning || productionStopping}
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
          {productionStopping && adapter?.running_execution_id != null && (
            <Alert
              type="info"
              showIcon
              data-testid="settings-production-stopping"
              message={`生产入口已关闭，等待 Execution #${adapter.running_execution_id} 完成`}
              description="Execution 进入终态并刷新后，取消发布与归档将自动解锁。"
            />
          )}

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
