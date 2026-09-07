package main

import (
	"encoding/binary"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// CSV 转 JSON：可修改的配置集中在这里。
// 运行时提供待处理的数据或文件；处理规则在下面配置。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "encoding": "utf-8-sig",
  "delimiter": ",",
  "header": true,
  "skip_empty": true,
  "max_input_bytes": 2097152,
  "max_output_bytes": 4194304,
  "max_rows": 10000,
  "max_columns": 200,
  "max_field_bytes": 65536
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

func Handle(ctx *Context, input any) (any, error) {
	c, e := settings(input)
	if e != nil {
		return nil, e
	}
	maximum := limit(c, "max_input_bytes", 2097152, 16777216)
	var data []byte
	if s, ok := c["content"].(string); ok {
		data = []byte(s)
	} else if len(ctx.InputFiles) > 0 {
		f, e := os.Open(ctx.InputFiles[0].Path)
		if e != nil {
			return nil, errors.New("csv_content_required")
		}
		defer f.Close()
		data, e = io.ReadAll(io.LimitReader(f, int64(maximum+1)))
		if e != nil {
			return nil, errors.New("csv_operation_failed")
		}
	} else {
		return nil, errors.New("csv_content_required")
	}
	if len(data) > maximum {
		return nil, errors.New("input_too_large")
	}
	encoding := strings.ToLower(textValue(c, "encoding", "utf-8-sig"))
	if encoding == "utf-16le" || encoding == "utf16le" {
		if _, inline := c["content"].(string); !inline {
			if len(data)%2 != 0 {
				return nil, errors.New("invalid_encoding_or_content")
			}
			words := make([]uint16, len(data)/2)
			for i := range words {
				words[i] = binary.LittleEndian.Uint16(data[i*2:])
			}
			data = []byte(string(utf16.Decode(words)))
		}
	} else if encoding != "utf-8" && encoding != "utf8" && encoding != "utf-8-sig" {
		return nil, errors.New("invalid_encoding_or_content")
	}
	if !utf8.Valid(data) {
		return nil, errors.New("invalid_encoding_or_content")
	}
	reader := csv.NewReader(strings.NewReader(strings.TrimPrefix(string(data), "\ufeff")))
	delimiter := []rune(textValue(c, "delimiter", ","))
	if len(delimiter) != 1 {
		return nil, errors.New("invalid_delimiter")
	}
	reader.Comma = delimiter[0]
	reader.FieldsPerRecord = -1
	maxColumns := limit(c, "max_columns", 200, 2000)
	maxField := limit(c, "max_field_bytes", 65536, 1048576)
	maxRows := limit(c, "max_rows", 10000, 100000)
	maxOutput := limit(c, "max_output_bytes", 4194304, 16777216)
	headers := []string{}
	if c["headers"] != nil {
		for _, v := range array(c["headers"]) {
			s, ok := v.(string)
			if !ok {
				return nil, errors.New("invalid_headers")
			}
			headers = append(headers, s)
		}
	}
	validate := func() error {
		seen := map[string]bool{}
		for _, h := range headers {
			if strings.TrimSpace(h) == "" || seen[h] {
				return errors.New("invalid_or_duplicate_header")
			}
			seen[h] = true
		}
		if len(headers) > maxColumns {
			return errors.New("column_limit_exceeded")
		}
		return nil
	}
	if e = validate(); e != nil {
		return nil, e
	}
	rows := []any{}
	var checkpoint any
	size := 2
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, errors.New("invalid_csv")
		}
		line, _ := reader.FieldPos(0)
		if len(row) > maxColumns {
			return nil, errors.New("column_limit_exceeded")
		}
		empty := true
		for _, v := range row {
			if len(v) > maxField {
				return nil, errors.New("field_limit_exceeded")
			}
			if strings.TrimSpace(v) != "" {
				empty = false
			}
		}
		if empty && c["skip_empty"] != false {
			continue
		}
		if len(headers) == 0 && c["header"] != false {
			headers = row
			if e = validate(); e != nil {
				return nil, e
			}
			continue
		}
		var item any = row
		if len(headers) > 0 {
			if len(row) > len(headers) {
				return nil, errors.New("row_has_extra_columns")
			}
			m := object{}
			for i, h := range headers {
				m[h] = nil
				if i < len(row) {
					m[h] = row[i]
				}
			}
			item = m
		}
		addition := len(encoded(item)) + 1
		if len(rows) >= maxRows || size+addition > maxOutput {
			checkpoint = object{"next_physical_row": line}
			break
		}
		rows = append(rows, item)
		size += addition
	}
	return object{"rows": rows, "count": len(rows), "partial": checkpoint != nil, "checkpoint": checkpoint, "encoding": encoding, "delimiter": string(delimiter)}, nil
}
