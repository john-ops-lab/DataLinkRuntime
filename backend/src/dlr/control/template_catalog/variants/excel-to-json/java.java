import java.io.InputStream;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Enumeration;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipException;
import java.util.zip.ZipFile;
import org.apache.poi.ss.usermodel.Cell;
import org.apache.poi.ss.usermodel.CellType;
import org.apache.poi.ss.usermodel.DataFormatter;
import org.apache.poi.ss.usermodel.DateUtil;
import org.apache.poi.ss.usermodel.Row;
import org.apache.poi.ss.usermodel.Sheet;
import org.apache.poi.ss.usermodel.Workbook;
import org.apache.poi.ss.usermodel.WorkbookFactory;
import org.apache.poi.xssf.usermodel.XSSFWorkbook;
import org.apache.poi.ss.util.CellRangeAddress;

/** Bounded XLSX/XLS data reader that never evaluates formulas or active content. */
public class Adapter {
    // Excel 转 JSON：可修改的配置集中在这里。
    // 运行时提供待处理的数据或文件；处理规则在下面配置。
    // 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
    private static final Map<String, Object> CONFIG = defaultConfig();
    @SuppressWarnings("unchecked")
    private static Map<String, Object> defaultConfig() {
        // 参数说明与下方 JSON 使用相同顺序。
        // sheet: 工作表名称；null 使用第一个工作表。
        // range: 需要读取的单元格范围，例如 A1:D100。
        // header: 是否把首行作为字段名。
        // header_row: 作为表头的行号，从 1 开始。
        // null_policy: 空单元格处理：null 保留空值、empty-string 转为空字符串、omit 忽略空字段。
        // max_file_bytes: 单个文件读取大小上限，单位字节。
        // max_rows: 最多读取的行数。
        // max_columns: 最多读取的列数。
        // max_output_bytes: 返回结果大小上限，单位字节。
        return (Map<String, Object>) Json.parse("""
            {
              "sheet": null,
              "range": "A1:D100",
              "header": true,
              "header_row": 1,
              "null_policy": "null",
              "max_file_bytes": 8388608,
              "max_rows": 5000,
              "max_columns": 200,
              "max_output_bytes": 4194304
            }
            """);
    }

    public Object handle(Context context, Object rawInput) throws Exception {
        if (rawInput == null) rawInput = Map.of();
        if (!(rawInput instanceof Map<?, ?>)) throw new IllegalArgumentException("输入必须是 JSON 对象");
        Map<String, Object> configuredInput = new java.util.LinkedHashMap<>(CONFIG);
        for (Map.Entry<?, ?> entry : ((Map<?, ?>) rawInput).entrySet()) {
            configuredInput.put(String.valueOf(entry.getKey()), entry.getValue());
        }
        rawInput = configuredInput;
        try {
            return run(context, rawInput);
        } catch (Exception error) {
            String code = error.getMessage();
            if (code != null && STABLE_ERRORS.contains(code)) {
                throw new IllegalArgumentException(code);
            }
            throw new IllegalArgumentException("excel_operation_failed");
        }
    }

