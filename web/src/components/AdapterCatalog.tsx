/** 左侧 Adapter Catalog：高密度行式导航 + 新建表单（M3.1 §7，业务合同仍沿用 M1）。 */

import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Drawer, Input, Radio, Segmented, Space } from "antd";

import {
  hasActiveProductionExecution,
  productionDisplayState,
  productionRunningVersionId,
  productionRunningVersionSeq,
  productionStateLabel,
} from "../status";
import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, AdapterLanguage, Worker } from "../types";

interface AdapterCatalogProps {
  adapters: Adapter[];
  selectedId: number | null;
  // Interaction lock: while a mutation is in flight, switching adapters and
  // starting a new create are disabled so state cannot be mixed across adapters.
  busy: boolean;
  onSelect: (adapter: Adapter) => void;
  // Returns true only when the adapter was actually created; the form is cleared
  // and closed only on real success, so failures keep the user's input editable.
  onCreate: (name: string, description: string, language: AdapterLanguage) => Promise<boolean>;
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
  const displayState = productionDisplayState(adapter);
  const runningVersionId = productionRunningVersionId(adapter);
  const attention: string[] = [];
  let productionFact = "未发布";

  if (runningVersionId !== null) {
    // M5.1: the locked production version is expressed from the server-side
    // production_version_id/seq; “生产运行” is reserved for a real active
    // Execution, an open but idle entry says “生产入口已开启 · 空闲”.
    const versionText = versionLabel(
      runningVersionId,
      productionRunningVersionSeq(adapter),
      versionSeqById,
    );
    if (displayState === "stopping") {
      productionFact = `停止中 ${versionText}`;
      attention.push(
        adapter.running_execution_id === null || adapter.running_execution_id === undefined
          ? "等待执行完成"
          : `等待 Execution #${adapter.running_execution_id} 完成`,
      );
    } else if (hasActiveProductionExecution(adapter)) {
      productionFact = `生产运行 ${versionText}`;
    } else {
      productionFact = `生产入口已开启 · 空闲 ${versionText}`;
    }
  } else if (
    displayState === "stopped" &&
    adapter.last_production_version_id !== null &&
    adapter.last_production_version_id !== undefined
  ) {
    productionFact = `已停止 · 上次 ${versionLabel(
        adapter.last_production_version_id,
        adapter.last_production_version_seq,
        versionSeqById,
      )}`;
  } else if (adapter.published_version_id !== null && adapter.published_version_id !== undefined) {
    productionFact = `待启动 ${versionLabel(
      adapter.published_version_id,
      adapter.published_version_seq,
      versionSeqById,
    )}`;
  }
  const publishedVersionId = adapter.published_version_id;
  const comparisonVersionId = runningVersionId ?? adapter.last_production_version_id ?? null;
  if (
    publishedVersionId !== null &&
    publishedVersionId !== undefined &&
    comparisonVersionId !== null &&
    publishedVersionId !== comparisonVersionId
  ) {
    attention.push(
      `${versionLabel(publishedVersionId, adapter.published_version_seq, versionSeqById)} 待启动`,
    );
  }
  const workerId = adapter.production_worker_id;
  if (workerId !== null && workerId !== undefined) {
    const worker = workersById.get(workerId);
    if (worker !== undefined && worker.status !== "online") {
      attention.push("Worker 离线");
    }
  }
  const primary = `${LANGUAGE_LABELS[adapter.language]} · ${productionFact}`;
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
      const created = await onCreate(trimmed, description, language);
      if (created) {
        setName("");
        setDescription("");
        setLanguage("python");
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
            const displayState = productionDisplayState(adapter);
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
                    className={`catalog-status-dot catalog-status-${displayState}`}
                    title={`生产状态：${productionStateLabel(displayState)}`}
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
