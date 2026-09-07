package main

import (
	"bytes"
	"context"
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

// REST 分页采集：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// HTTP_BEARER_TOKEN：HTTP Bearer Token，使用此认证时配置。
// HTTP_API_KEY：HTTP API Key，使用此认证时配置。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "url": "https://api.example/resources",
  "strategy": "page",
  "records_path": "items",
  "next_path": "next",
  "page_parameter": "page",
  "size_parameter": "page_size",
  "start_page": 1,
  "page_size": 100,
  "headers": {},
  "query_auth": null,
  "allow_cross_origin_next": false,
  "max_pages": 20,
  "max_records": 10000,
  "max_bytes": 4194304,
  "timeout_seconds": 30,
  "max_retries": 2
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

func Handle(ctx *Context, input any) (any, error) {
	c, e := settings(input)
	if e != nil {
		return nil, e
	}
	base, e := checkedURL(stringValue(c["url"]))
	if e != nil {
		return nil, e
	}
	if e = authQuery(base, "", "", false); e != nil {
		return nil, e
	}
	headers, secrets, name, value, e := requestAuth(ctx, c)
	if e != nil {
		return nil, e
	}
	strategy := textValue(c, "strategy", "page")
	if !strings.Contains("|page|offset|cursor|next-url|", "|"+strategy+"|") {
		return nil, errors.New("invalid_strategy")
	}
	page := limit(c, "start_page", 1, 1000000)
	offset := number(c["start_offset"])
	size := limit(c, "page_size", 100, 1000)
	maxPages := limit(c, "max_pages", 20, 500)
	maxRecords := limit(c, "max_records", 10000, 100000)
	maxBytes := limit(c, "max_bytes", 4194304, 16777216)
	cursor := ""
	next := base
	requestCtx, cancel := context.WithTimeout(ctx.Context, time.Duration(limit(c, "timeout_seconds", 30, 120))*time.Second)
	defer cancel()
	records := []any{}
	seen := map[string]bool{}
	batches := map[string]bool{}
	pages := 0
	total := 0
	partial := false
	completed := false
	for pages < maxPages {
		if total >= maxBytes {
			partial = true
			break
		}
		u := *base
		if strategy == "next-url" {
			u = *next
		}
		q := u.Query()
		switch strategy {
		case "page":
			q.Set(textValue(c, "page_parameter", "page"), strconv.Itoa(page))
			q.Set(textValue(c, "size_parameter", "page_size"), strconv.Itoa(size))
		case "offset":
			q.Set(textValue(c, "offset_parameter", "offset"), strconv.Itoa(offset))
			q.Set(textValue(c, "limit_parameter", "limit"), strconv.Itoa(size))
		case "cursor":
			q.Set(textValue(c, "limit_parameter", "limit"), strconv.Itoa(size))
			if cursor != "" {
				q.Set(textValue(c, "cursor_parameter", "cursor"), cursor)
			}
		}
		u.RawQuery = q.Encode()
		cross := u.Host != base.Host || u.Scheme != base.Scheme
		h := headers.Clone()
		if cross {
			if c["allow_cross_origin_next"] != true {
				return nil, errors.New("cross_origin_next_url")
			}
			h = http.Header{"Accept": []string{"application/json"}}
			q := u.Query()
			q.Del(name)
			u.RawQuery = q.Encode()
			if e = authQuery(&u, "", "", false); e != nil {
				return nil, e
			}
		} else if e = authQuery(&u, name, value, strategy == "next-url"); e != nil {
			return nil, e
		}
		var data []byte
		status := 0
		var err error
		retries := max(0, min(number(c["max_retries"]), 5))
		for attempt := 0; attempt <= retries; attempt++ {
			data, status, _, err = httpRead(requestCtx, "GET", &u, h, nil, maxBytes-total)
			if err == nil && status != 429 && status < 500 {
				break
			}
			if attempt < retries {
				select {
				case <-requestCtx.Done():
					return nil, errors.New("request_timeout")
				case <-time.After(time.Duration(100*(1<<attempt)) * time.Millisecond):
				}
			}
		}
		if err != nil {
			return nil, err
		}
		if status < 200 || status >= 300 {
			return nil, errors.New("unexpected_status")
		}
		var payload any
		if json.Unmarshal(data, &payload) != nil {
			return nil, errors.New("invalid_json_response")
		}
		pages++
		total += len(data)
		batch, ok := at(payload, textValue(c, "records_path", "items")).([]any)
		if !ok {
			return nil, errors.New("records_path_not_array")
		}
		if len(batch) == 0 {
			completed = true
			break
		}
		safe := array(scrub(batch, secrets))
		fingerprint := string(encoded(safe))
		if batches[fingerprint] {
			return nil, errors.New("pagination_no_progress")
		}
		batches[fingerprint] = true
		if len(records)+len(safe) > maxRecords || len(encoded(records))+len(encoded(safe)) > maxBytes {
			partial = true
			break
		}
		records = append(records, safe...)
		switch strategy {
		case "page":
			page++
		case "offset":
			offset += len(batch)
		default:
			candidate := stringValue(at(payload, textValue(c, "next_path", "next")))
			if candidate == "" {
				completed = true
			}
			if seen[candidate] {
				return nil, errors.New("pagination_loop_detected")
			}
			seen[candidate] = true
			if strategy == "cursor" {
				cursor = candidate
			} else {
				next, e = u.Parse(candidate)
				if e != nil {
					return nil, errors.New("invalid_url")
				}
				next, e = checkedURL(next.String())
				if e != nil {
					return nil, e
				}
			}
		}
		if completed {
			break
		}
		if len(records) >= maxRecords {
			partial = true
			break
		}
	}
	partial = partial || (!completed && pages == maxPages)
	var checkpoint any
	if partial {
		if strategy == "page" {
			checkpoint = object{"strategy": "page", "start_page": page}
		} else if strategy == "offset" {
			checkpoint = object{"strategy": "offset", "start_offset": offset}
		}
	}
	return object{"records": records, "count": len(records), "pages": pages, "bytes": total, "partial": partial, "checkpoint": checkpoint}, nil
}
