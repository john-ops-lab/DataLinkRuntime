/** Left panel: adapter list plus the create form. */

import { useState } from "react";
import type { FormEvent } from "react";

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

  return (
    <aside className="adapter-list">
      <div className="adapter-list-header">
        <h2>Adapters</h2>
        <button
          type="button"
          data-testid="show-create-form"
          disabled={busy}
          onClick={() => setCreating(true)}
        >
          + New Adapter
        </button>
      </div>

      {creating && (
        <form className="create-form" onSubmit={(event) => void handleCreate(event)}>
          <input
            data-testid="new-adapter-name"
            placeholder="name"
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
          <input
            data-testid="new-adapter-description"
            placeholder="description (optional)"
            value={description}
            disabled={busy}
            onChange={(event) => setDescription(event.target.value)}
          />
          <div className="create-form-actions">
            <button type="submit" data-testid="create-adapter" disabled={busy || submitting}>
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
                disabled={busy}
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
