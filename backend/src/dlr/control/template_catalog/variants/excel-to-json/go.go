package main

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/extrame/xls"
	"github.com/xuri/excelize/v2"
	"io"
	"os"
	"strconv"
	"strings"
)

// Excel 转 JSON：可修改的配置集中在这里。
// 运行时提供待处理的数据或文件；处理规则在下面配置。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "sheet": null,
  "range": "A1:D100",
  "header": true,
  "header_row": 1,
  "null_policy": "null",
  "max_file_bytes": 8388608,
  "max_rows": 5000,
  "max_columns": 200,
  "max_output_bytes": 4194304
}`

type object = map[string]any

func settings(input any) (object, error) {
	cfg := object{}
	if err := json.Unmarshal([]byte(configJSON), &cfg); err != nil {
		return nil, err
	}
	if input == nil {
		return cfg, nil
	}
	values, ok := input.(map[string]any)
	if !ok {
		return nil, errors.New("input_must_be_object")
	}
	for k, v := range values {
		cfg[k] = v
	}
	return cfg, nil
}
func stringValue(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}
func textValue(v object, k string, fallback string) string {
	if v[k] == nil {
		return fallback
	}
	return stringValue(v[k])
}
func number(v any) int { n, _ := strconv.Atoi(stringValue(v)); return n }
func limit(v object, k string, fallback, maximum int) int {
	n := number(v[k])
	if n <= 0 {
		return fallback
	}
	return min(n, maximum)
}
func array(v any) []any    { a, _ := v.([]any); return a }
func mapping(v any) object { m, _ := v.(map[string]any); return m }
func encoded(v any) []byte { b, _ := json.Marshal(v); return b }
func lookup(v any, p string) (any, bool) {
	if p == "" {
		return v, true
	}
	parts := strings.Split(p, ".")
	if strings.HasPrefix(p, "/") {
		parts = strings.Split(p[1:], "/")
		for i, s := range parts {
			parts[i] = strings.ReplaceAll(strings.ReplaceAll(s, "~1", "/"), "~0", "~")
		}
	}
	for _, k := range parts {
		switch c := v.(type) {
		case map[string]any:
			var ok bool
			v, ok = c[k]
			if !ok {
				return nil, false
			}
		case []any:
			i, e := strconv.Atoi(k)
			if e != nil || i < 0 || i >= len(c) {
				return nil, false
			}
			v = c[i]
		default:
			return nil, false
		}
	}
	return v, true
}
func at(v any, p string) any { r, _ := lookup(v, p); return r }

func Handle(ctx *Context, input any) (result any, err error) {
	defer func() {
		if recover() != nil {
			result = nil
			err = errors.New("excel_operation_failed")
		}
	}()
	c, e := settings(input)
	if e != nil {
		return nil, e
	}
	if len(ctx.InputFiles) == 0 {
		return nil, errors.New("excel_file_required")
	}
	file := ctx.InputFiles[0]
	f, e := os.Open(file.Path)
	if e != nil {
		return nil, errors.New("excel_file_required")
	}
	defer f.Close()
	data, e := io.ReadAll(io.LimitReader(f, int64(limit(c, "max_file_bytes", 8388608, 33554432)+1)))
	if e != nil || len(data) > limit(c, "max_file_bytes", 8388608, 33554432) {
		return nil, errors.New("file_too_large")
	}
	names := []string{}
	var cell func(int, int) (any, error)
	var closeBook func()
	formulas := 0
	legacy := strings.HasSuffix(strings.ToLower(file.OriginalName), ".xls")
	if legacy {
		book, e := xls.OpenReader(bytes.NewReader(data), "utf-8")
		if e != nil || book == nil {
			return nil, errors.New("invalid_workbook")
		}
		for i := 0; i < book.NumSheets(); i++ {
			names = append(names, book.GetSheet(i).Name)
		}
		index := 0
		if s := stringValue(c["sheet"]); s != "" {
			index = -1
			for i, n := range names {
				if n == s {
					index = i
				}
			}
		}
		if index < 0 || len(names) == 0 {
			return nil, errors.New("sheet_not_found")
		}
		sheet := book.GetSheet(index)
		cell = func(row, col int) (any, error) {
			if row > int(sheet.MaxRow)+1 {
				return nil, nil
			}
			r := sheet.Row(row - 1)
			if r == nil {
				return nil, nil
			}
			return r.Col(col - 1), nil
		}
	} else {
		archive, e := zip.NewReader(bytes.NewReader(data), int64(len(data)))
		if e != nil || len(archive.File) > 2000 {
			return nil, errors.New("invalid_workbook")
		}
		var expanded uint64
		for _, entry := range archive.File {
			expanded += entry.UncompressedSize64
			if expanded > 67108864 || entry.Flags&1 != 0 || strings.Contains(entry.Name, "../") {
				return nil, errors.New("unsafe_ooxml_archive")
			}
			lower := strings.ToLower(entry.Name)
			if strings.Contains(lower, "vbaproject") || strings.Contains(lower, "externallinks/") || strings.Contains(lower, "embeddings/") {
				return nil, errors.New("active_content_forbidden")
			}
		}
		book, e := excelize.OpenReader(bytes.NewReader(data), excelize.Options{UnzipSizeLimit: 67108864, UnzipXMLSizeLimit: 16777216})
		if e != nil {
			return nil, errors.New("invalid_workbook")
		}
		closeBook = func() { _ = book.Close() }
		defer closeBook()
		names = book.GetSheetList()
		if len(names) == 0 {
			return nil, errors.New("sheet_not_found")
		}
		sheet := textValue(c, "sheet", names[0])
		index, e := book.GetSheetIndex(sheet)
		if e != nil || index < 0 {
			return nil, errors.New("sheet_not_found")
		}
		cell = func(row, col int) (any, error) {
			address, e := excelize.CoordinatesToCellName(col, row)
			if e != nil {
				return nil, e
			}
			formula, e := book.GetCellFormula(sheet, address)
			if e != nil {
				return nil, e
			}
			if formula != "" {
				formulas++
				return nil, nil
			}
			v, e := book.GetCellValue(sheet, address)
			return v, e
		}
	}
	rawRange := strings.Split(textValue(c, "range", "A1:D100"), ":")
	if len(rawRange) != 2 {
		return nil, errors.New("invalid_range")
	}
	startCol, startRow, e := excelize.CellNameToCoordinates(rawRange[0])
	if e != nil {
		return nil, errors.New("invalid_range")
	}
	endCol, endRow, e := excelize.CellNameToCoordinates(rawRange[1])
	if e != nil || endCol < startCol || endRow < startRow {
		return nil, errors.New("invalid_range")
	}
	startRow = limit(c, "start_row", startRow, 1048576)
	startCol = limit(c, "start_column", startCol, 16384)
	lastCol := min(endCol, startCol+limit(c, "max_columns", 200, 2000)-1)
	headers := []string{}
	if c["header"] != false {
		headerRow := limit(c, "header_row", startRow, 1048576)
		seen := map[string]bool{}
		for col := startCol; col <= lastCol; col++ {
			v, e := cell(headerRow, col)
			if e != nil {
				return nil, errors.New("excel_operation_failed")
			}
			h := stringValue(v)
			if h == "" || seen[h] {
				return nil, errors.New("invalid_or_duplicate_header")
			}
			seen[h] = true
			headers = append(headers, h)
		}
		startRow = max(startRow, headerRow+1)
	}
	rows := []any{}
	size := 2
	next := startRow
	partial := lastCol < endCol
	for row := startRow; row <= endRow; row++ {
		next = row
		if len(rows) >= limit(c, "max_rows", 5000, 50000) {
			partial = true
			break
		}
		values := []any{}
		mapped := object{}
		for col := startCol; col <= lastCol; col++ {
			v, e := cell(row, col)
			if e != nil {
				return nil, errors.New("excel_operation_failed")
			}
			if v == "" && c["null_policy"] != "empty-string" {
				v = nil
			}
			values = append(values, v)
			if len(headers) > 0 {
				if !(v == nil && c["null_policy"] == "omit") {
					mapped[headers[col-startCol]] = v
				}
			}
		}
		var item any = values
		if len(headers) > 0 {
			item = mapped
		}
		addition := len(encoded(item)) + 1
		if size+addition > limit(c, "max_output_bytes", 4194304, 16777216) {
			partial = true
			break
		}
		rows = append(rows, item)
		size += addition
		next = row + 1
	}
	var checkpoint any
	if partial {
		checkpoint = object{"next_row": next, "next_column": lastCol + 1, "reasons": []string{"bounded"}}
	}
	return object{"sheets": names[:min(len(names), 100)], "rows": rows, "count": len(rows), "partial": partial, "checkpoint": checkpoint, "active_content": object{"executed": false, "formulas_replaced_with_null": formulas, "legacy_xls_data_only": legacy, "ooxml_preflight": !legacy}}, nil
}
