/** Left panel: adapter search, list plus the create form. */

import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Card, Input, Space } from "antd";

import type { Adapter } from "../types";

interface AdapterListProps {
  adapters: Adapter[];
  selectedId: number | null;
  // Interaction lock: while a mutation is in flight, switching adapters and
  // starting a new create are disabled so state cannot be mixed across adapters.
  busy: boolean;
  onSelect: (adapter: Adapter) => void;
  // Returns true only when the adapter was actually created; the form is cleared
  // and closed only on real success, so failures keep the user's input editable.
  onCreate: (name: string, description: string) => Promise<boolean>;
}

export default function AdapterList({ adapters, selectedId, busy, onSelect, onCreate }: AdapterListProps) {
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
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
  const visible = keyword === "" ? adapters : adapters.filter((adapter) => adapter.name.toLowerCase().includes(keyword));

  return (
    <Card
      className="adapter-list"
      size="small"
      title="Adapter 列表"
      extra={
        <Button
          size="small"
          type="primary"
          data-testid="show-create-form"
          disabled={busy}
          onClick={() => setCreating(true)}
        >
          + 新建
        </Button>
      }
    >
      <Input.Search
        data-testid="adapter-search"
        placeholder="搜索 Adapter"
        allowClear
        size="small"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />

      {creating && (
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
      )}

      {visible.length === 0 ? (
        <p className="adapter-list-empty">{adapters.length === 0 ? "暂无 Adapter" : "没有匹配的 Adapter"}</p>
      ) : (
        <ul>
          {visible.map((adapter) => (
            <li key={adapter.id}>
              <button
                type="button"
                data-testid="adapter-item"
                className={adapter.id === selectedId ? "selected" : ""}
                disabled={busy}
                onClick={() => onSelect(adapter)}
              >
                {adapter.name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
