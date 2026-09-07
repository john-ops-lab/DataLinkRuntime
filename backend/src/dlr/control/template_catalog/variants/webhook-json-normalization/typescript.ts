// TypeScript 参考模板；外部 JSON 的动态字段显式保留，Context 使用平台类型。
import type { Context } from "dlr";

// Webhook 数据整理：可修改的配置集中在这里。
// 运行时提供待处理的数据或文件；处理规则在下面配置。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
const CONFIG: Record<string, any> = {
    // 必须存在的 Webhook 字段。
    "required": ["event_id"],
    // 字段映射规则；source/pointer 指定原字段路径，target 指定结果字段名。
    "mappings": [{ "source": "event_id", "target": "id", "required": true }, { "source": "occurred_at", "target": "timestamp", "type": "datetime", "required": true }],
    // 最多处理的字段数。
    "max_fields": 200,
    // 输入大小上限，单位字节。
    "max_input_bytes": 1048576,
    // 返回结果大小上限，单位字节。
    "max_output_bytes": 2097152,
    // JSON 最大嵌套层数。
    "max_depth": 32,
};
/** Pure webhook payload validation and normalization. */
const DANGEROUS_TARGET_SEGMENTS = new Set<any>(["__proto__", "prototype", "constructor"]);
const PATH_SEGMENT = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_PATH_LENGTH = 256;
const MAX_PATH_SEGMENTS = 32;
const MIN_OUTPUT_BYTES = 128;
function validPath(path: any, { target = false }: any = {}): any {
    if (typeof path !== "string" || path.length < 1 || path.length > MAX_PATH_LENGTH)
        return false;
    const segments = path.split(".");
    return segments.length <= MAX_PATH_SEGMENTS
        && segments.every((segment: any) => PATH_SEGMENT.test(segment))
        && (!target || segments.every((segment: any) => !DANGEROUS_TARGET_SEGMENTS.has(segment)));
}
function readPath(value: any, path: any): any {
    let current = value;
    for (const segment of path.split(".")) {
        if (!segment)
            throw new Error("invalid_path");
        if (current && typeof current === "object" && !Array.isArray(current)
            && Object.hasOwn(current, segment))
            current = current[segment];
        else
            throw new Error("missing_path");
    }
    return current;
}
function utc(value: any): any {
    if (typeof value !== "string" || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) {
        throw new Error("timestamp_requires_timezone");
    }
    const calendar = /^(\d{4})-(\d{2})-(\d{2})[T ]/.exec(value);
    if (calendar === null)
        throw new Error("invalid_timestamp");
    const year = Number(calendar[1]);
    const month = Number(calendar[2]);
    const day = Number(calendar[3]);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const monthDays: any[] = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (month < 1 || month > 12 || day < 1 || day > monthDays[month - 1]) {
        throw new Error("invalid_timestamp");
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime()))
        throw new Error("invalid_timestamp");
    return parsed.toISOString();
}
function assign(target: any, path: any, value: any): any {
    const segments = path.split(".");
    if (!validPath(path, { target: true })) {
        throw new Error("invalid_target_path");
    }
    let current = target;
    for (const segment of segments.slice(0, -1)) {
        current[segment] ??= Object.create(null);
        if (!current[segment] || typeof current[segment] !== "object"
            || Array.isArray(current[segment]))
            throw new Error("target_path_conflict");
        current = current[segment];
    }
    current[segments.at(-1)] = value;
}
function exceedsDepth(value: any, maximum: any): any {
    const stack: any[] = [[value, 0]];
    while (stack.length > 0) {
        const [current, parentDepth] = stack.pop();
        if (!current || typeof current !== "object")
            continue;
        const currentDepth = parentDepth + 1;
        if (currentDepth > maximum)
            return true;
        const values = Array.isArray(current) ? current : Object.values(current);
        for (const child of values)
            stack.push([child, currentDepth]);
    }
    return false;
}
function serializedSize(value: any): any {
    try {
        return Buffer.byteLength(JSON.stringify(value), "utf8");
    }
    catch {
        throw new Error("payload_too_large");
    }
}
function boundedResult(errors: any, normalized: any, maxFields: any, maxOutputBytes: any): any {
    if (errors.length === 0) {
        const result: Record<string, any> = { valid: true, data: normalized, errors: [], partial: false };
        if (serializedSize(result) <= maxOutputBytes)
            return result;
        errors = [{ field: "", code: "output_limit" }];
    }
    const selected: any[] = [];
    for (const error of errors.slice(0, maxFields)) {
        const candidate: Record<string, any> = {
            valid: false, data: null, errors: [...selected, error], partial: true,
        };
        if (serializedSize(candidate) > maxOutputBytes)
            break;
        selected.push(error);
    }
    let partial = selected.length < errors.length;
    let result: Record<string, any> = { valid: false, data: null, errors: selected, partial };
    if (!partial && serializedSize(result) > maxOutputBytes) {
        selected.pop();
        partial = true;
        result = { valid: false, data: null, errors: selected, partial };
    }
    return result;
}
function boundedInteger(input: any, key: any, fallback: any, minimum: any, maximum: any, code: any): any {
    if (!Object.hasOwn(input, key))
        return fallback;
    const value = input[key];
    if (!Number.isInteger(value) || value < minimum || value > maximum)
        throw new Error(code);
    return value;
}
export function handle(context: Context, input: any): any {
    if (input === undefined || input === null)
        input = {};
    if (typeof input !== "object" || Array.isArray(input))
        throw new Error("输入必须是 JSON 对象");
    input = { ...CONFIG, ...input };
    if (!input || typeof input !== "object" || Array.isArray(input))
        throw new Error("input_must_be_object");
    const payload = input.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload))
        throw new Error("payload_must_be_object");
    const required = input.required ?? [];
    const mappings = input.mappings ?? [];
    const maxFields = boundedInteger(input, "max_fields", 200, 1, 1000, "invalid_max_fields");
    const maxInputBytes = boundedInteger(input, "max_input_bytes", 1048576, 1, 8388608, "invalid_max_input_bytes");
    const maxOutputBytes = boundedInteger(input, "max_output_bytes", 2097152, MIN_OUTPUT_BYTES, 8388608, "invalid_max_output_bytes");
    const maxDepth = boundedInteger(input, "max_depth", 32, 1, 64, "invalid_max_depth");
    if (!Array.isArray(required) || required.length > maxFields
        || required.some((path: any) => !validPath(path)))
        throw new Error("invalid_required");
    if (!Array.isArray(mappings) || mappings.length > maxFields)
        throw new Error("invalid_mappings");
    if (exceedsDepth(payload, maxDepth))
        throw new Error("payload_too_deep");
    if (serializedSize(payload) > maxInputBytes)
        throw new Error("payload_too_large");
    const errors: any[] = [];
    for (const [index, path] of required.entries()) {
        try {
            if (readPath(payload, path) === null)
                throw new Error("missing_path");
        }
        catch {
            errors.push({ field: `required[${index}]`, code: "required" });
        }
    }
    const normalized = Object.create(null);
    let normalizedBudget = 2;
    let assignedMappings = 0;
    const validResultOverhead = serializedSize({
        valid: true, data: {}, errors: [], partial: false,
    }) - 2;
    for (const [index, mapping] of mappings.entries()) {
        const field = `mappings[${index}]`;
        if (!mapping || typeof mapping !== "object" || Array.isArray(mapping)
            || !validPath(mapping.source) || !validPath(mapping.target, { target: true })
            || (Object.hasOwn(mapping, "required") && typeof mapping.required !== "boolean")
            || ![undefined, null, "datetime"].includes(mapping.type)) {
            errors.push({ field, code: "invalid_mapping" });
            continue;
        }
        let value: any;
        try {
            value = readPath(payload, mapping.source);
        }
        catch (error: any) {
            if (error instanceof Error && error.message === "missing_path"
                && Object.hasOwn(mapping, "default")) {
                value = mapping.default;
            }
            else if (mapping.required || !(error instanceof Error) || error.message !== "missing_path") {
                errors.push({
                    field: mapping.required ? `${field}.source` : field,
                    code: error instanceof Error && error.message === "missing_path" ? "missing" : "invalid_value",
                });
                continue;
            }
            else {
                continue;
            }
        }
        try {
            if (mapping.type === "datetime")
                value = utc(value);
            const candidate = Object.create(null);
            assign(candidate, mapping.target, value);
            const addition = serializedSize(candidate) - 2 + (assignedMappings === 0 ? 0 : 1);
            if (validResultOverhead + normalizedBudget + addition > maxOutputBytes) {
                errors.push({ field: "", code: "output_limit" });
                break;
            }
            assign(normalized, mapping.target, value);
            normalizedBudget += addition;
            assignedMappings += 1;
        }
        catch {
            errors.push({ field, code: "invalid_value" });
        }
    }
    return boundedResult(errors, normalized, maxFields, maxOutputBytes);
}
