// TypeScript 参考模板；外部 JSON 的动态字段显式保留，Context 使用平台类型。
import type { Context } from "dlr";

// Excel 转 JSON：可修改的配置集中在这里。
// 运行时提供待处理的数据或文件；处理规则在下面配置。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
const CONFIG: Record<string, any> = {
    // 工作表名称；null 使用第一个工作表。
    "sheet": null,
    // 需要读取的单元格范围，例如 A1:D100。
    "range": "A1:D100",
    // 是否把首行作为字段名。
    "header": true,
    // 作为表头的行号，从 1 开始。
    "header_row": 1,
    // 空单元格处理：null 保留空值、empty-string 转为空字符串、omit 忽略空字段。
    "null_policy": "null",
    // 单个文件读取大小上限，单位字节。
    "max_file_bytes": 8388608,
    // 最多读取的行数。
    "max_rows": 5000,
    // 最多读取的列数。
    "max_columns": 200,
    // 返回结果大小上限，单位字节。
    "max_output_bytes": 4194304,
};
/** Bounded XLSX/XLS data reader that never evaluates formulas or active content. */
import fs from "node:fs";
import zlib from "node:zlib";
import * as XLSX from "@e965/xlsx";
const STABLE_ERRORS = new Set<any>([
    "input_must_be_object", "workbook_input_file_required", "workbook_too_large",
    "macro_enabled_workbook_rejected", "unsupported_workbook_format",
    "workbook_archive_limit", "encrypted_workbook_rejected",
    "workbook_external_links_rejected", "workbook_active_content_rejected",
    "workbook_macros_rejected", "invalid_xlsx_package",
    "unsupported_xlsx_compression", "sheet_not_found", "invalid_range",
    "range_start_outside_selection", "header_row_outside_range", "invalid_null_policy",
    "invalid_or_duplicate_header", "unsupported_cell_type",
]);
const A1_RANGE = /^([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)$/i;
function positive(value: any, fallback: any, maximum: any): any {
    return Number.isInteger(value) && value > 0 ? Math.min(value, maximum) : fallback;
}
function normalizeCell(value: any): any {
    if (value === null || typeof value === "string" || typeof value === "boolean")
        return value;
    if (typeof value === "number") {
        if (!Number.isFinite(value))
            throw new Error("unsupported_cell_type");
        return value;
    }
    if (value instanceof Date) {
        if (Number.isNaN(value.getTime()))
            throw new Error("unsupported_cell_type");
        return value.toISOString().replace(/Z$/, "");
    }
    if (Buffer.isBuffer(value))
        return { $binary_base64: value.toString("base64") };
    if (ArrayBuffer.isView(value)) {
        return {
            $binary_base64: Buffer.from(value.buffer, value.byteOffset, value.byteLength).toString("base64"),
        };
    }
    throw new Error("unsupported_cell_type");
}
function checkpoint(reasons: any, nextRow: any, nextColumn: any): any {
    if (reasons.length === 0)
        return null;
    const result: Record<string, any> = { reason: reasons.length === 1 ? reasons[0] : "multiple_limits" };
    if (reasons.length > 1)
        result.limits = reasons;
    if (nextRow !== null)
        result.next_row = nextRow;
    if (nextColumn !== null)
        result.next_column = nextColumn;
    return result;
}
function zipEntries(bytes: any): any {
    let end = -1;
    for (let at = bytes.length - 22; at >= Math.max(0, bytes.length - 65557); at -= 1) {
        if (bytes.readUInt32LE(at) === 0x06054b50) {
            end = at;
            break;
        }
    }
    if (end < 0)
        throw new Error("invalid_xlsx_package");
    const count = bytes.readUInt16LE(end + 10);
    const directorySize = bytes.readUInt32LE(end + 12);
    const directoryOffset = bytes.readUInt32LE(end + 16);
    if (count > 10000 || directoryOffset + directorySize > bytes.length) {
        throw new Error("workbook_archive_limit");
    }
    const entries: any[] = [];
    let at = directoryOffset;
    let expanded = 0;
    for (let index = 0; index < count; index += 1) {
        if (at + 46 > bytes.length || bytes.readUInt32LE(at) !== 0x02014b50) {
            throw new Error("invalid_xlsx_package");
        }
        const flags = bytes.readUInt16LE(at + 8);
        const method = bytes.readUInt16LE(at + 10);
        const compressedSize = bytes.readUInt32LE(at + 20);
        const size = bytes.readUInt32LE(at + 24);
        const nameLength = bytes.readUInt16LE(at + 28);
        const extraLength = bytes.readUInt16LE(at + 30);
        const commentLength = bytes.readUInt16LE(at + 32);
        const localOffset = bytes.readUInt32LE(at + 42);
        const next = at + 46 + nameLength + extraLength + commentLength;
        if (next > bytes.length || flags & 1)
            throw new Error("encrypted_workbook_rejected");
        const name = bytes.subarray(at + 46, at + 46 + nameLength).toString("utf8").replaceAll("\\", "/");
        expanded += size;
        if (size > 67108864 || expanded > 134217728)
            throw new Error("workbook_archive_limit");
        entries.push({ name, method, compressedSize, size, localOffset });
        at = next;
    }
    return entries;
}
function entryBytes(archive: any, entry: any): any {
    const at = entry.localOffset;
    if (at + 30 > archive.length || archive.readUInt32LE(at) !== 0x04034b50) {
        throw new Error("invalid_xlsx_package");
    }
    const nameLength = archive.readUInt16LE(at + 26);
    const extraLength = archive.readUInt16LE(at + 28);
    const start = at + 30 + nameLength + extraLength;
    const end = start + entry.compressedSize;
    if (end > archive.length || entry.size > 1048576)
        throw new Error("workbook_archive_limit");
    const compressed = archive.subarray(start, end);
    try {
        if (entry.method === 0)
            return compressed;
        if (entry.method === 8)
            return zlib.inflateRawSync(compressed, { maxOutputLength: 1048577 });
    }
    catch {
        throw new Error("workbook_archive_limit");
    }
    throw new Error("unsupported_xlsx_compression");
}
function inspectXlsx(bytes: any): any {
    const entries = zipEntries(bytes);
    let relationshipBytes = 0;
    for (const entry of entries) {
        const name = entry.name.toLowerCase();
        if (/(?:^|\/)(?:vbaproject\.bin|activex\/|embeddings\/|externallinks\/|connections\.xml|macrosheets\/|dialogsheets\/|customui\/)/i.test(name)) {
            if (name.includes("externallinks/") || name.endsWith("connections.xml")) {
                throw new Error("workbook_external_links_rejected");
            }
            throw new Error("workbook_active_content_rejected");
        }
        if (name.endsWith(".rels") || name === "[content_types].xml") {
            const content = entryBytes(bytes, entry);
            relationshipBytes += content.length;
            if (relationshipBytes > 4194304)
                throw new Error("workbook_archive_limit");
            const text = content.toString("utf8").toLowerCase();
            if (/targetmode\s*=\s*["']external/.test(text)) {
                throw new Error("workbook_external_links_rejected");
            }
            if (["vbaproject", "macroenabled", "activex", "oleobject"].some((marker: any) => text.includes(marker))) {
                throw new Error("workbook_active_content_rejected");
            }
        }
    }
}
function run(context: Context, input: any): any {
    if (!input || typeof input !== "object" || Array.isArray(input))
        throw new Error("input_must_be_object");
    if (context.inputFiles.length === 0)
        throw new Error("workbook_input_file_required");
    const item = context.inputFiles[0];
    const maxFile = positive(input.max_file_bytes, 8388608, 33554432);
    const maxRows = positive(input.max_rows, 5000, 50000);
    const maxColumns = positive(input.max_columns, 200, 2000);
    const maxOutput = positive(input.max_output_bytes, 4194304, 16777216);
    if (item.sizeBytes > maxFile)
        throw new Error("workbook_too_large");
    const suffix = item.originalName.toLowerCase().match(/\.[^.]+$/)?.[0] ?? "";
    if ([".xlsm", ".xltm", ".xlam"].includes(suffix))
        throw new Error("macro_enabled_workbook_rejected");
    if (![".xlsx", ".xls"].includes(suffix))
        throw new Error("unsupported_workbook_format");
    const workbookBytes = fs.readFileSync(item.path);
    if (workbookBytes.length > maxFile)
        throw new Error("workbook_too_large");
    if (suffix === ".xlsx")
        inspectXlsx(workbookBytes);
    const workbook = XLSX.read(workbookBytes, {
        type: "buffer",
        cellDates: true,
        cellFormula: true,
        cellHTML: false,
        cellNF: false,
        cellStyles: false,
        bookVBA: true,
        bookFiles: true,
        dense: false,
    });
    if (workbook.vbaraw)
        throw new Error("workbook_macros_rejected");
    if (Object.keys((workbook as XLSX.WorkBook & { files?: Record<string, unknown> }).files ?? {}).some((name: any) => /externalLinks|connections\.xml/i.test(name))) {
        throw new Error("workbook_external_links_rejected");
    }
    const sheetName = typeof input.sheet === "string" && input.sheet
        ? input.sheet : workbook.SheetNames[0];
    const sheet = workbook.Sheets[sheetName];
    if (!sheet)
        throw new Error("sheet_not_found");
    let rawRange = input.range ?? sheet["!ref"] ?? "A1:A1";
    if (input.range === undefined && /^[A-Z]+[1-9][0-9]*$/i.test(rawRange)) {
        rawRange = `${rawRange}:${rawRange}`;
    }
    if (typeof rawRange !== "string" || !A1_RANGE.test(rawRange))
        throw new Error("invalid_range");
    let range: any;
    try {
        range = XLSX.utils.decode_range(rawRange);
    }
    catch {
        throw new Error("invalid_range");
    }
    if (range.s.r < 0 || range.s.c < 0 || range.s.r > range.e.r || range.s.c > range.e.c
        || range.e.r >= 1048576 || range.e.c >= 16384) {
        throw new Error("invalid_range");
    }
    let physicalEndRow = range.e.r;
    if (input.range !== undefined) {
        let sheetReference = sheet["!ref"] ?? "A1:A1";
        if (/^[A-Z]+[1-9][0-9]*$/i.test(sheetReference)) {
            sheetReference = `${sheetReference}:${sheetReference}`;
        }
        try {
            physicalEndRow = Math.min(physicalEndRow, XLSX.utils.decode_range(sheetReference).e.r);
        }
        catch {
            throw new Error("invalid_range");
        }
    }
    let startRow = positive(input.start_row, range.s.r + 1, 1048576) - 1;
    const startColumn = positive(input.start_column, range.s.c + 1, 16384) - 1;
    if (startRow > range.e.r || startColumn > range.e.c)
        throw new Error("range_start_outside_selection");
    if (input.header !== false && input.header_row !== undefined) {
        const headerRow = positive(input.header_row, startRow + 1, 1048576) - 1;
        if (headerRow < startRow || headerRow > range.e.r)
            throw new Error("header_row_outside_range");
        startRow = headerRow;
    }
    const endColumn = Math.min(range.e.c, startColumn + maxColumns - 1);
    const nullPolicy = input.null_policy ?? "null";
    if (!["null", "empty-string", "omit"].includes(nullPolicy))
        throw new Error("invalid_null_policy");
    let formulas = false;
    const readRow = (row: any) => {
        const values: any[] = [];
        for (let column = startColumn; column <= endColumn; column += 1) {
            const cell = sheet[XLSX.utils.encode_cell({ r: row, c: column })];
            if (cell?.f) {
                formulas = true;
                values.push(null);
            }
            else
                values.push(normalizeCell(cell?.v ?? null));
        }
        return values;
    };
    const useHeader = input.header !== false;
    let headers: any = null;
    if (useHeader && startRow <= physicalEndRow) {
        headers = readRow(startRow).map((value: any, at: any) => {
            if (value !== null && typeof value === "object")
                throw new Error("invalid_or_duplicate_header");
            return value === null || value === "" ? `column_${at + 1}` : String(value);
        });
        if (new Set<any>(headers).size !== headers.length)
            throw new Error("invalid_or_duplicate_header");
    }
    const rows: any[] = [];
    let outputBytes = 2;
    const reasons = range.e.c > endColumn ? ["column_limit"] : [];
    let nextRow: any = null;
    const dataStart = startRow + (headers === null ? 0 : 1);
    for (let row = dataStart; row <= physicalEndRow; row += 1) {
        if (rows.length >= maxRows) {
            reasons.unshift("row_limit");
            nextRow = row + 1;
            break;
        }
        const raw = readRow(row);
        const normalized = raw.map((value: any) => value === null && nullPolicy === "empty-string" ? "" : value);
        const value = headers
            ? Object.fromEntries(headers.flatMap((name: any, at: any) => nullPolicy === "omit" && normalized[at] === null ? [] : [[name, normalized[at] ?? null]]))
            : normalized;
        const encodedBytes = Buffer.byteLength(JSON.stringify(value), "utf8") + (rows.length ? 1 : 0);
        if (outputBytes + encodedBytes > maxOutput) {
            reasons.unshift("output_limit");
            nextRow = row + 1;
            break;
        }
        rows.push(value);
        outputBytes += encodedBytes;
    }
    const cursor = checkpoint(reasons, nextRow, range.e.c > endColumn ? endColumn + 2 : null);
    return {
        sheets: workbook.SheetNames.slice(0, 100),
        rows,
        count: rows.length,
        partial: cursor !== null,
        checkpoint: cursor,
        active_content: {
            executed: false,
            formulas_replaced_with_null: formulas,
            legacy_xls_data_only: suffix === ".xls",
            ooxml_preflight: suffix === ".xlsx",
        },
    };
}
export function handle(context: Context, input: any): any {
    if (input === undefined || input === null)
        input = {};
    if (typeof input !== "object" || Array.isArray(input))
        throw new Error("输入必须是 JSON 对象");
    input = { ...CONFIG, ...input };
    try {
        return run(context, input);
    }
    catch (error: any) {
        const code = error instanceof Error ? error.message : "";
        if (STABLE_ERRORS.has(code))
            throw new Error(code);
        throw new Error("excel_operation_failed");
    }
}