    private Object run(Context context, Object rawInput) throws Exception {
        Map<String, Object> input = object(rawInput);
        if (context.inputFiles.isEmpty()) throw new IllegalArgumentException("workbook_input_file_required");
        InputFile item = context.inputFiles.get(0);
        int maxFile = positive(input.get("max_file_bytes"), 8_388_608, 33_554_432);
        int maxRows = positive(input.get("max_rows"), 5_000, 50_000);
        int maxColumns = positive(input.get("max_columns"), 200, 2_000);
        int maxOutput = positive(input.get("max_output_bytes"), 4_194_304, 16_777_216);
        if (item.sizeBytes > maxFile || Files.size(item.path) > maxFile) {
            throw new IllegalArgumentException("workbook_too_large");
        }
        String name = item.originalName.toLowerCase(Locale.ROOT);
        if (name.endsWith(".xlsm") || name.endsWith(".xltm") || name.endsWith(".xlam")) {
            throw new IllegalArgumentException("macro_enabled_workbook_rejected");
        }
        boolean legacyXls = name.endsWith(".xls");
        if (!name.endsWith(".xlsx") && !legacyXls) {
            throw new IllegalArgumentException("unsupported_workbook_format");
        }
        if (!legacyXls) inspectXlsx(item.path);
        InputStream stream = null;
        Workbook workbook = null;
        try {
            stream = Files.newInputStream(item.path);
            workbook = WorkbookFactory.create(stream, null);
            if (workbook instanceof XSSFWorkbook xlsx && !xlsx.getExternalLinksTable().isEmpty()) {
                throw new IllegalArgumentException("workbook_external_links_rejected");
            }
            List<String> sheetNames = new ArrayList<>();
            for (int at = 0; at < Math.min(workbook.getNumberOfSheets(), 100); at++) {
                sheetNames.add(workbook.getSheetName(at));
            }
            String selected = input.get("sheet") instanceof String value && !value.isBlank()
                ? value : workbook.getSheetName(0);
            Sheet sheet = workbook.getSheet(selected);
            if (sheet == null) throw new IllegalArgumentException("sheet_not_found");
            int sheetLastColumn = 0;
            for (Row row : sheet) {
                sheetLastColumn = Math.max(sheetLastColumn, Math.max(0, row.getLastCellNum() - 1));
            }
            CellRangeAddress selectedRange;
            try {
                Object rawRange = input.get("range");
                if (rawRange != null && (!(rawRange instanceof String range)
                    || !range.matches("(?i)^[A-Z]+[1-9][0-9]*:[A-Z]+[1-9][0-9]*$"))) {
                    throw new IllegalArgumentException("invalid_range");
                }
                selectedRange = rawRange instanceof String range
                    ? CellRangeAddress.valueOf(range)
                    : new CellRangeAddress(0, sheet.getLastRowNum(), 0, sheetLastColumn);
                if (selectedRange.getFirstRow() < 0 || selectedRange.getFirstColumn() < 0
                    || selectedRange.getFirstRow() > selectedRange.getLastRow()
                    || selectedRange.getFirstColumn() > selectedRange.getLastColumn()
                    || selectedRange.getLastRow() >= 1_048_576
                    || selectedRange.getLastColumn() >= 16_384) {
                    throw new IllegalArgumentException("invalid_range");
                }
            } catch (RuntimeException error) {
                throw new IllegalArgumentException("invalid_range");
            }
            int startRow = positive(input.get("start_row"), selectedRange.getFirstRow() + 1, 1_048_576) - 1;
            int startColumn = positive(input.get("start_column"), selectedRange.getFirstColumn() + 1, 16_384) - 1;
            if (startRow > selectedRange.getLastRow() || startColumn > selectedRange.getLastColumn()) {
                throw new IllegalArgumentException("range_start_outside_selection");
            }
            if (!Boolean.FALSE.equals(input.get("header")) && input.get("header_row") != null) {
                int headerRow = positive(input.get("header_row"), startRow + 1, 1_048_576) - 1;
                if (headerRow < startRow || headerRow > selectedRange.getLastRow()) {
                    throw new IllegalArgumentException("header_row_outside_range");
                }
                startRow = headerRow;
            }
            int endColumn = Math.min(selectedRange.getLastColumn(), startColumn + maxColumns - 1);
            String nullPolicy = String.valueOf(input.getOrDefault("null_policy", "null"));
            if (!List.of("null", "empty-string", "omit").contains(nullPolicy)) {
                throw new IllegalArgumentException("invalid_null_policy");
            }
            DataFormatter formatter = new DataFormatter(Locale.ROOT);
            boolean formulas = false;
            boolean columnLimited = selectedRange.getLastColumn() > endColumn;
            boolean useHeader = !Boolean.FALSE.equals(input.get("header"));
            List<String> headers = null;
            int maximumRow = Math.min(selectedRange.getLastRow(), sheet.getLastRowNum());
            if (useHeader && startRow <= maximumRow) {
                RowValue header = readRow(sheet, startRow, startColumn, endColumn, formatter);
                formulas = header.formulas();
                headers = new ArrayList<>();
                Set<String> unique = new java.util.HashSet<>();
                for (int at = 0; at < header.values().size(); at++) {
                    Object value = header.values().get(at);
                    if (value instanceof Map<?, ?> || value instanceof List<?>) {
                        throw new IllegalArgumentException("invalid_or_duplicate_header");
                    }
                    headers.add(value == null || String.valueOf(value).isEmpty()
                        ? "column_" + (at + 1) : String.valueOf(value));
                    if (!unique.add(headers.get(at))) {
                        throw new IllegalArgumentException("invalid_or_duplicate_header");
                    }
                }
            }
            List<Object> rows = new ArrayList<>();
            int outputBytes = 2;
            List<String> reasons = new ArrayList<>();
            if (columnLimited) reasons.add("column_limit");
            Integer nextRow = null;
            int dataStart = startRow + (headers == null ? 0 : 1);
            for (int rowAt = dataStart; rowAt <= maximumRow; rowAt++) {
                if (rows.size() >= maxRows) {
                    reasons.add(0, "row_limit");
                    nextRow = rowAt + 1;
                    break;
                }
                RowValue read = readRow(sheet, rowAt, startColumn, endColumn, formatter);
                formulas = formulas || read.formulas();
                List<Object> raw = read.values();
                Object value;
                if (headers == null) {
                    value = "empty-string".equals(nullPolicy)
                        ? raw.stream().map(cell -> cell == null ? "" : cell).toList()
                        : raw;
                } else {
                    Map<String, Object> mapped = new LinkedHashMap<>();
                    for (int column = 0; column < headers.size(); column++) {
                        Object cell = column < raw.size() ? raw.get(column) : null;
                        if (cell == null && "omit".equals(nullPolicy)) continue;
                        mapped.put(headers.get(column), cell == null && "empty-string".equals(nullPolicy) ? "" : cell);
                    }
                    value = mapped;
                }
                int encodedBytes = Json.stringify(value)
                    .getBytes(java.nio.charset.StandardCharsets.UTF_8).length
                    + (rows.isEmpty() ? 0 : 1);
                if (outputBytes + encodedBytes > maxOutput) {
                    reasons.add(0, "output_limit");
                    nextRow = rowAt + 1;
                    break;
                }
                rows.add(value);
                outputBytes += encodedBytes;
            }
            Map<String, Object> checkpoint = checkpoint(
                reasons,
                nextRow,
                columnLimited ? endColumn + 2 : null
            );
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("sheets", sheetNames); result.put("rows", rows); result.put("count", rows.size());
            result.put("partial", checkpoint != null);
            result.put("checkpoint", checkpoint);
            result.put("active_content", Map.of(
                "executed", false,
                "formulas_replaced_with_null", formulas,
                "legacy_xls_data_only", legacyXls,
                "ooxml_preflight", !legacyXls
            ));
            return result;
        } finally {
            closeSafely(workbook);
            closeSafely(stream);
        }
    }

