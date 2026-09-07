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

// REST 接口请求：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// HTTP_BASIC_CREDENTIAL：HTTP Basic 认证，值为 username:password。
// HTTP_BEARER_TOKEN：HTTP Bearer Token，使用此认证时配置。
// HTTP_API_KEY：HTTP API Key，使用此认证时配置。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "url": "https://api.example/resources",
  "method": "GET",
  "query": {},
  "query_auth": null,
  "headers": {
    "Accept": "application/json"
  },
  "body": null,
  "response_type": "json",
  "allowed_statuses": [
    200
  ],
  "timeout_seconds": 30,
  "max_response_bytes": 1048576,
  "max_redirects": 3
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
	method := strings.ToUpper(textValue(c, "method", "GET"))
	if !strings.Contains("|GET|POST|PUT|PATCH|DELETE|", "|"+method+"|") {
		return nil, errors.New("unsupported_method")
	}
	side := method != "GET"
	u, e := checkedURL(stringValue(c["url"]))
	if e != nil {
		return nil, e
	}
	if e = authQuery(u, "", "", false); e != nil {
		return nil, e
	}
	q := u.Query()
	for k, v := range mapping(c["query"]) {
		if sensitiveName(k) {
			return nil, errors.New("direct_credential_query_forbidden")
		}
		s, ok := v.(string)
		if !ok {
			return nil, errors.New("invalid_query")
		}
		q.Set(k, s)
	}
	u.RawQuery = q.Encode()
	headers, secrets, name, value, e := requestAuth(ctx, c)
	if e != nil {
		return nil, e
	}
	if e = authQuery(u, name, value, false); e != nil {
		return nil, e
	}
	var body []byte
	if c["body"] != nil {
		body = encoded(c["body"])
		headers.Set("Content-Type", textValue(c, "content_type", "application/json"))
		if c["content_type"] == "text/plain" {
			body = []byte(stringValue(c["body"]))
		}
	}
	requestCtx, cancel := context.WithTimeout(ctx.Context, time.Duration(limit(c, "timeout_seconds", 30, 120))*time.Second)
	defer cancel()
	maximum := limit(c, "max_response_bytes", 1048576, 8388608)
	origin := u.Scheme + "://" + u.Host
	fail := func(code string) any {
		return object{"ok": false, "error": code, "side_effect_uncertain": side, "retried": false}
	}
	for redirects := 0; ; redirects++ {
		data, status, responseHeaders, err := httpRead(requestCtx, method, u, headers, body, maximum)
		if err != nil {
			if err.Error() == "response_too_large" {
				return object{"ok": true, "status": status, "partial": true, "bytes_read": maximum, "response": nil}, nil
			}
			return fail("request_failed"), nil
		}
		if status == 301 || status == 302 || status == 303 || status == 307 || status == 308 {
			if side {
				return fail("side_effect_redirect_forbidden"), nil
			}
			if redirects >= limit(c, "max_redirects", 3, 10) {
				return fail("redirect_limit_exceeded"), nil
			}
			location := responseHeaders.Get("Location")
			if location == "" {
				return fail("redirect_without_location"), nil
			}
			next, e := u.Parse(location)
			if e != nil {
				return fail("invalid_url"), nil
			}
			next, e = checkedURL(next.String())
			if e != nil || next.Scheme+"://"+next.Host != origin {
				return fail("cross_origin_redirect"), nil
			}
			if e = authQuery(next, name, value, true); e != nil {
				return fail(e.Error()), nil
			}
			u = next
			continue
		}
		allowed := false
		for _, v := range array(c["allowed_statuses"]) {
			if number(v) == status {
				allowed = true
			}
		}
		if !allowed {
			return object{"ok": false, "error": "unexpected_status", "status": status, "side_effect_uncertain": side, "retried": false}, nil
		}
		var payload any = string(data)
		if c["response_type"] == "json" || strings.Contains(responseHeaders.Get("Content-Type"), "application/json") {
			if json.Unmarshal(data, &payload) != nil {
				return object{"ok": false, "error": "invalid_json_response", "status": status}, nil
			}
		}
		payload = scrub(payload, secrets)
		partial := len(encoded(payload)) > maximum
		if partial {
			payload = nil
		}
		return object{"ok": true, "status": status, "content_type": responseHeaders.Get("Content-Type"), "partial": partial, "bytes_read": len(data), "response": payload, "side_effect_warning": side}, nil
	}
}
