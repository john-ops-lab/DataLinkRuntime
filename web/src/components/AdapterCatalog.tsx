/** 左侧 Adapter Catalog：高密度行式导航 + 新建表单（M3.1 §7，业务合同仍沿用 M1）。 */

import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Drawer, Input, Radio, Segmented, Space } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, AdapterLanguage, AdapterType, Worker } from "../types";

interface AdapterCatalogProps {
  adapters: Adapter[];
  selectedId: number | null;
  // Interaction lock: while a mutation is in flight, switching adapters and
  // starting a new create are disabled so state cannot be mixed across adapters.
  busy: boolean;
  onSelect: (adapter: Adapter) => void;
  // Returns true only when the adapter was actually created; the form is cleared
  // and closed only on real success, so failures keep the user's input editable.
  onCreate: (
    name: string,
    description: string,
    language: AdapterLanguage,
    adapterType: AdapterType,
  ) => Promise<boolean>;
  // Version lists are loaded only for selected Adapters. Known id -> seq
  // mappings are cached by App; unknown ids stay explicit as #id instead of
  // causing one list request per Catalog row.
  versionSeqById: Map<number, number>;
  // Loaded once by App and shared with Worker status/settings/Catalog. This
  // keeps Worker names visible without a per-Adapter request.
  workers: Worker[];
}

function versionLabel(
  versionId: number,
  serverSeq: number | null | undefined,
  versionSeqById: Map<number, number>,
): string {
  const seq = serverSeq ?? versionSeqById.get(versionId);
  return seq === undefined ? `#${versionId}` : `v${seq}`;
}

function catalogSubtitle(
  adapter: Adapter,
  versionSeqById: Map<number, number>,
  workersById: Map<number, Worker>,
): { primary: string; attention: string[]; full: string } {
  if (adapter.adapter_type === "task") {
    const attention: string[] = [];
    const mode = adapter.run_mode === "schedule" ? "定时运行" : "手动运行";
    const runtimeFact =
      adapter.running_execution_id != null
        ? `Execution #${adapter.running_execution_id} 运行中`
        : adapter.runtime_locked
          ? "定时运行中"
          : "空闲";
    const workerId = adapter.runtime_worker_id;
    if (workerId != null) {
      const worker = workersById.get(workerId);
      if (worker !== undefined && worker.status !== "online") {
        attention.push("Worker 离线");
      }
    }
    const primary = `${LANGUAGE_LABELS[adapter.language]} · ${mode} · ${runtimeFact}`;
    const fullParts = [primary, ...attention];
    if (workerId == null) {
      fullParts.push("Worker 未配置");
    } else {
      const worker = workersById.get(workerId);
      fullParts.push(worker === undefined ? `Worker #${workerId}` : `Worker ${worker.name}`);
    }
    if (adapter.description.trim() !== "") {
      fullParts.push(adapter.description.trim());
    }
    return { primary, attention, full: fullParts.join(" · ") };
  }
  const attention: string[] = [];
  const runtimeFact =
    adapter.running_execution_id != null
      ? `调用 #${adapter.running_execution_id} 运行中`
      : adapter.runtime_locked
        ? "接收中"
        : "已停止";
  const workerId = adapter.runtime_worker_id;
  if (workerId !== null && workerId !== undefined) {
    const worker = workersById.get(workerId);
    if (worker !== undefined && worker.status !== "online") {
      attention.push("Worker 离线");
    }
  }
  const revision =
    adapter.latest_version_id == null
      ? "未保存 Revision"
      : `Revision ${versionLabel(adapter.latest_version_id, null, versionSeqById)}`;
  const primary = `${LANGUAGE_LABELS[adapter.language]} · ${runtimeFact} · ${revision}`;
  const fullParts = [primary, ...attention];
  if (workerId === null || workerId === undefined) {
    fullParts.push("Worker 未配置");
  } else {
    const worker = workersById.get(workerId);
    fullParts.push(worker === undefined ? `Worker #${workerId}` : `Worker ${worker.name}`);
  }
  if (adapter.description.trim() !== "") {
    fullParts.push(adapter.description.trim());
  }
  return { primary, attention, full: fullParts.join(" · ") };
}

