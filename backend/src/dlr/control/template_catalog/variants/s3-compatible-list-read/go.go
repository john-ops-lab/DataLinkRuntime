package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"io"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// S3 对象读取：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// S3_ACCESS_KEY_ID：S3 Access Key ID。
// S3_SECRET_ACCESS_KEY：S3 Secret Access Key。
// S3_SESSION_TOKEN：S3 临时凭据 Token，仅使用临时凭据时配置。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "endpoint": "https://storage.example",
  "region": "example-region-1",
  "bucket": "example-bucket",
  "prefix": "exports/",
  "continuation_token": null,
  "object_offset": 0,
  "read_keys": [],
  "force_path_style": true,
  "max_pages": 20,
  "max_objects": 1000,
  "max_object_bytes": 1048576,
  "max_total_bytes": 4194304
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
	u, e := url.Parse(stringValue(c["endpoint"]))
	if e != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "https" && u.Scheme != "http") {
		return nil, errors.New("invalid_endpoint")
	}
	access, secret := ctx.Secrets.Get("S3_ACCESS_KEY_ID"), ctx.Secrets.Get("S3_SECRET_ACCESS_KEY")
	if access == "" || secret == "" {
		return nil, errors.New("missing_credential")
	}
	lookup := minio.BucketLookupPath
	if c["force_path_style"] == false {
		lookup = minio.BucketLookupDNS
	}
	client, e := minio.New(u.Host, &minio.Options{Creds: credentials.NewStaticV4(access, secret, ctx.Secrets.Get("S3_SESSION_TOKEN")), Secure: u.Scheme == "https", Region: textValue(c, "region", "us-east-1"), BucketLookup: lookup})
	if e != nil {
		return nil, errors.New("s3_operation_failed")
	}
	core := minio.Core{Client: client}
	requestCtx, cancel := context.WithTimeout(ctx.Context, 120*time.Second)
	defer cancel()
	bucket := stringValue(c["bucket"])
	if bucket == "" {
		return nil, errors.New("bucket_required")
	}
	token := stringValue(c["continuation_token"])
	offset := number(c["object_offset"])
	if offset < 0 {
		return nil, errors.New("invalid_checkpoint")
	}
	maxPages := limit(c, "max_pages", 20, 200)
	maximum := limit(c, "max_objects", 1000, 10000)
	maxTotal := limit(c, "max_total_bytes", 4194304, 16777216)
	maxObject := limit(c, "max_object_bytes", 1048576, 8388608)
	requested := map[string]bool{}
	for _, v := range array(c["read_keys"]) {
		requested[stringValue(v)] = true
	}
	objects := []any{}
	contents := []any{}
	pages, total := 0, 0
	var checkpoint any
	seen := map[string]bool{}
	result := func() object {
		return object{"objects": objects, "contents": contents, "summary": object{"objects": len(objects), "bytes_read": total, "pages": pages}, "partial": checkpoint != nil, "checkpoint": checkpoint}
	}
scan:
	for pages < maxPages {
		page, e := core.ListObjectsV2(bucket, stringValue(c["prefix"]), "", token, "", min(1000, maximum+1))
		if e != nil {
			return nil, errors.New("s3_operation_failed")
		}
		pages++
		for i, item := range page.Contents {
			if i < offset {
				continue
			}
			mark := object{"continuation_token": token, "object_offset": i}
			if len(objects) >= maximum {
				checkpoint = mark
				break scan
			}
			metadata := object{"key": item.Key, "size": item.Size, "etag": item.ETag, "last_modified": item.LastModified.UTC().Format(time.RFC3339Nano)}
			var content any
			if requested[item.Key] {
				if item.Size > int64(maxObject) {
					content = object{"key": item.Key, "status": "skipped", "reason": "object_too_large"}
				} else {
					opts := minio.GetObjectOptions{}
					_ = opts.SetRange(0, int64(maxObject))
					f, e := client.GetObject(requestCtx, bucket, item.Key, opts)
					if e != nil {
						return nil, errors.New("s3_read_failed")
					}
					data, e := io.ReadAll(io.LimitReader(f, int64(maxObject+1)))
					_ = f.Close()
					if e != nil {
						return nil, errors.New("s3_read_failed")
					}
					if len(data) > maxObject {
						content = object{"key": item.Key, "status": "skipped", "reason": "object_too_large"}
					} else {
						content = object{"key": item.Key, "status": "read", "bytes": len(data), "content_base64": base64.StdEncoding.EncodeToString(data)}
					}
				}
			}
			needed := len(encoded(result())) + len(encoded(metadata)) + len(encoded(content)) + len(encoded(mark)) + 128
			if needed > maxTotal {
				checkpoint = mark
				break scan
			}
			objects = append(objects, metadata)
			if content != nil {
				contents = append(contents, content)
				total += number(mapping(content)["bytes"])
			}
		}
		offset = 0
		if !page.IsTruncated {
			break
		}
		if page.NextContinuationToken == "" || seen[page.NextContinuationToken] {
			return nil, errors.New("pagination_loop_detected")
		}
		seen[page.NextContinuationToken] = true
		token = page.NextContinuationToken
		if pages == maxPages {
			checkpoint = object{"continuation_token": token, "object_offset": 0}
		}
	}
	return result(), nil
}
