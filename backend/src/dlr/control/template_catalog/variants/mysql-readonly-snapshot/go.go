package main

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/go-sql-driver/mysql"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// MySQL 数据查询：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// MYSQL_DSN：MySQL 连接字符串（包含账号密码）。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "sql": "SELECT id, name FROM example_items WHERE updated_at >= ?",
  "params": [
    "2026-01-01T00:00:00Z"
  ],
  "max_rows": 5000,
  "max_output_bytes": 4194304,
  "max_cell_bytes": 1048576,
  "batch_size": 500,
  "timeout_seconds": 30
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
	query := stringValue(c["sql"])
	if !regexp.MustCompile(`(?i)^\s*select\b`).MatchString(query) || strings.Contains(query, ";") || regexp.MustCompile(`(?i)\b(insert|update|delete|merge|call|execute|create|alter|drop|truncate|copy)\b`).MatchString(query) {
		return nil, errors.New("readonly_select_required")
	}
	dsn := ctx.Secrets.Get("MYSQL_DSN")
	if dsn == "" {
		return nil, errors.New("missing_credential")
	}
	u, e := url.Parse(dsn)
	if e != nil || u.Scheme != "mysql" || u.User == nil {
		return nil, errors.New("invalid_dsn")
	}
	password, _ := u.User.Password()
	port := u.Port()
	if port == "" {
		port = "3306"
	}
	cfg := mysql.NewConfig()
	cfg.User = u.User.Username()
	cfg.Passwd = password
	cfg.Net = "tcp"
	cfg.Addr = net.JoinHostPort(u.Hostname(), port)
	cfg.DBName = strings.TrimPrefix(u.Path, "/")
	cfg.ParseTime = true
	cfg.MultiStatements = false
	if tls := u.Query().Get("ssl-mode"); tls != "" {
		if tls == "REQUIRED" || tls == "VERIFY_IDENTITY" || tls == "VERIFY_CA" {
			cfg.TLSConfig = "true"
		} else {
			return nil, errors.New("unsupported_tls_mode")
		}
	}
	dsn = cfg.FormatDSN()
	db, e := sql.Open("mysql", dsn)
	if e != nil {
		return nil, errors.New("database_connection_failed")
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	requestCtx, cancel := context.WithTimeout(ctx.Context, time.Duration(limit(c, "timeout_seconds", 30, 300))*time.Second)
	defer cancel()
	tx, e := db.BeginTx(requestCtx, &sql.TxOptions{ReadOnly: true})
	if e != nil {
		return nil, errors.New("readonly_transaction_failed")
	}
	defer tx.Rollback()
	params := array(c["params"])
	for i, v := range params {
		if n, ok := v.(json.Number); ok {
			params[i] = string(n)
		}
	}
	cursor, e := tx.QueryContext(requestCtx, query, params...)
	if e != nil {
		return nil, errors.New("database_query_failed")
	}
	defer cursor.Close()
	columns, e := cursor.Columns()
	if e != nil {
		return nil, errors.New("database_query_failed")
	}
	seen := map[string]bool{}
	for _, col := range columns {
		if seen[col] {
			return nil, errors.New("duplicate_column_name")
		}
		seen[col] = true
	}
	types, _ := cursor.ColumnTypes()
	rows := []any{}
	size := 2
	partial := false
	failure := ""
	for cursor.Next() {
		if len(rows) >= limit(c, "max_rows", 5000, 100000) {
			partial = true
			break
		}
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if e = cursor.Scan(pointers...); e != nil {
			failure = "database_query_failed"
			partial = true
			break
		}
		row := object{}
		for i, col := range columns {
			v := values[i]
			switch x := v.(type) {
			case []byte:
				kind := strings.ToUpper(types[i].DatabaseTypeName())
				if strings.Contains(kind, "BLOB") || kind == "BYTEA" || strings.Contains(kind, "BINARY") {
					v = object{"$binary_base64": base64.StdEncoding.EncodeToString(x)}
				} else {
					v = string(x)
				}
			case int64:
				v = strconv.FormatInt(x, 10)
			case time.Time:
				v = x.UTC().Format(time.RFC3339Nano)
			}
			if len(encoded(v)) > limit(c, "max_cell_bytes", 1048576, 8388608) {
				partial = true
				failure = "cell_limit_exceeded"
				break
			}
			row[col] = v
		}
		if failure != "" {
			break
		}
		addition := len(encoded(row)) + 1
		if size+addition > limit(c, "max_output_bytes", 4194304, 16777216) {
			partial = true
			break
		}
		rows = append(rows, row)
		size += addition
	}
	if cursor.Err() != nil {
		partial = true
		failure = "database_query_failed"
	}
	var checkpoint any
	if partial {
		checkpoint = object{"row_offset": len(rows)}
	}
	result := object{"rows": rows, "count": len(rows), "partial": partial, "checkpoint": checkpoint}
	if failure != "" {
		result["error"] = failure
	}
	return result, nil
}
