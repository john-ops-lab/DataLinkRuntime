/** 左侧 Adapter Catalog：高密度行式导航 + 新建表单（M3.1 §7，业务合同仍沿用 M1）。 */

import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Drawer, Input, Segmented, Space } from "antd";

import {
  productionDisplayState,
  productionStateLabel,
} from "../status";
import type { Adapter } from "../types";

interface AdapterCatalogProps {
  adapters: Adapter[];
  selectedId: number | null;
  // Interaction lock: while a mutation is in flight, switching adapters and
  // starting a new create are disabled so state cannot be mixed across adapters.
  busy: boolean;
  onSelect: (adapter: Adapter) => void;
  // Returns true only when the adapter was actually created; the form is cleared
  // and closed only on real success, so failures keep the user's input editable.
  onCreate: (name: string, description: string) => Promise<boolean>;
  // The list API only exposes latest/published version ids, no seq. Once an
  // adapter's version list has been loaded, its known seq values are cached
  // in App state and keep showing across adapter switches; adapters that were
  // never loaded still expose their real saved/published state via the
  // version pointers, never hiding a published status (Issue #8 补充).
  latestSeqById: Map<number, number>;
  publishedSeqById: Map<number, number>;
}

function catalogSubtitle(
  adapter: Adapter,
  latestSeqById: Map<number, number>,
  publishedSeqById: Map<number, number>,
): string {
  if (adapter.latest_version_id === null) {
    return "暂无版本";
  }
  const latestSeq = latestSeqById.get(adapter.id);
  const publishedSeq = publishedSeqById.get(adapter.id);
  if (latestSeq === undefined) {
    // Versions not loaded yet: no seq is invented, but the real published
    // state stays visible instead of degrading to a neutral placeholder.
    return adapter.published_version_id === null ? "已保存 · 未发布" : "已保存 · 已发布";
  }
  if (publishedSeq !== undefined) {
    return `v${latestSeq} · Published v${publishedSeq}`;
  }
  return adapter.published_version_id === null ? `v${latestSeq} · 未发布` : `v${latestSeq} · 已发布`;
}

export default function AdapterCatalog({
  adapters,
  selectedId,
  busy,
  onSelect,
  onCreate,
  latestSeqById,
  publishedSeqById,
}: AdapterCatalogProps) {
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
  // M3.2：归档 Adapter 默认隐藏，避免污染活跃工作列表。
  const [view, setView] = useState<"active" | "archived">("active");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      const created = await onCreate(trimmed, description);
      if (created) {
        setName("");
        setDescription("");
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
    keyword === "" ? inView : inView.filter((adapter) => adapter.name.toLowerCase().includes(keyword));

  return (
    <aside className="catalog" data-testid="adapter-catalog">
      <div className="catalog-header">
        <h2 className="catalog-title">Adapters</h2>
        <Button
          size="small"
          type="primary"
          data-testid="show-create-form"
          disabled={busy}
          onClick={() => setCreating(true)}
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
            return (
              <button
                key={adapter.id}
                type="button"
                data-testid="adapter-item"
                className={adapter.id === selectedId ? "catalog-item selected" : "catalog-item"}
                disabled={busy}
                onClick={() => onSelect(adapter)}
              >
                <span className="catalog-item-name">
                  <span
                    className={`catalog-status-dot catalog-status-${displayState}`}
                    title={`生产状态：${productionStateLabel(displayState)}`}
                  />
                  {adapter.name}
                </span>
                <span className="catalog-item-sub">
                  {catalogSubtitle(adapter, latestSeqById, publishedSeqById)}
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
        destroyOnClose
        onClose={() => setCreating(false)}
      >
        <form className="create-form" onSubmit={(event) => void handleCreate(event)}>
          <Input
            data-testid="new-adapter-name"
            placeholder="名称"
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
          <Input
            data-testid="new-adapter-description"
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
