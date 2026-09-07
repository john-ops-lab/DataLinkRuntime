package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ServiceNow 资产采集：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// SERVICENOW_USERNAME：ServiceNow 用户名，使用 Basic 认证时配置。
// SERVICENOW_PASSWORD：ServiceNow 密码，使用 Basic 认证时配置。
// SERVICENOW_BEARER_TOKEN：ServiceNow Bearer Token，与 Basic 认证二选一。
// CMDB_TOKEN：目标 CMDB Token，仅同步时配置。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "mode": "preview",
  "instance_url": "https://tenant.example",
  "instance_id": "EXAMPLE_TENANT",
  "table": "cmdb_ci",
  "encoded_query": "active=true",
  "fields": [
    "sys_id",
    "name",
    "sys_class_name",
    "install_status"
  ],
  "display_value": false,
  "max_pages": 20,
  "max_records": 5000,
  "max_bytes": 8388608,
  "page_size": 500,
  "timeout_seconds": 30,
  "offset": 0,
  "batch_size": 200,
  "source_scope": "servicenow:EXAMPLE_TENANT:cmdb_ci",
  "scan_id": "",
  "cmdb_base_url": "https://cmdb.example"
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

func sensitiveName(name string) bool {
	compact := regexp.MustCompile(`[^a-z0-9]`).ReplaceAllString(strings.ToLower(name), "")
	for _, s := range []string{"accesskey", "apikey", "authorization", "authentication", "clientsecret", "cookie", "credential", "password", "privatekey", "secret", "signature", "token"} {
		if strings.Contains(compact, s) {
			return true
		}
	}
	return strings.HasSuffix(compact, "auth") || strings.HasSuffix(compact, "sig")
}
func checkedURL(raw string) (*url.URL, error) {
	u, e := url.Parse(raw)
	if e != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.Fragment != "" {
		return nil, errors.New("invalid_url")
	}
	return u, nil
}
func requestAuth(ctx *Context, c object) (http.Header, []string, string, string, error) {
	headers := http.Header{}
	sensitive := []string{}
	for k, v := range mapping(c["headers"]) {
		if strings.EqualFold(k, "DLR-Auth") {
			continue
		}
		if sensitiveName(k) {
			return nil, nil, "", "", errors.New("direct_credential_header_forbidden")
		}
		lower := strings.ToLower(k)
		if strings.ContainsAny(stringValue(v), "\r\n") || strings.HasPrefix(lower, "proxy-") || strings.Contains("|host|content-length|transfer-encoding|connection|upgrade|te|trailer|", "|"+lower+"|") {
			return nil, nil, "", "", errors.New("invalid_headers")
		}
		headers.Set(k, stringValue(v))
	}
	for k, v := range mapping(c["headers"]) {
		if !strings.EqualFold(k, "DLR-Auth") {
			continue
		}
		parts := strings.SplitN(stringValue(v), ":", 2)
		if len(parts) != 2 {
			return nil, nil, "", "", errors.New("invalid_auth_scheme")
		}
		secret := ctx.Secrets.Get(parts[1])
		if secret == "" {
			return nil, nil, "", "", errors.New("missing_credential")
		}
		sensitive = append(sensitive, secret)
		switch {
		case parts[0] == "bearer":
			headers.Set("Authorization", "Bearer "+secret)
		case parts[0] == "basic":
			headers.Set("Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte(secret)))
		case strings.HasPrefix(parts[0], "api-key/"):
			headers.Set(parts[0][8:], secret)
		default:
			return nil, nil, "", "", errors.New("invalid_auth_scheme")
		}
	}
	name, value := "", ""
	if q := mapping(c["query_auth"]); q != nil {
		name = stringValue(q["parameter"])
		if len(q) != 2 || !regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,63}$`).MatchString(name) {
			return nil, nil, "", "", errors.New("invalid_query_auth")
		}
		value = ctx.Secrets.Get(stringValue(q["secret_binding"]))
		if value == "" {
			return nil, nil, "", "", errors.New("missing_credential")
		}
		sensitive = append(sensitive, value, url.QueryEscape(value), url.PathEscape(value))
	}
	for _, values := range headers {
		for _, v := range values {
			if headers.Get("Authorization") == v {
				sensitive = append(sensitive, v)
			}
		}
	}
	return headers, sensitive, name, value, nil
}
func authQuery(u *url.URL, name, value string, allowInjected bool) error {
	q := u.Query()
	for k, values := range q {
		if sensitiveName(k) && !(allowInjected && k == name && len(values) == 1 && values[0] == value) {
			return errors.New("direct_credential_query_forbidden")
		}
	}
	if name != "" {
		if q.Has(name) && !(allowInjected && q.Get(name) == value && len(q[name]) == 1) {
			return errors.New("credential_query_collision")
		}
		q.Set(name, value)
	}
	u.RawQuery = q.Encode()
	return nil
}
func scrub(v any, secrets []string) any {
	sort.SliceStable(secrets, func(i, j int) bool { return len(secrets[i]) > len(secrets[j]) })
	switch x := v.(type) {
	case string:
		for _, s := range secrets {
			if s != "" {
				x = strings.ReplaceAll(x, s, "<redacted>")
			}
		}
		return x
	case []any:
		r := make([]any, len(x))
		for i, v := range x {
			r[i] = scrub(v, secrets)
		}
		return r
	case map[string]any:
		r := object{}
		for k, v := range x {
			r[stringValue(scrub(k, secrets))] = scrub(v, secrets)
		}
		return r
	}
	return v
}
func httpRead(ctx context.Context, method string, u *url.URL, headers http.Header, body []byte, maximum int) ([]byte, int, http.Header, error) {
	req, e := http.NewRequestWithContext(ctx, method, u.String(), bytes.NewReader(body))
	if e != nil {
		return nil, 0, nil, errors.New("request_failed")
	}
	req.Header = headers.Clone()
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, e := client.Do(req)
	if e != nil {
		return nil, 0, nil, errors.New("request_failed")
	}
	defer response.Body.Close()
	data, e := io.ReadAll(io.LimitReader(response.Body, int64(maximum+1)))
	if e != nil {
		return nil, response.StatusCode, response.Header, errors.New("request_failed")
	}
	if len(data) > maximum {
		return nil, response.StatusCode, response.Header, errors.New("response_too_large")
	}
	return data, response.StatusCode, response.Header, nil
}

func digest(v any) string { sum := sha256.Sum256(encoded(v)); return fmt.Sprintf("%x", sum) }
func syncSnapshot(ctx *Context, requestCtx context.Context, c object, provider string, assets, relations []any, summary object) (any, error) {
	scan, scope := stringValue(c["scan_id"]), stringValue(c["source_scope"])
	identity := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`)
	if !identity.MatchString(scan) || !identity.MatchString(scope) {
		return nil, errors.New("stable_scan_identity_required")
	}
	base, e := checkedURL(stringValue(c["cmdb_base_url"]))
	token := ctx.Secrets.Get("CMDB_TOKEN")
	if e != nil || token == "" {
		return nil, errors.New("cmdb_target_not_configured")
	}
	loopback := base.Hostname() == "localhost" || base.Hostname() == "127.0.0.1" || base.Hostname() == "::1"
	if base.RawQuery != "" || (base.Scheme != "https" && !loopback) {
		return nil, errors.New("cmdb_target_not_configured")
	}
	result := object{"mode": "sync", "scan_id": scan, "source_scope": scope, "partial": false, "summary": summary, "failed": []any{}, "checkpoint": nil}
	fail := func() any {
		result["partial"] = true
		result["failed"] = []string{"target_batch"}
		result["checkpoint"] = object{"scan_id": scan}
		return result
	}
	post := func(path string, body object, idem string) error {
		u := *base
		u.Path = path
		body["schema_version"] = "dlr-cmdb-upsert/v1"
		body["source_scope"] = scope
		body["scan_id"] = scan
		body["idempotency_key"] = idem
		h := http.Header{"Content-Type": []string{"application/json"}, "Authorization": []string{"Bearer " + token}, "Idempotency-Key": []string{idem}}
		_, status, _, err := httpRead(requestCtx, "POST", &u, h, encoded(body), 1048576)
		if err != nil || status < 200 || status >= 300 {
			return errors.New("cmdb_target_error")
		}
		return nil
	}
	if post("/api/v1/import-scans:begin", object{"operation": "begin_scan", "provider": provider, "catalog_version": "1.0.0"}, digest([]string{"begin", scope, scan})) != nil {
		return fail(), nil
	}
	for _, phase := range []string{"assets", "relationships"} {
		items := assets
		if phase == "relationships" {
			items = relations
		}
		size := limit(c, "batch_size", 200, 1000)
		for start := 0; start < len(items); start += size {
			index := start / size
			batchID := fmt.Sprintf("%s:%s:%s:%06d", phase, provider, scope, index)
			body := object{"operation": "upsert_" + phase, "batch_id": batchID, "batch_index": index, phase: items[start:min(start+size, len(items))]}
			if post("/api/v1/import-scans/"+scan+"/"+phase+":upsert", body, digest([]string{phase, scope, scan, batchID})) != nil {
				return fail(), nil
			}
		}
	}
	if post("/api/v1/import-scans/"+scan+":finish", object{"operation": "finish_scan", "complete": true, "summary": summary}, digest([]string{"finish", scope, scan})) != nil {
		return fail(), nil
	}
	return result, nil
}
func snapshot(ctx *Context, requestCtx context.Context, c object, provider string, assets, relations []any, pages int, failures []any, partial bool, checkpoint any) (any, error) {
	if len(failures) > 50 {
		failures = failures[:50]
	}
	sort.SliceStable(assets, func(i, j int) bool {
		return stringValue(mapping(assets[i])["external_key"]) < stringValue(mapping(assets[j])["external_key"])
	})
	sort.SliceStable(relations, func(i, j int) bool { return string(encoded(relations[i])) < string(encoded(relations[j])) })
	summary := object{"assets": len(assets), "relationships": len(relations), "pages": pages, "failures": failures}
	maximum := limit(c, "max_bytes", 8388608, 16777216)
	result := object{"schema_version": "dlr-asset-snapshot/v1", "assets": assets, "relationships": relations, "summary": summary, "partial": partial, "checkpoint": checkpoint}
	for len(encoded(result)) > maximum {
		partial = true
		result["partial"] = true
		result["checkpoint"] = object{"limit_reached": true}
		if len(relations) > 0 {
			relations = relations[:len(relations)-1]
			result["relationships"] = relations
			summary["relationships"] = len(relations)
		} else if len(assets) > 0 {
			assets = assets[:len(assets)-1]
			result["assets"] = assets
			summary["assets"] = len(assets)
		} else {
			return nil, errors.New("max_bytes_too_small")
		}
	}
	if c["mode"] == "sync" {
		if partial {
			return object{"mode": "sync", "scan_id": c["scan_id"], "source_scope": c["source_scope"], "partial": true, "summary": summary, "failed": []string{"incomplete_source"}, "checkpoint": result["checkpoint"]}, nil
		}
		return syncSnapshot(ctx, requestCtx, c, provider, assets, relations, summary)
	}
	return result, nil
}

