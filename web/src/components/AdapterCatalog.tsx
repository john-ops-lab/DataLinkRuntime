/** 左侧 Adapter Catalog：高密度行式导航 + 新建表单（M3.1 §7，业务合同仍沿用 M1）。 */

import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Drawer, Dropdown, Input, Radio, Space } from "antd";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, AdapterLanguage, AdapterType, Worker } from "../types";

type AdapterTypeFilter = "task-manual" | "task-schedule" | "webhook";

const ADAPTER_TYPE_FILTERS: ReadonlyArray<{ value: AdapterTypeFilter; label: string }> = [
  { value: "task-manual", label: "任务型（手动）" },
  { value: "task-schedule", label: "任务型（定时）" },
  { value: "webhook", label: "Webhook" },
];

function matchesTypeFilter(adapter: Adapter, filter: AdapterTypeFilter): boolean {
  if (filter === "webhook") {
    return adapter.adapter_type === "webhook";
  }
  return adapter.adapter_type === "task" && adapter.run_mode === filter.replace("task-", "");
}

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
  // M5.5.9：列表项三点菜单——“设置”直接进入该 Adapter 设置；“复制”进入 Clone 流程。
  onOpenSettings: (adapter: Adapter) => void;
  onClone: (adapter: Adapter) => void;
}

function versionLabel(
  versionId: number,
  serverSeq: number | null | undefined,
  versionSeqById: Map<number, number>,
): string {
  const seq = serverSeq ?? versionSeqById.get(versionId);
  return seq === undefined ? `#${versionId}` : `v${seq}`;
}

function catalogRuntimeStatus(adapter: Adapter): {
  dot: "running" | "stopped";
  label: string;
  fact: string;
} {
  // M5.5.10：主界面不展示内部 Execution #N，只保留用户可见状态。
  if (adapter.adapter_type === "task") {
    if (adapter.running_execution_id != null) {
      return { dot: "running", label: "运行中", fact: "运行中" };
    }
    const label = adapter.runtime_locked ? "定时运行中" : "空闲";
    return { dot: adapter.runtime_locked ? "running" : "stopped", label, fact: label };
  }
  if (adapter.running_execution_id != null) {
    return { dot: "running", label: "调用中", fact: "调用中" };
  }
  const label = adapter.runtime_locked ? "接收中" : "已停止";
  return { dot: adapter.runtime_locked ? "running" : "stopped", label, fact: label };
}

