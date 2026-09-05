// MySQL 数据查询：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// MYSQL_DSN：MySQL 连接字符串（包含账号密码）。
const CONFIG = {
  // 填写一条 SELECT；动态值通过 params 绑定，不要拼接用户输入。
  "sql": "SELECT id, name FROM example_items WHERE updated_at >= ?",
  // 按 SQL 占位符顺序填写参数。
  "params": ["2026-01-01T00:00:00Z"],
  // 最多读取的行数。
  "max_rows": 5000,
  // 返回结果大小上限，单位字节。
  "max_output_bytes": 4194304,
  // 单个单元格大小上限，单位字节。
  "max_cell_bytes": 1048576,
  // 每批处理的记录数。
  "batch_size": 500,
  // 单次请求超时时间，单位秒。
  "timeout_seconds": 30,
};

/** Bounded read-only MySQL snapshot. */
import mysql from "mysql2";

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
function normalizeMysqlCell(value, field) {
  const type = field?.type ?? field?.columnType;
  if (typeof value === "string"
      && ([7, 12, 17, 18, "TIMESTAMP", "DATETIME"].includes(type))) {
    return value.replace(" ", "T");
  }
  return normalizeCell(value);
}
function checkedQuery(input) {
  if (typeof input.sql !== "string" || !/^\s*select\b/i.test(input.sql)
      || input.sql.includes(";")
      || /\b(insert|update|delete|merge|call|execute|create|alter|drop|truncate|copy)\b/i.test(input.sql)) {
    throw new Error("single_select_required");
  }
  return input.sql;
}
function connectionOptions(dsn) {
  try {
    const parsed = new URL(dsn);
    const database = decodeURIComponent(parsed.pathname.replace(/^\//, ""));
    const user = decodeURIComponent(parsed.username);
    const password = decodeURIComponent(parsed.password);
    const port = parsed.port ? Number(parsed.port) : 3306;
    if (parsed.protocol !== "mysql:" || !parsed.hostname || !user || !database
        || database.includes("/") || parsed.search || parsed.hash
        || !Number.isInteger(port) || port < 1 || port > 65_535) {
      throw new Error("invalid_mysql_dsn");
    }
    return {
      host: parsed.hostname,
      port,
      user,
      password,
      database,
      multipleStatements: false,
      supportBigNumbers: true,
      bigNumberStrings: true,
      dateStrings: true,
      timezone: "Z",
    };
  } catch {
    throw new Error("invalid_mysql_dsn");
  }
}
function connect(connection) {
  return new Promise((resolve, reject) => connection.connect(
    (error) => error ? reject(error) : resolve(),
  ));
}
function command(connection, sql, values = undefined) {
  return new Promise((resolve, reject) => {
    const callback = (error, result) => error ? reject(error) : resolve(result);
    if (values === undefined) connection.query(sql, callback);
    else connection.query(sql, values, callback);
  });
}
function close(connection) {
  return new Promise((resolve) => connection.end(() => resolve()));
}

export async function handle(context, input) {
  if (input === undefined || input === null) input = {};
  if (typeof input !== "object" || Array.isArray(input)) throw new Error("输入必须是 JSON 对象");
  input = { ...CONFIG, ...input };
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  const sql = checkedQuery(input);
  const params = input.params ?? [];
  if (!Array.isArray(params) || params.length > 64) throw new Error("params_must_be_array");
  const maxRows = positive(input.max_rows, 5_000, 100_000);
  const batchSize = positive(input.batch_size, 500, 5_000);
  const maxOutput = positive(input.max_output_bytes, 4_194_304, 16_777_216);
  const maxCell = positive(input.max_cell_bytes, 1_048_576, 8_388_608);
  const timeout = positive(input.timeout_seconds, 30, 300);
  // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
  const dsn = context.secrets.get("MYSQL_DSN");
  if (!dsn) throw new Error("missing_credential");
  const options = connectionOptions(dsn);
  let connection = null;
  let connected = false;
  let abortConnection = false;
  const rows = [];
  try {
    connection = mysql.createConnection({
      ...options,
      connectTimeout: Math.min(timeout, 60) * 1000,
    });
    await connect(connection);
    connected = true;
    await command(connection, "START TRANSACTION READ ONLY");
    await command(connection, "SET time_zone = '+00:00'");
    await command(connection, "SET SESSION MAX_EXECUTION_TIME=?", [timeout * 1000]);
    const boundedSql = `SELECT * FROM (${sql}) AS dlr_snapshot LIMIT ${maxRows + 1}`;
    const query = connection.query({
      sql: boundedSql,
      values: params,
      rowsAsArray: true,
      timeout: timeout * 1000,
    });
    let names = null;
    let fields = null;
    let fieldsError = null;
    query.once("fields", (value) => {
      try { names = columnNames(value); fields = value; }
      catch (error) { fieldsError = error; }
    });
    const stream = query.stream({ highWaterMark: batchSize });
    let outputBytes = 2;
    let partial = false;
    let pending = [];
    const consume = () => {
      for (const rawRow of pending) {
        if (rows.length >= maxRows) { partial = true; abortConnection = true; return false; }
        if (fieldsError !== null) throw fieldsError;
        if (names === null || fields === null || !Array.isArray(rawRow)
            || rawRow.length !== names.length) {
          throw new Error("column_count_mismatch");
        }
        let normalized;
        try { normalized = rawRow.map((value, at) => normalizeMysqlCell(value, fields[at])); }
        catch { partial = true; abortConnection = true; return false; }
        if (normalized.some((value) => jsonBytes(value) > maxCell)) {
          partial = true; abortConnection = true; return false;
        }
        const row = Object.fromEntries(names.map((name, at) => [name, normalized[at]]));
        const encodedBytes = jsonBytes(row) + (rows.length ? 1 : 0);
        if (outputBytes + encodedBytes > maxOutput) {
          partial = true; abortConnection = true; return false;
        }
        rows.push(row); outputBytes += encodedBytes;
      }
      pending = [];
      return true;
    };
    for await (const rawRow of stream) {
      pending.push(rawRow);
      if (pending.length === batchSize && !consume()) break;
    }
    if (!partial && pending.length > 0) consume();
    if (fieldsError !== null) throw fieldsError;
    if (names === null) throw new Error("missing_column_metadata");
    const count = rows.length;
    return { rows, count, partial, checkpoint: partial ? { row_offset: count } : null };
  } catch {
    return {
      rows, count: rows.length, partial: true,
      error: connected ? "database_query_failed" : "database_connection_failed",
    };
  } finally {
    if (connection !== null) {
      if (!connected || abortConnection) {
        try { connection.destroy(); } catch { /* cleanup must not replace the stable result */ }
      } else {
        await command(connection, "ROLLBACK").catch(() => {});
        await close(connection).catch(() => {});
      }
    }
  }
}