func Handle(ctx *Context, input any) (any, error) {
	c, e := settings(input)
	if e != nil {
		return nil, e
	}
	if c["mode"] != "preview" && c["mode"] != "sync" {
		return nil, errors.New("invalid_mode")
	}
	base, e := checkedURL(stringValue(c["instance_url"]))
	if e != nil {
		return nil, e
	}
	table := textValue(c, "table", "cmdb_ci")
	if !regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]{0,79}$`).MatchString(table) {
		return nil, errors.New("invalid_table")
	}
	base.Path = "/api/now/table/" + table
	headers := http.Header{"Accept": []string{"application/json"}}
	if token := ctx.Secrets.Get("SERVICENOW_BEARER_TOKEN"); token != "" {
		headers.Set("Authorization", "Bearer "+token)
	} else if basic := ctx.Secrets.Get("SERVICENOW_USERNAME") + ":" + ctx.Secrets.Get("SERVICENOW_PASSWORD"); basic != ":" {
		headers.Set("Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte(basic)))
	} else {
		return nil, errors.New("missing_credential")
	}
	requestCtx, cancel := context.WithTimeout(ctx.Context, time.Duration(limit(c, "timeout_seconds", 30, 120))*time.Second)
	defer cancel()
	offset := number(c["offset"])
	size := limit(c, "page_size", 500, 1000)
	maxPages := limit(c, "max_pages", 20, 100)
	maximum := limit(c, "max_bytes", 8388608, 16777216)
	assets, failures := []any{}, []any{}
	pages, total := 0, 0
	partial, complete := false, false
	account := stringValue(c["instance_id"])
	if account == "" {
		return nil, errors.New("instance_id_required")
	}
	seen := map[string]bool{}
	for pages < maxPages {
		q := base.Query()
		q.Set("sysparm_query", stringValue(c["encoded_query"]))
		fields := []string{}
		for _, v := range array(c["fields"]) {
			fields = append(fields, stringValue(v))
		}
		q.Set("sysparm_fields", strings.Join(fields, ","))
		q.Set("sysparm_display_value", stringValue(c["display_value"]))
		q.Set("sysparm_offset", strconv.Itoa(offset))
		q.Set("sysparm_limit", strconv.Itoa(size))
		base.RawQuery = q.Encode()
		var data []byte
		status := 0
		var err error
		for attempt := 0; attempt < 3; attempt++ {
			data, status, _, err = httpRead(requestCtx, "GET", base, headers, nil, maximum-total)
			if err == nil && status != 429 && status < 500 {
				break
			}
			if requestCtx.Err() != nil {
				break
			}
		}
		pages++
		if err != nil || status < 200 || status >= 300 {
			partial = true
			failures = append(failures, "servicenow_request_failed")
			break
		}
		var payload object
		if json.Unmarshal(data, &payload) != nil {
			partial = true
			failures = append(failures, "servicenow_invalid_result")
			break
		}
		batch, ok := payload["result"].([]any)
		if !ok {
			partial = true
			failures = append(failures, "servicenow_invalid_result")
			break
		}
		if len(batch) == 0 {
			complete = true
			break
		}
		total += len(data)
		fingerprint := digest(batch)
		if seen[fingerprint] {
			partial = true
			failures = append(failures, "pagination_no_progress")
			break
		}
		seen[fingerprint] = true
		for _, raw := range batch {
			record := mapping(raw)
			id, ok := record["sys_id"].(string)
			if !ok || id == "" {
				partial = true
				failures = append(failures, "invalid_source_record")
				break
			}
			attrs := object{}
			for k, v := range record {
				if k == "sys_id" {
					continue
				}
				if m := mapping(v); m != nil {
					v = m["display_value"]
				}
				switch v.(type) {
				case nil, string, float64, bool:
					attrs[k] = v
				}
			}
			escape := func(v string) string { return strings.ReplaceAll(strings.ReplaceAll(v, "%", "%25"), ":", "%3A") }
			asset := object{"external_key": "servicenow:" + escape(account) + ":global:cmdb_ci:" + escape(id), "class": textValue(attrs, "sys_class_name", "cmdb_ci"), "provider_type": "cmdb_ci", "name": textValue(attrs, "name", id), "account": account, "region": "global", "zone": nil, "status": attrs["install_status"], "tags": object{}, "attributes": attrs}
			if len(assets) >= limit(c, "max_records", 5000, 50000) || len(encoded(assets))+len(encoded(asset))+1024 > maximum {
				partial = true
				break
			}
			assets = append(assets, asset)
			offset++
		}
		if partial {
			break
		}
		if len(batch) < size {
			complete = true
			break
		}
		if total >= maximum {
			partial = true
			break
		}
	}
	partial = partial || !complete
	var checkpoint any
	if partial {
		checkpoint = object{"offset": offset}
	}
	return snapshot(ctx, requestCtx, c, "servicenow", assets, []any{}, pages, failures, partial, checkpoint)
}
