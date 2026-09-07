package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"
)

// Webhook 数据整理：可修改的配置集中在这里。
// 运行时提供待处理的数据或文件；处理规则在下面配置。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "required": [
    "event_id"
  ],
  "mappings": [
    {
      "source": "event_id",
      "target": "id",
      "required": true
    },
    {
      "source": "occurred_at",
      "target": "timestamp",
      "type": "datetime",
      "required": true
    }
  ],
  "max_fields": 200,
  "max_input_bytes": 1048576,
  "max_output_bytes": 2097152,
  "max_depth": 32
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

func assign(target object, path string, value any) error {
	parts := strings.Split(path, ".")
	for i, k := range parts {
		if k == "" || k == "__proto__" || k == "constructor" || k == "prototype" {
			return errors.New("invalid_target")
		}
		if i == len(parts)-1 {
			if _, ok := target[k]; ok {
				return errors.New("duplicate_target")
			}
			target[k] = value
			return nil
		}
		if target[k] == nil {
			target[k] = object{}
		}
		next, ok := target[k].(map[string]any)
		if !ok {
			return errors.New("conflicting_target")
		}
		target = next
	}
	return nil
}
func depth(v any) int {
	d := 0
	switch x := v.(type) {
	case map[string]any:
		for _, v := range x {
			d = max(d, depth(v))
		}
	case []any:
		for _, v := range x {
			d = max(d, depth(v))
		}
	default:
		return 0
	}
	return d + 1
}
func Handle(ctx *Context, input any) (any, error) {
	c, e := settings(input)
	if e != nil {
		return nil, e
	}
	payload := mapping(c["payload"])
	if payload == nil {
		return nil, errors.New("payload_must_be_object")
	}
	if len(encoded(payload)) > limit(c, "max_input_bytes", 1048576, 8388608) {
		return nil, errors.New("payload_too_large")
	}
	if depth(payload) > limit(c, "max_depth", 32, 64) {
		return nil, errors.New("payload_too_deep")
	}
	fields := limit(c, "max_fields", 200, 1000)
	if len(array(c["required"])) > fields || len(array(c["mappings"])) > fields {
		return nil, errors.New("invalid_mappings")
	}
	issues := []any{}
	normalized := object{}
	partial := false
	for i, p := range array(c["required"]) {
		v, ok := lookup(payload, stringValue(p))
		if !ok || v == nil {
			issues = append(issues, object{"field": fmt.Sprintf("required[%d]", i), "code": "required"})
		}
	}
	for i, raw := range array(c["mappings"]) {
		m := mapping(raw)
		field := fmt.Sprintf("mappings[%d]", i)
		v, found := lookup(payload, stringValue(m["source"]))
		if !found {
			v, found = m["default"]
			if !found {
				if m["required"] == true {
					issues = append(issues, object{"field": field + ".source", "code": "missing"})
				}
				continue
			}
		}
		value, err := convert(v, stringValue(m["type"]))
		if err == nil {
			err = assign(normalized, stringValue(m["target"]), value)
		}
		if err != nil {
			issues = append(issues, object{"field": field, "code": "invalid_value"})
		}
		if len(encoded(normalized))+len(encoded(issues))+100 > limit(c, "max_output_bytes", 2097152, 8388608) {
			normalized = object{}
			issues = []any{object{"field": "", "code": "output_limit"}}
			partial = true
			break
		}
	}
	return object{"valid": len(issues) == 0, "data": normalized, "errors": issues, "partial": partial}, nil
}
