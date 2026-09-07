package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/pkg/sftp"
	"golang.org/x/crypto/ssh"
	"io"
	"net"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"
)

// SFTP 文件读取：可修改的配置集中在这里。
// 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
// 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
// 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
// SFTP_USERNAME：SFTP 用户名。
// SFTP_PASSWORD：SFTP 密码（密码和私钥二选一）。
// SFTP_PRIVATE_KEY：SFTP 私钥全文（密码和私钥二选一）。
// SFTP_PRIVATE_KEY_PASSPHRASE：私钥口令，仅加密私钥需要。
// Go：配置保持 JSON 格式；入口由平台调用，无需 main()。
const configJSON = `{
  "host": "sftp.example",
  "port": 22,
  "host_fingerprint_sha256": "SHA256:EXAMPLE_HOST_KEY_FINGERPRINT",
  "base_directory": "/exports",
  "path": ".",
  "start_at": null,
  "suffix": ".json",
  "read_paths": [],
  "max_files": 500,
  "max_file_bytes": 1048576,
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
	username := ctx.Secrets.Get("SFTP_USERNAME")
	if username == "" {
		username = stringValue(c["username"])
	}
	fingerprint := stringValue(c["host_fingerprint_sha256"])
	if username == "" || fingerprint == "" || stringValue(c["host"]) == "" {
		return nil, errors.New("host_username_fingerprint_and_base_required")
	}
	auth := []ssh.AuthMethod{}
	if password := ctx.Secrets.Get("SFTP_PASSWORD"); password != "" {
		auth = append(auth, ssh.Password(password))
	}
	if key := ctx.Secrets.Get("SFTP_PRIVATE_KEY"); key != "" {
		signer, e := ssh.ParsePrivateKey([]byte(key))
		if e != nil {
			return nil, errors.New("invalid_private_key")
		}
		auth = append(auth, ssh.PublicKeys(signer))
	}
	if len(auth) == 0 {
		return nil, errors.New("missing_credential")
	}
	config := &ssh.ClientConfig{User: username, Auth: auth, Timeout: 30 * time.Second, HostKeyCallback: func(_ string, _ net.Addr, key ssh.PublicKey) error {
		if ssh.FingerprintSHA256(key) != fingerprint {
			return errors.New("host_fingerprint_mismatch")
		}
		return nil
	}}
	address := net.JoinHostPort(stringValue(c["host"]), strconv.Itoa(limit(c, "port", 22, 65535)))
	dialer := net.Dialer{Timeout: 30 * time.Second}
	conn, e := dialer.DialContext(ctx.Context, "tcp", address)
	if e != nil {
		return nil, errors.New("sftp_connection_failed")
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(120 * time.Second))
	sshConn, chans, reqs, e := ssh.NewClientConn(conn, address, config)
	if e != nil {
		return nil, errors.New("sftp_connection_or_host_key_failed")
	}
	transport := ssh.NewClient(sshConn, chans, reqs)
	defer transport.Close()
	client, e := sftp.NewClient(transport)
	if e != nil {
		return nil, errors.New("sftp_operation_failed")
	}
	defer client.Close()
	base, e := client.RealPath(stringValue(c["base_directory"]))
	if e != nil || base == "" {
		return nil, errors.New("invalid_base_directory")
	}
	inside := func(p string) bool { return p == base || strings.HasPrefix(p, strings.TrimSuffix(base, "/")+"/") }
	directory, e := client.RealPath(path.Join(base, textValue(c, "path", ".")))
	if e != nil || !inside(directory) {
		return nil, errors.New("path_outside_base")
	}
	entries, e := client.ReadDirContext(ctx.Context, directory)
	if e != nil {
		return nil, errors.New("sftp_list_failed")
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	files := []any{}
	contents := []any{}
	total := 0
	var checkpoint any
	maximum := limit(c, "max_files", 500, 5000)
	maxFile := limit(c, "max_file_bytes", 1048576, 8388608)
	maxTotal := limit(c, "max_total_bytes", 4194304, 16777216)
	start := stringValue(c["start_at"])
	startFound := start == ""
	requested := map[string]bool{}
	for _, v := range array(c["read_paths"]) {
		requested[stringValue(v)] = true
	}
	result := func() object {
		return object{"files": files, "contents": contents, "summary": object{"files": len(files), "bytes_read": total}, "partial": checkpoint != nil, "checkpoint": checkpoint}
	}
	for _, entry := range entries {
		if !entry.Mode().IsRegular() || !strings.HasSuffix(entry.Name(), stringValue(c["suffix"])) {
			continue
		}
		resolved, e := client.RealPath(path.Join(directory, entry.Name()))
		if e != nil || !inside(resolved) {
			continue
		}
		relative := strings.TrimPrefix(strings.TrimPrefix(resolved, base), "/")
		if !startFound {
			if relative != start {
				continue
			}
			startFound = true
		}
		mark := object{"start_at": relative}
		if len(files) >= maximum {
			checkpoint = mark
			break
		}
		metadata := object{"path": relative, "size": entry.Size(), "modified_at": entry.ModTime().UTC().Format(time.RFC3339Nano)}
		var content any
		if requested[relative] {
			if entry.Size() > int64(maxFile) {
				content = object{"path": relative, "status": "skipped", "reason": "file_too_large"}
			} else {
				f, e := client.Open(resolved)
				if e != nil {
					return nil, errors.New("sftp_read_failed")
				}
				data, e := io.ReadAll(io.LimitReader(f, int64(maxFile+1)))
				_ = f.Close()
				if e != nil {
					return nil, errors.New("sftp_read_failed")
				}
				if len(data) > maxFile {
					content = object{"path": relative, "status": "skipped", "reason": "file_too_large"}
				} else {
					content = object{"path": relative, "status": "read", "bytes": len(data), "content_base64": base64.StdEncoding.EncodeToString(data)}
				}
			}
		}
		if len(encoded(result()))+len(encoded(metadata))+len(encoded(content))+len(encoded(mark))+128 > maxTotal {
			checkpoint = mark
			break
		}
		files = append(files, metadata)
		if content != nil {
			contents = append(contents, content)
			total += number(mapping(content)["bytes"])
		}
	}
	if !startFound {
		return nil, errors.New("invalid_checkpoint")
	}
	return result(), nil
}
