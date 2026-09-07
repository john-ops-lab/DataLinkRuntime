package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
)

// JSON 字段整理：可修改的配置集中在这里。
// 运行时提供待处理的数据或文件；处理规则在下面配置。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "mappings": [
    {
      "pointer": "/profile/name",
      "target": "name",
      "type": "string",
      "default": ""
    },
    {
      "pointer": "/id",
      "target": "id",
      "type": "string"
    }
  ],
  "filters": [],
  "sort": {
    "field": "name",
    "direction": "asc"
  },
  "dedupe_by": "id",
  "max_records": 10000,
  "max_fields": 200,
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

func convert(v any, kind string) (any, error) {
	switch kind {
	case "", "any":
		return v, nil
	case "string":
		if v == nil {
			return "", nil
		}
		return stringValue(v), nil
	case "number", "integer":
		n, e := strconv.ParseFloat(stringValue(v), 64)
		if e != nil || math.IsNaN(n) || math.IsInf(n, 0) || (kind == "integer" && math.Trunc(n) != n) {
			return nil, errors.New("invalid_number")
		}
		return n, nil
	case "boolean":
		switch stringValue(v) {
		case "true", "1":
			return true, nil
		case "false", "0":
			return false, nil
		}
		return nil, errors.New("invalid_boolean")
	case "datetime":
		if t, e := time.Parse(time.RFC3339Nano, stringValue(v)); e == nil {
			return t.UTC().Format(time.RFC3339Nano), nil
		}
		if n, e := strconv.ParseFloat(stringValue(v), 64); e == nil {
			if math.Abs(n) > 1e11 {
				n /= 1000
			}
			return time.Unix(int64(n), int64((n-math.Trunc(n))*1e9)).UTC().Format(time.RFC3339Nano), nil
		}
		return nil, errors.New("invalid_datetime")
	default:
		return nil, errors.New("unsupported_conversion")
	}
}

func Handle(ctx *Context, input any) (any, error) {
	c, e := settings(input)
	if e != nil {
		return nil, e
	}
	records, ok := c["records"].([]any)
	if !ok {
		return nil, errors.New("records_and_mappings_required")
	}
	mappings := array(c["mappings"])
	if len(mappings) > limit(c, "max_fields", 200, 1000) {
		return nil, errors.New("invalid_limits")
	}
	rows := []any{}
	seen := map[string]bool{}
	maximum := limit(c, "max_records", 10000, 100000)
	partial := len(records) > maximum
	for _, record := range records[:min(len(records), maximum)] {
		keep := true
		for _, raw := range array(c["filters"]) {
			rule := mapping(raw)
			v, exists := lookup(record, stringValue(rule["pointer"]))
			switch textValue(rule, "op", "equals") {
			case "exists":
				want := rule["value"] != false
				keep = keep && (exists == want)
			case "equals":
				keep = keep && exists && string(encoded(v)) == string(encoded(rule["value"]))
			default:
				return nil, errors.New("unsupported_filter")
			}
		}
		if !keep {
			continue
		}
		row := object{}
		for _, raw := range mappings {
			m := mapping(raw)
			target := stringValue(m["target"])
			if target == "" || strings.Contains(target, ".") {
				return nil, errors.New("invalid_target")
			}
			v, found := lookup(record, stringValue(m["pointer"]))
			if !found {
				v, found = m["default"]
				if !found {
					if m["required"] == true {
						return nil, errors.New("missing_pointer")
					}
					continue
				}
			}
			value, err := convert(v, stringValue(m["type"]))
			if err != nil {
				return nil, err
			}
			row[target] = value
		}
		if field := stringValue(c["dedupe_by"]); field != "" {
			key := string(encoded(row[field]))
			if seen[key] {
				continue
			}
			seen[key] = true
		}
		rows = append(rows, row)
	}
	if s := mapping(c["sort"]); s != nil {
		field := stringValue(s["field"])
		desc := s["direction"] == "desc"
		sort.SliceStable(rows, func(i, j int) bool {
			a, b := string(encoded(mapping(rows[i])[field])), string(encoded(mapping(rows[j])[field]))
			if desc {
				return a > b
			}
			return a < b
		})
	}
	bytes := 2
	bounded := []any{}
	for _, r := range rows {
		size := len(encoded(r)) + 1
		if bytes+size > limit(c, "max_output_bytes", 4194304, 16777216) {
			partial = true
			break
		}
		bounded = append(bounded, r)
		bytes += size
	}
	var checkpoint any
	if partial {
		checkpoint = object{"reason": "output_limit", "emitted": len(bounded)}
		if len(records) > maximum {
			checkpoint = object{"reason": "input_limit", "next_index": maximum}
		}
	}
	return object{"records": bounded, "count": len(bounded), "partial": partial, "checkpoint": checkpoint}, nil
}
