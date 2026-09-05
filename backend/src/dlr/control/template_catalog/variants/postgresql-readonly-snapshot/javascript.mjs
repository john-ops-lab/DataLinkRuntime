/** Bounded read-only PostgreSQL snapshot. */
import pg from "pg";

const TEXT_DATE_OIDS = new Set([1082, 1083, 1114, 1184, 1266, 1700]);
const JSON_SAFE_TYPES = {
  getTypeParser(oid, format) {
    if (format === "text" && TEXT_DATE_OIDS.has(oid)) {
      if (oid === 1114 || oid === 1184) {
        return (value) => value.replace(" ", "T").replace(/([+-][0-9]{2})$/, "$1:00");
      }
      return (value) => value;
    }
    return pg.types.getTypeParser(oid, format);
  },
};

function positive(value, fallback, maximum) {
  return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback;
}
function normalizeCell(value, depth = 0) {
  if (depth > 32) throw new Error("unsupported_cell_type");
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("unsupported_cell_type");
    return value;
  }
  if (typeof value === "bigint") return value.toString();
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) throw new Error("unsupported_cell_type");
    return value.toISOString().replace(/Z$/, "+00:00");
  }
  if (Buffer.isBuffer(value)) return { $binary_base64: value.toString("base64") };
  if (ArrayBuffer.isView(value)) {
    return {
      $binary_base64: Buffer.from(value.buffer, value.byteOffset, value.byteLength).toString("base64"),
    };
  }
  if (value instanceof ArrayBuffer) {
    return { $binary_base64: Buffer.from(value).toString("base64") };
  }
  if (Array.isArray(value)) return value.map((item) => normalizeCell(item, depth + 1));
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) throw new Error("unsupported_cell_type");
    return Object.fromEntries(Object.entries(value).map(
      ([key, item]) => [key, normalizeCell(item, depth + 1)],
    ));
  }
  throw new Error("unsupported_cell_type");
}
function jsonBytes(value) {
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new Error("unsupported_cell_type");
  return Buffer.byteLength(encoded, "utf8");
}
function columnNames(fields) {
  if (!Array.isArray(fields) || fields.length === 0) throw new Error("missing_column_metadata");
  const names = fields.map((field) => String(field.name));
  if (new Set(names).size !== names.length) throw new Error("duplicate_column_label");
  return names;
}
function checkedQuery(input) {
  if (typeof input.sql !== "string" || !/^\s*select\b/i.test(input.sql)
      || input.sql.includes(";")
      || /\b(insert|update|delete|merge|call|execute|create|alter|drop|truncate|copy)\b/i.test(input.sql)) {
    throw new Error("single_select_required");
  }
  return input.sql;
}

export async function handle(context, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  const sql = checkedQuery(input);
  const params = input.params ?? [];
  if (!Array.isArray(params) || params.length > 64) throw new Error("params_must_be_array");
  const maxRows = positive(input.max_rows, 5_000, 100_000);
  const batchSize = positive(input.batch_size, 500, 5_000);
  const maxOutput = positive(input.max_output_bytes, 4_194_304, 16_777_216);
  const maxCell = positive(input.max_cell_bytes, 1_048_576, 8_388_608);
  const timeout = positive(input.timeout_seconds, 30, 300);
  const dsn = context.secrets.get("POSTGRES_DSN");
  if (!dsn) throw new Error("missing_credential");
  let client = null;
  let connected = false;
  const rows = [];
  try {
    client = new pg.Client({ connectionString: dsn, statement_timeout: timeout * 1000, query_timeout: timeout * 1000 });
    await client.connect();
    connected = true;
    await client.query("BEGIN READ ONLY");
    await client.query("SET LOCAL TIME ZONE 'UTC'");
    const boundedSql = `SELECT * FROM (${sql}) AS dlr_snapshot`;
    await client.query({
      text: `DECLARE dlr_snapshot_cursor NO SCROLL CURSOR FOR ${boundedSql}`,
      values: params,
    });
    let outputBytes = 2;
    let partial = false;
    let names = null;
    while (!partial) {
      const fetchCount = Math.min(batchSize, maxRows + 1 - rows.length);
      const result = await client.query({
        text: `FETCH FORWARD ${fetchCount} FROM dlr_snapshot_cursor`,
        rowMode: "array",
        types: JSON_SAFE_TYPES,
      });
      if (names === null) names = columnNames(result.fields);
      if (!Array.isArray(result.rows) || result.rows.length > fetchCount) {
        throw new Error("invalid_cursor_result");
      }
      for (const rawRow of result.rows) {
        if (rows.length >= maxRows) { partial = true; break; }
        if (!Array.isArray(rawRow) || rawRow.length !== names.length) {
          throw new Error("column_count_mismatch");
        }
        let normalized;
        try { normalized = rawRow.map((value) => normalizeCell(value)); }
        catch { partial = true; break; }
        if (normalized.some((value) => jsonBytes(value) > maxCell)) { partial = true; break; }
        const row = Object.fromEntries(names.map((name, at) => [name, normalized[at]]));
        const encodedBytes = jsonBytes(row) + (rows.length ? 1 : 0);
        if (outputBytes + encodedBytes > maxOutput) { partial = true; break; }
        rows.push(row); outputBytes += encodedBytes;
      }
      if (!partial && result.rows.length < fetchCount) break;
    }
    const count = rows.length;
    await client.query("ROLLBACK");
    return { rows, count, partial, checkpoint: partial ? { row_offset: count } : null };
  } catch {
    if (client !== null) {
      try { await client.query("ROLLBACK"); } catch { /* connection may already be unavailable */ }
    }
    return {
      rows, count: rows.length, partial: true,
      error: connected ? "database_query_failed" : "database_connection_failed",
    };
  } finally {
    if (client !== null) await client.end().catch(() => {});
  }

}
