/** Finite RFC 6901 mapping, filtering, sorting and de-duplication. */

const MAX_POINTER_BYTES = 1_024;
const MAX_POINTER_TOKENS = 64;
const MAX_POINTER_TOKEN_BYTES = 256;
const MAX_TARGET_BYTES = 256;
const CONVERSIONS = new Set([undefined, null, "string", "integer", "number", "boolean", "datetime"]);

function validPointer(path) {
  if (typeof path !== "string" || Buffer.byteLength(path, "utf8") > MAX_POINTER_BYTES) return false;
  if (path === "") return true;
  if (!path.startsWith("/")) return false;
  const tokens = path.slice(1).split("/");
  return tokens.length <= MAX_POINTER_TOKENS && tokens.every((raw) => (
    !/~(?:[^01]|$)/.test(raw)
    && Buffer.byteLength(raw.replaceAll("~1", "/").replaceAll("~0", "~"), "utf8")
      <= MAX_POINTER_TOKEN_BYTES
  ));
}

function validTarget(target) {
  return typeof target === "string" && Buffer.byteLength(target, "utf8") >= 1
    && Buffer.byteLength(target, "utf8") <= MAX_TARGET_BYTES
    && !/[\u0000-\u001f]/.test(target);
}

function pointer(value, path) {
  if (!validPointer(path)) throw new Error("invalid_json_pointer");
  if (path === "") return value;
  let current = value;
  for (const raw of path.slice(1).split("/")) {
    const token = raw.replaceAll("~1", "/").replaceAll("~0", "~");
    if (Array.isArray(current) && /^\d+$/.test(token) && Number(token) < current.length) current = current[Number(token)];
    else if (current && typeof current === "object" && Object.hasOwn(current, token)) current = current[token];
    else throw new Error("missing_pointer");
  }
  return current;
}

function convert(value, kind) {
  if (kind === undefined || kind === null) return value;
  if (kind === "string") return String(value);
  if (kind === "integer") {
    if (typeof value !== "number"
        && (typeof value !== "string" || value.length > 32
          || !/^-?(?:0|[1-9][0-9]*)$/.test(value))) throw new Error("invalid_integer");
    const converted = Number(value);
    if (!Number.isSafeInteger(converted)) throw new Error("invalid_integer");
    return converted;
  }
  if (kind === "number") {
    if (typeof value !== "number"
        && (typeof value !== "string" || value.length > 128
          || !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/.test(value))) {
      throw new Error("invalid_number");
    }
    const converted = Number(value);
    if (!Number.isFinite(converted)) throw new Error("invalid_number");
    return converted;
  }
  if (kind === "boolean") {
    if ([true, "true", "1", 1].includes(value)) return true;
    if ([false, "false", "0", 0].includes(value)) return false;
    throw new Error("invalid_boolean");
  }
  if (kind === "datetime") {
    if (typeof value !== "string" || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) throw new Error("invalid_datetime");
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) throw new Error("invalid_datetime");
    return parsed.toISOString();
  }
  throw new Error("unsupported_conversion");
}

function matches(item, rule) {
  let value;
  let exists = true;
  try { value = pointer(item, rule.pointer); } catch (error) {
    if (!(error instanceof Error) || error.message !== "missing_pointer") throw error;
    exists = false;
  }
  if ((rule.op ?? "equals") === "exists") return exists === (rule.value ?? true);
  if ((rule.op ?? "equals") === "equals") return exists && jsonEquals(value, rule.value);
  throw new Error("unsupported_filter");
}

function stableKey(value) {
  if (value === null || value === undefined) return [1, ""];
  return [0, canonicalJson(value)];
}

function jsonEquals(left, right) {
  if (typeof left === "boolean" || typeof right === "boolean") return left === right;
  if (typeof left === "number" || typeof right === "number") {
    return typeof left === "number" && typeof right === "number" && left === right;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((value, index) => jsonEquals(value, right[index]));
  }
  if ((left && typeof left === "object") || (right && typeof right === "object")) {
    if (!left || !right || typeof left !== "object" || typeof right !== "object"
        || Array.isArray(left) || Array.isArray(right)) return false;
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length
      && leftKeys.every((key, index) => key === rightKeys[index]
        && jsonEquals(left[key], right[key]));
  }
  return left === right;
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    const result = Object.create(null);
    for (const key of Object.keys(value).sort()) result[key] = canonicalize(value[key]);
    return result;
  }
  return value;
}

function canonicalJson(value) {
  const encoded = JSON.stringify(canonicalize(value));
  if (typeof encoded !== "string") throw new Error("output_not_json");
  return encoded;
}

function boundedInteger(input, key, fallback, maximum) {
  if (!Object.hasOwn(input, key)) return fallback;
  const value = input[key];
  if (!Number.isInteger(value) || value < 1 || value > maximum) throw new Error("invalid_limits");
  return value;
}

