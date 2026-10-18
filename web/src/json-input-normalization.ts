/** Normalizes parsed JSON only, preserving every non-NUL character and JSON type. */
export class JsonInputKeyConflict extends Error {
  constructor(public readonly path: string, public readonly key: string) {
    super("input_json_key_conflict");
  }
}

export function normalizeJsonInput(value: unknown, path = ""): { value: unknown; removed: number } {
  if (typeof value === "string") {
    return { value: value.split("\u0000").join(""), removed: value.split("\u0000").length - 1 };
  }
  if (Array.isArray(value)) {
    const items = value.map((item, index) => normalizeJsonInput(item, `${path}/${index}`));
    return { value: items.map((item) => item.value), removed: items.reduce((sum, item) => sum + item.removed, 0) };
  }
  if (value !== null && typeof value === "object") {
    const keys = new Set<string>();
    let removed = 0;
    const entries = Object.entries(value).map(([key, item]) => {
      const cleanKey = normalizeJsonInput(key);
      const normalizedKey = cleanKey.value as string;
      if (keys.has(normalizedKey)) throw new JsonInputKeyConflict(path, normalizedKey);
      keys.add(normalizedKey);
      const normalized = normalizeJsonInput(item, `${path}/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`);
      removed += cleanKey.removed + normalized.removed;
      return [normalizedKey, normalized.value];
    });
    return { value: Object.fromEntries(entries), removed };
  }
  return { value, removed: 0 };
}
