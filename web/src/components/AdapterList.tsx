/** Left panel: adapter list plus the create form. */

import { useState } from "react";
import type { FormEvent } from "react";

import type { Adapter } from "../types";

interface AdapterListProps {
  adapters: Adapter[];
  selectedId: number | null;
  onSelect: (adapter: Adapter) => void;
  onCreate: (name: string, description: string) => Promise<void>;
}

export default function AdapterList({ adapters, selectedId, onSelect, onCreate }: AdapterListProps) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy) {
      return;
    }
    setBusy(true);
    try {
      await onCreate(trimmed, description);
      setName("");
      setDescription("");
      setCreating(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="adapter-list">
      <div className="adapter-list-header">
        <h2>Adapters</h2>
        <button type="button" data-testid="show-create-form" onClick={() => setCreating(true)}>
          + New Adapter
        </button>
      </div>

      {creating && (
        <form className="create-form" onSubmit={(event) => void handleCreate(event)}>
          <input
            data-testid="new-adapter-name"
            placeholder="name"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <input
            data-testid="new-adapter-description"
            placeholder="description (optional)"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <div className="create-form-actions">
            <button type="submit" data-testid="create-adapter" disabled={busy}>
              Create
            </button>
            <button type="button" onClick={() => setCreating(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {adapters.length === 0 ? (
        <p className="adapter-list-empty">No adapters yet.</p>
      ) : (
        <ul>
          {adapters.map((adapter) => (
            <li key={adapter.id}>
              <button
                type="button"
                data-testid="adapter-item"
                className={adapter.id === selectedId ? "selected" : ""}
                onClick={() => onSelect(adapter)}
              >
                {adapter.name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
