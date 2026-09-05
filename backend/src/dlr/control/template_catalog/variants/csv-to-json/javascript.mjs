/** Bounded CSV to JSON conversion without Managed Input coupling. */
import fs from "node:fs";

const STABLE_ERRORS = new Set([
  "input_must_be_object", "input_too_large", "csv_content_required",
  "invalid_encoding_or_content", "invalid_delimiter", "invalid_headers", "invalid_or_duplicate_header",
  "invalid_quote", "unterminated_quote", "column_limit_exceeded",
  "field_limit_exceeded", "row_has_extra_columns",
]);

function positive(value, fallback, maximum) {
  return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback;
}

function* parseCsv(text, delimiter, maxColumns, maxField) {
  let row = [];
  let field = "";
  let fieldBytes = 0;
  let quoted = false;
  let physicalRow = 0;
  const append = (value) => {
    field += value;
    fieldBytes += Buffer.byteLength(value, "utf8");
    if (fieldBytes > maxField) throw new Error("field_limit_exceeded");
  };
  const finishField = () => {
    row.push(field);
    if (row.length > maxColumns) throw new Error("column_limit_exceeded");
    field = "";
    fieldBytes = 0;
  };
  for (let at = 0; at < text.length;) {
    const codePoint = text.codePointAt(at);
    const char = String.fromCodePoint(codePoint);
    const width = char.length;
    if (quoted) {
      if (char === '"' && text[at + 1] === '"') { append('"'); at += 2; continue; }
      else if (char === '"') quoted = false;
      else append(char);
    } else if (char === '"') {
      if (field.length !== 0) throw new Error("invalid_quote");
      quoted = true;
    } else if (char === delimiter) {
      finishField();
    } else if (char === "\n") {
      finishField();
      physicalRow += 1;
      yield { row, physicalRow };
      row = [];
    } else if (char === "\r" && text[at + width] === "\n") {
      // CR in a CRLF record terminator is not part of the field.
    } else {
      append(char);
    }
    at += width;
  }
  if (quoted) throw new Error("unterminated_quote");
  if (field.length > 0 || row.length > 0) {
    finishField();
    physicalRow += 1;
    yield { row, physicalRow };
  }
}

function validateHeaders(headers, maxColumns, maxField) {
  if (headers.length === 0 || new Set(headers).size !== headers.length
      || headers.some((item) => item.trim().length === 0)) {
    throw new Error("invalid_or_duplicate_header");
  }
  if (headers.length > maxColumns) throw new Error("column_limit_exceeded");
  if (headers.some((item) => Buffer.byteLength(item, "utf8") > maxField)) {
    throw new Error("field_limit_exceeded");
  }
}

function run(context, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("input_must_be_object");
  const maxInput = positive(input.max_input_bytes, 2_097_152, 16_777_216);
  const maxOutput = positive(input.max_output_bytes, 4_194_304, 16_777_216);
  const maxRows = positive(input.max_rows, 10_000, 100_000);
  const maxColumns = positive(input.max_columns, 200, 2_000);
  const maxField = positive(input.max_field_bytes, 65_536, 1_048_576);
  const requestedEncoding = String(input.encoding ?? "utf-8-sig").toLowerCase();
  const encoding = ["utf8", "utf-8", "utf-8-sig"].includes(requestedEncoding)
    ? "utf-8" : ["utf16le", "utf-16le"].includes(requestedEncoding) ? "utf-16le" : null;
  if (encoding === null) throw new Error("invalid_encoding_or_content");
  let bytes;
  if (typeof input.content === "string") bytes = Buffer.from(input.content, encoding);
  else if (context.inputFiles.length > 0) {
    if (context.inputFiles[0].sizeBytes > maxInput) throw new Error("input_too_large");
    bytes = fs.readFileSync(context.inputFiles[0].path);
  } else throw new Error("csv_content_required");
  if (bytes.byteLength > maxInput) throw new Error("input_too_large");
  let text;
  try { text = new TextDecoder(encoding, { fatal: true }).decode(bytes).replace(/^\uFEFF/, ""); }
  catch { throw new Error("invalid_encoding_or_content"); }
  const delimiter = input.delimiter ?? ",";
  if (typeof delimiter !== "string" || [...delimiter].length !== 1) throw new Error("invalid_delimiter");
  let headers = input.headers;
  if (headers !== undefined && (!Array.isArray(headers) || headers.some((item) => typeof item !== "string"))) {
    throw new Error("invalid_headers");
  }
  if (headers !== undefined) validateHeaders(headers, maxColumns, maxField);
  const rows = [];
  let size = 2;
  let checkpoint = null;
  for (const parsed of parseCsv(text, delimiter, maxColumns, maxField)) {
    const { row, physicalRow } = parsed;
    if (input.skip_empty !== false && row.every((cell) => cell.trim().length === 0)) continue;
    if (headers === undefined && input.header !== false) {
      headers = row;
      validateHeaders(headers, maxColumns, maxField);
      continue;
    }
    if (headers && row.length > headers.length) throw new Error("row_has_extra_columns");
    const item = headers
      ? Object.fromEntries(headers.map((name, index) => [name, row[index] ?? null]))
      : row;
    const encoded = Buffer.byteLength(JSON.stringify(item), "utf8") + (rows.length ? 1 : 0);
    if (rows.length >= maxRows || size + encoded > maxOutput) {
      checkpoint = { next_physical_row: physicalRow };
      break;
    }
    rows.push(item); size += encoded;
  }
  return {
    rows,
    count: rows.length,
    partial: checkpoint !== null,
    checkpoint,
    encoding,
    delimiter,
  };
}

export function handle(context, input) {
  try {
    return run(context, input);
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    if (STABLE_ERRORS.has(code)) throw new Error(code);
    throw new Error("csv_operation_failed");
  }
}