    private static final Set<String> STABLE_ERRORS = Set.of(
        "input_must_be_object", "workbook_input_file_required", "workbook_too_large",
        "macro_enabled_workbook_rejected", "unsupported_workbook_format",
        "workbook_archive_limit", "encrypted_workbook_rejected",
        "workbook_external_links_rejected", "workbook_active_content_rejected",
        "workbook_macros_rejected", "invalid_xlsx_package",
        "unsupported_xlsx_compression", "sheet_not_found", "invalid_range",
        "range_start_outside_selection", "header_row_outside_range", "invalid_null_policy",
        "invalid_or_duplicate_header", "unsupported_cell_type"
    );

    private static void closeSafely(AutoCloseable resource) {
        if (resource == null) return;
        try { resource.close(); }
        catch (Exception ignored) { /* cleanup must not replace the stable result */ }
    }

    private static void inspectXlsx(java.nio.file.Path path) throws Exception {
        try (ZipFile archive = new ZipFile(path.toFile())) {
            if (archive.size() > 10_000) throw new IllegalArgumentException("workbook_archive_limit");
            long expanded = 0;
            long relationshipBytes = 0;
            Enumeration<? extends ZipEntry> entries = archive.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                String name = entry.getName().replace('\\', '/').toLowerCase(Locale.ROOT);
                long size = entry.getSize();
                expanded += Math.max(0, size);
                if (size < 0 || size > 67_108_864 || expanded > 134_217_728) {
                    throw new IllegalArgumentException("workbook_archive_limit");
                }
                if (name.matches(".*(?:^|/)(?:vbaproject\\.bin|activex/|embeddings/|externallinks/|connections\\.xml|macrosheets/|dialogsheets/|customui/).*$")) {
                    if (name.contains("externallinks/") || name.endsWith("connections.xml")) {
                        throw new IllegalArgumentException("workbook_external_links_rejected");
                    }
                    throw new IllegalArgumentException("workbook_active_content_rejected");
                }
                if (name.endsWith(".rels") || "[content_types].xml".equals(name)) {
                    byte[] content;
                    try (InputStream stream = archive.getInputStream(entry)) {
                        content = stream.readNBytes(1_048_577);
                    }
                    relationshipBytes += content.length;
                    if (content.length > 1_048_576 || relationshipBytes > 4_194_304) {
                        throw new IllegalArgumentException("workbook_archive_limit");
                    }
                    String lowered = new String(content, java.nio.charset.StandardCharsets.UTF_8)
                        .toLowerCase(Locale.ROOT);
                    if (lowered.matches("(?s).*targetmode\\s*=\\s*['\"]external.*")) {
                        throw new IllegalArgumentException("workbook_external_links_rejected");
                    }
                    if (List.of("vbaproject", "macroenabled", "activex", "oleobject").stream()
                        .anyMatch(lowered::contains)) {
                        throw new IllegalArgumentException("workbook_active_content_rejected");
                    }
                }
            }
        } catch (ZipException error) {
            throw new IllegalArgumentException("invalid_xlsx_package");
        }
    }

    private record RowValue(List<Object> values, boolean formulas) {}

    private static RowValue readRow(
        Sheet sheet, int rowAt, int startColumn, int endColumn, DataFormatter formatter
    ) {
        Row row = sheet.getRow(rowAt);
        List<Object> values = new ArrayList<>();
        boolean formulas = false;
        for (int column = startColumn; column <= endColumn; column++) {
            Cell cell = row == null
                ? null : row.getCell(column, Row.MissingCellPolicy.RETURN_BLANK_AS_NULL);
            if (cell == null) values.add(null);
            else if (cell.getCellType() == CellType.FORMULA) {
                formulas = true;
                values.add(null);
            } else values.add(cellValue(cell, formatter));
        }
        return new RowValue(values, formulas);
    }

    private static Map<String, Object> checkpoint(
        List<String> reasons, Integer nextRow, Integer nextColumn
    ) {
        if (reasons.isEmpty()) return null;
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("reason", reasons.size() == 1 ? reasons.get(0) : "multiple_limits");
        if (reasons.size() > 1) result.put("limits", List.copyOf(reasons));
        if (nextRow != null) result.put("next_row", nextRow);
        if (nextColumn != null) result.put("next_column", nextColumn);
        return result;
    }

    private static Object cellValue(Cell cell, DataFormatter formatter) {
        return switch (cell.getCellType()) {
            case BOOLEAN -> cell.getBooleanCellValue();
            case NUMERIC -> {
                if (DateUtil.isCellDateFormatted(cell)) {
                    yield cell.getLocalDateTimeCellValue().format(
                        java.time.format.DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSS")
                    );
                }
                double value = cell.getNumericCellValue();
                if (!Double.isFinite(value)) {
                    throw new IllegalArgumentException("unsupported_cell_type");
                }
                yield value;
            }
            case STRING -> cell.getStringCellValue();
            case BLANK -> null;
            default -> formatter.formatCellValue(cell);
        };
    }
    private static int positive(Object raw, int fallback, int maximum) { return raw instanceof Number number && number.intValue() > 0 ? Math.min(number.intValue(), maximum) : fallback; }
    @SuppressWarnings("unchecked") private static Map<String, Object> object(Object raw) { if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException("input_must_be_object"); return (Map<String, Object>) value; }
}