export default function AdapterCatalog({
  adapters,
  selectedId,
  busy,
  onSelect,
  onCreate,
  versionSeqById,
  workers,
}: AdapterCatalogProps) {
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
  // M3.2：归档 Adapter 默认隐藏，避免污染活跃工作列表。
  const [view, setView] = useState<"active" | "archived">("active");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [language, setLanguage] = useState<AdapterLanguage>("python");
  const [adapterType, setAdapterType] = useState<AdapterType>("task");
  const [submitting, setSubmitting] = useState(false);
  const workersById = new Map(workers.map((worker) => [worker.id, worker]));

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      const created = await onCreate(trimmed, description, language, adapterType);
      if (created) {
        setName("");
        setDescription("");
        setLanguage("python");
        setAdapterType("task");
        setCreating(false);
      }
    } finally {
      setSubmitting(false);
    }
  }

  const keyword = search.trim().toLowerCase();
  const inView = adapters.filter((adapter) =>
    view === "archived" ? !!adapter.archived_at : !adapter.archived_at,
  );
  const visible =
    keyword === ""
      ? inView
      : inView.filter((adapter) =>
          [adapter.name, adapter.description].some((value) => value.toLowerCase().includes(keyword)),
        );

  return (
    <aside className="catalog" data-testid="adapter-catalog">
      <div className="catalog-header">
        <h2 className="catalog-title">Adapters</h2>
        <Button
          size="small"
          type="primary"
          data-testid="show-create-form"
          disabled={busy}
          onClick={() => {
            setLanguage("python");
            setAdapterType("task");
            setCreating(true);
          }}
        >
          新建
        </Button>
      </div>

      <div className="catalog-search">
        <Input
          data-testid="adapter-search"
          placeholder="搜索 Adapter"
          allowClear
          size="small"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <Segmented
          block
          size="small"
          data-testid="catalog-view"
          className="catalog-view-switch"
          value={view}
          options={[
            { label: "活跃", value: "active" },
            { label: "已归档", value: "archived" },
          ]}
          onChange={(value) => setView(value as "active" | "archived")}
        />
      </div>

      <div className="catalog-list">
        {visible.length === 0 ? (
          <p className="catalog-empty">{inView.length === 0 ? (view === "archived" ? "暂无已归档 Adapter" : "暂无 Adapter") : "没有匹配的 Adapter"}</p>
        ) : (
          visible.map((adapter) => {
            const runtimeState =
              adapter.running_execution_id != null
                ? "running"
                : adapter.runtime_locked
                  ? "running"
                  : "stopped";
            const subtitle = catalogSubtitle(adapter, versionSeqById, workersById);
            return (
              <button
                key={adapter.id}
                type="button"
                data-testid="adapter-item"
                className={adapter.id === selectedId ? "catalog-item selected" : "catalog-item"}
                disabled={busy}
                title={`${adapter.name}${adapter.description ? ` — ${adapter.description}` : ""}\n${subtitle.full}`}
                aria-label={`${adapter.name}，${subtitle.full}`}
                onClick={() => onSelect(adapter)}
              >
                <span className="catalog-item-name">
                  <span
                    className={`catalog-status-dot catalog-status-${runtimeState}`}
                    title={adapter.adapter_type === "task"
                      ? `Task 状态：${adapter.running_execution_id != null ? "运行中" : adapter.runtime_locked ? "定时运行中" : "空闲"}`
                      : `Webhook 状态：${adapter.running_execution_id != null ? "调用中" : adapter.runtime_locked ? "接收中" : "已停止"}`}
                  />
                  {adapter.name}
                </span>
                <span className="catalog-item-sub" title={subtitle.full}>
                  <span>{subtitle.primary}</span>
                  {subtitle.attention.map((item) => (
                    <span className="catalog-item-attention" key={item}> · {item}</span>
                  ))}
                </span>
              </button>
            );
          })
        )}
      </div>

      <Drawer
        title="新建 Adapter"
        width={360}
        open={creating}
        destroyOnHidden
        onClose={() => setCreating(false)}
      >
        <form className="create-form" onSubmit={(event) => void handleCreate(event)}>
          <Input
            data-testid="new-adapter-name"
            aria-label="Adapter 名称"
            placeholder="名称"
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
          <div className="settings-field" role="radiogroup" aria-label="Adapter 类型">
            <span className="settings-field-label">Adapter 类型</span>
            <Radio.Group
              data-testid="new-adapter-type"
              value={adapterType}
              disabled={busy}
              onChange={(event) => setAdapterType(event.target.value as AdapterType)}
            >
              <Radio value="task">任务型 Adapter</Radio>
              <Radio value="webhook">Webhook Adapter</Radio>
            </Radio.Group>
          </div>
          <div
            className="settings-field"
            role="radiogroup"
            aria-label="Adapter 开发语言"
          >
            <span className="settings-field-label">开发语言</span>
            <Radio.Group
              data-testid="new-adapter-language"
              value={language}
              disabled={busy}
              onChange={(event) => setLanguage(event.target.value as AdapterLanguage)}
            >
              <Radio value="python">Python</Radio>
              <Radio value="javascript">JavaScript</Radio>
              <Radio value="java">Java</Radio>
            </Radio.Group>
          </div>
          <Input
            data-testid="new-adapter-description"
            aria-label="Adapter 描述"
            placeholder="描述（可选）"
            value={description}
            disabled={busy}
            onChange={(event) => setDescription(event.target.value)}
          />
          <Space className="create-form-actions">
            <Button
              type="primary"
              htmlType="submit"
              data-testid="create-adapter"
              loading={submitting}
              disabled={busy}
            >
              创建
            </Button>
            <Button onClick={() => setCreating(false)}>取消</Button>
          </Space>
        </form>
      </Drawer>
    </aside>
  );
}