export function handle(context, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  if (!Array.isArray(input.records) || !Array.isArray(input.mappings)) throw new Error("records_and_mappings_required");
  const maxRecords = boundedInteger(input, "max_records", 10_000, 100_000);
  const maxFields = boundedInteger(input, "max_fields", 200, 1_000);
  const maxOutputBytes = boundedInteger(input, "max_output_bytes", 4_194_304, 16_777_216);
  if (input.mappings.length > maxFields) throw new Error("invalid_limits");
  const filters = input.filters ?? [];
  if (!Array.isArray(filters)) throw new Error("invalid_filters");
  if (filters.length > maxFields || filters.some((rule) => !rule || typeof rule !== "object"
      || Array.isArray(rule) || !validPointer(rule.pointer)
      || !["equals", "exists"].includes(rule.op ?? "equals")
      || (rule.op ?? "equals") === "exists"
      && Object.hasOwn(rule, "value") && typeof rule.value !== "boolean")) {
    throw new Error("invalid_filter");
  }
  if (input.dedupe_by !== undefined && input.dedupe_by !== null
      && !validTarget(input.dedupe_by)) {
    throw new Error("invalid_dedupe");
  }
  if (input.sort !== undefined
      && input.sort !== null
      && (!input.sort || typeof input.sort !== "object" || Array.isArray(input.sort)
        || !validTarget(input.sort.field)
        || !["asc", "desc"].includes(input.sort.direction ?? "asc"))) {
    throw new Error("invalid_sort");
  }
  const sortField = input.sort?.field;
  const dedupeField = input.dedupe_by ?? undefined;
  const descending = input.sort?.direction === "desc";
  const seen = new Set();
  const candidates = [];
  let outputBytes = 2;
  let outputLimited = false;
  for (const [ordinal, record] of input.records.slice(0, maxRecords).entries()) {
    if (!filters.every((rule) => rule && typeof rule === "object" && matches(record, rule))) continue;
    const output = Object.create(null);
    const fieldSizes = new Map();
    let objectBytes = 2;
    let recordLimited = false;
    let sortValue;
    let dedupeValue;
    for (const mapping of input.mappings) {
      if (!mapping || typeof mapping !== "object"
          || Array.isArray(mapping) || typeof mapping.pointer !== "string"
          || !validTarget(mapping.target) || !CONVERSIONS.has(mapping.type)) {
        throw new Error("invalid_mapping");
      }
      if (!validPointer(mapping.pointer)) throw new Error("invalid_json_pointer");
      let value;
      try { value = pointer(record, mapping.pointer); } catch (error) {
        if (!(error instanceof Error) || error.message !== "missing_pointer") throw error;
        if (!Object.hasOwn(mapping, "default")) continue;
        value = mapping.default;
      }
      try {
        const converted = convert(value, mapping.type);
        if (mapping.target === sortField) sortValue = converted;
        if (mapping.target === dedupeField) dedupeValue = converted;
        if (recordLimited) continue;
        const encodedValue = JSON.stringify(converted);
        if (typeof encodedValue !== "string") throw new Error("output_not_json");
        const fieldSize = Buffer.byteLength(JSON.stringify(mapping.target), "utf8")
          + 1 + Buffer.byteLength(encodedValue, "utf8");
        const previousSize = fieldSizes.get(mapping.target);
        let nextObjectBytes = objectBytes - (previousSize ?? 0) + fieldSize;
        if (previousSize === undefined && fieldSizes.size > 0) nextObjectBytes += 1;
        if (nextObjectBytes > maxOutputBytes) {
          recordLimited = true;
          if (sortField === undefined) break;
          continue;
        }
        output[mapping.target] = converted;
        fieldSizes.set(mapping.target, fieldSize);
        objectBytes = nextObjectBytes;
      } catch { throw new Error("conversion_failed"); }
    }
    if (dedupeField !== undefined) {
      const key = canonicalJson(recordLimited ? dedupeValue : output[dedupeField]);
      if (seen.has(key)) continue;
      seen.add(key);
    }
    if (recordLimited) {
      outputLimited = true;
      if (sortField === undefined) break;
    }
    const candidateOutput = recordLimited ? null : output;
    const encodedBytes = recordLimited ? maxOutputBytes + 1 : objectBytes;
    if (sortField === undefined) {
      const addition = encodedBytes + (candidates.length === 0 ? 0 : 1);
      if (outputBytes + addition > maxOutputBytes) { outputLimited = true; break; }
      candidates.push({ output, encodedBytes, ordinal, order: [0, ""] });
      outputBytes += addition;
      continue;
    }

    const candidate = {
      output: candidateOutput,
      encodedBytes,
      ordinal,
      order: stableKey(recordLimited ? sortValue : output[sortField]),
    };
    let low = 0; let high = candidates.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      const existing = candidates[middle];
      const comparison = candidate.order[0] - existing.order[0]
        || candidate.order[1].localeCompare(existing.order[1]);
      const before = comparison === 0
        ? ordinal < existing.ordinal
        : (descending ? comparison > 0 : comparison < 0);
      if (before) high = middle; else low = middle + 1;
    }
    candidates.splice(low, 0, candidate);
    let boundedBytes = 2;
    for (let at = 0; at < candidates.length; at += 1) {
      const item = candidates[at];
      const addition = item.encodedBytes + (at === 0 ? 0 : 1);
      if (boundedBytes + addition > maxOutputBytes) {
        item.output = null;
        candidates.splice(at + 1);
        outputLimited = true;
        break;
      }
      boundedBytes += addition;
    }
  }
  const bounded = candidates.flatMap((item) => item.output === null ? [] : [item.output]);
  const inputLimited = input.records.length > maxRecords;
  return {
    records: bounded,
    count: bounded.length,
    partial: inputLimited || outputLimited,
    checkpoint: inputLimited
      ? { reason: "input_limit", next_index: maxRecords }
      : outputLimited ? { reason: "output_limit", emitted: bounded.length } : null,
  };
}