function catalogSubtitle(
  adapter: Adapter,
  runtimeStatus: ReturnType<typeof catalogRuntimeStatus>,
  versionSeqById: Map<number, number>,
  workersById: Map<number, Worker>,
): { primary: string; attention: string[]; full: string } {
  // M5.5.9：目录行直接展示 Adapter 类型，便于快速扫描。
  const typeLabel = adapter.adapter_type === "task" ? "[任务]" : "[Webhook]";
  if (adapter.adapter_type === "task") {
    const attention: string[] = [];
    const mode = adapter.run_mode === "schedule" ? "定时运行" : "手动运行";
    const workerId = adapter.runtime_worker_id;
    if (workerId != null) {
      const worker = workersById.get(workerId);
      if (worker !== undefined && worker.status !== "online") {
        attention.push("运行节点离线");
      }
    }
    const primary = `${typeLabel} ${LANGUAGE_LABELS[adapter.language]} · ${mode} · ${runtimeStatus.fact}`;
    const fullParts = [primary, ...attention];
    if (workerId == null) {
      fullParts.push("运行节点未配置");
    } else {
      const worker = workersById.get(workerId);
      fullParts.push(worker === undefined ? `运行节点 #${workerId}` : `运行节点 ${worker.name}`);
    }
    if (adapter.description.trim() !== "") {
      fullParts.push(adapter.description.trim());
    }
    return { primary, attention, full: fullParts.join(" · ") };
  }
  const attention: string[] = [];
  const workerId = adapter.runtime_worker_id;
  if (workerId !== null && workerId !== undefined) {
    const worker = workersById.get(workerId);
    if (worker !== undefined && worker.status !== "online") {
      attention.push("运行节点离线");
    }
  }
  const revision =
    adapter.latest_version_id == null
      ? "未保存"
      : `已保存 ${versionLabel(adapter.latest_version_id, null, versionSeqById)}`;
  const primary = `${typeLabel} ${LANGUAGE_LABELS[adapter.language]} · ${runtimeStatus.fact} · ${revision}`;
  const fullParts = [primary, ...attention];
  if (workerId === null || workerId === undefined) {
    fullParts.push("运行节点未配置");
  } else {
    const worker = workersById.get(workerId);
    fullParts.push(worker === undefined ? `运行节点 #${workerId}` : `运行节点 ${worker.name}`);
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
  onOpenSettings,
  onClone,
}: AdapterCatalogProps) {
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
  const [typeFilters, setTypeFilters] = useState<AdapterTypeFilter[]>([]);
  const [typeFilterOpen, setTypeFilterOpen] = useState(false);
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
  const inView = adapters.filter((adapter) => !adapter.archived_at);
  const typeFiltered =
    typeFilters.length === 0
      ? inView
      : inView.filter((adapter) => typeFilters.some((filter) => matchesTypeFilter(adapter, filter)));
  const visible = keyword === ""
    ? typeFiltered
    : typeFiltered.filter((adapter) =>
        [adapter.name, adapter.description].some((value) => value.toLowerCase().includes(keyword)),
      );
  const typeFilterLabel = typeFilters.length === 0 ? "类型" : `类型（${typeFilters.length}）`;
  const typeFilterPanel = (
    <div
      className="catalog-type-filter-menu"
      data-testid="adapter-type-filter-menu"
      role="group"
      aria-label="适配器类型筛选"
      onMouseDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
    >
      <div className="catalog-type-filter-actions">
        <Button
          type="link"
          size="small"
          data-testid="adapter-type-select-all"
          onClick={() => setTypeFilters(ADAPTER_TYPE_FILTERS.map((option) => option.value))}
        >
          全选
        </Button>
        <Button
          type="link"
          size="small"
          data-testid="adapter-type-clear"
          onClick={() => setTypeFilters([])}
        >
          清空
        </Button>
        <Button
          type="link"
          size="small"
          data-testid="adapter-filter-clear-all"
          onClick={() => {
            setTypeFilters([]);
            setSearch("");
          }}
        >
          清空全部
        </Button>
      </div>
      {ADAPTER_TYPE_FILTERS.map((option) => (
        <label className="catalog-type-filter-option" key={option.value}>
          <input
            type="checkbox"
            checked={typeFilters.includes(option.value)}
            aria-label={option.label}
            onChange={() => {
              setTypeFilters((current) => current.includes(option.value)
                ? current.filter((value) => value !== option.value)
                : [...current, option.value]);
            }}
          />
          <span>{option.label}</span>
        </label>
      ))}
    </div>
  );

  return (
    <aside className="catalog" data-testid="adapter-catalog">
      <div className="catalog-header">
        <h2 className="catalog-title">适配器</h2>
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
        <Space.Compact className="catalog-search-control" style={{ width: "100%" }}>
          <Input
            data-testid="adapter-search"
            aria-label="搜索适配器"
            placeholder="搜索适配器"
            allowClear
            size="small"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <Dropdown
            open={typeFilterOpen}
            onOpenChange={setTypeFilterOpen}
            trigger={["click"]}
            popupRender={() => typeFilterPanel}
          >
            <Button
              type="text"
              size="small"
              className="catalog-type-filter-trigger"
              data-testid="adapter-type-filter"
              aria-label={`类型筛选，${typeFilters.length === 0 ? "全部类型" : `已选 ${typeFilters.length} 项`}`}
              aria-haspopup="true"
              aria-expanded={typeFilterOpen}
            >
              {typeFilterLabel}
            </Button>
          </Dropdown>
        </Space.Compact>
      </div>

      <div className="catalog-list">
        {visible.length === 0 ? (
          <p className="catalog-empty">{inView.length === 0 ? "暂无适配器" : "没有匹配的适配器"}</p>
        ) : (
          visible.map((adapter) => {
            const runtimeStatus = catalogRuntimeStatus(adapter);
            const statusDescription = `${adapter.adapter_type === "task" ? "任务" : "Webhook "}状态：${runtimeStatus.label}`;
            const subtitle = catalogSubtitle(adapter, runtimeStatus, versionSeqById, workersById);
            const accessibleRuntimeFact = statusDescription;
            return (
              <div key={adapter.id} className="catalog-row">
                <button
                  type="button"
                  data-testid="adapter-item"
                  className={adapter.id === selectedId ? "catalog-item selected" : "catalog-item"}
                  disabled={busy}
                  title={`${adapter.name}${adapter.description ? ` — ${adapter.description}` : ""}\n${subtitle.full}`}
                  aria-label={`${adapter.name}，${subtitle.full.replace(runtimeStatus.fact, accessibleRuntimeFact)}`}
                  onClick={() => onSelect(adapter)}
                >
                  <span className="catalog-item-name">
                    <span
                      className={`catalog-status-dot catalog-status-${runtimeStatus.dot}`}
                      title={statusDescription}
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
                {/* M5.5.9：三点菜单只提供“设置/复制”；点击菜单按钮不触发行选择，
                    再次点击或点击空白处由 Dropdown 关闭，键盘可达（原生 Button）。 */}
                <Dropdown
                  trigger={["click"]}
                  placement="bottomRight"
                  menu={{
                    items: [
                      { key: "settings", label: "设置" },
                      { key: "clone", label: "复制" },
                    ],
                    onClick: ({ key }) => {
                      if (key === "settings") {
                        onOpenSettings(adapter);
                      } else if (key === "clone") {
                        onClone(adapter);
                      }
                    },
                  }}
                >
                  <Button
                    size="small"
                    type="text"
                    className="catalog-item-menu"
                    disabled={busy}
                    aria-label={`${adapter.name} 更多操作`}
                    data-testid="adapter-item-menu"
                  >
                    ···
                  </Button>
                </Dropdown>
              </div>
            );
          })
        )}
      </div>

      <Drawer
        title="新建适配器"
        width={360}
        open={creating}
        destroyOnHidden
        onClose={() => setCreating(false)}
      >
        <form className="create-form" onSubmit={(event) => void handleCreate(event)}>
          <Input
            data-testid="new-adapter-name"
            aria-label="适配器名称"
            placeholder="名称"
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
          <div className="settings-field" role="radiogroup" aria-label="适配器类型">
            <span className="settings-field-label">适配器类型</span>
            <Radio.Group
              data-testid="new-adapter-type"
              value={adapterType}
              disabled={busy}
              onChange={(event) => setAdapterType(event.target.value as AdapterType)}
            >
              <Radio value="task">任务型适配器</Radio>
              <Radio value="webhook">Webhook 适配器</Radio>
            </Radio.Group>
          </div>
          <div
            className="settings-field"
            role="radiogroup"
            aria-label="适配器开发语言"
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
            aria-label="适配器描述"
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
