# ruff: noqa: E501
"""Embedded Go Adapter harness; only standard library dependencies."""

SOURCE = r"""package main

import (
    "context"
    "encoding/json"
    "errors"
    "fmt"
    "io"
    "os"
    "path/filepath"
    "regexp"
    "runtime/debug"
    "strconv"
    "strings"
)

type InputFile struct {
    Ordinal int `json:"ordinal"`
    Path string `json:"path"`
    OriginalName string `json:"originalName"`
    ContentType string `json:"contentType"`
    SizeBytes int64 `json:"sizeBytes"`
    SHA256 string `json:"sha256"`
}
type Secrets struct{}
func (Secrets) Get(key string) string { return os.Getenv("DLR_SECRET_" + key) }
type Logger struct{}
func (Logger) Info(values ...any) { fmt.Println(append([]any{"[INFO]"}, values...)...) }
func (Logger) Warn(values ...any) { fmt.Fprintln(os.Stderr, append([]any{"[WARN]"}, values...)...) }
func (Logger) Error(values ...any) { fmt.Fprintln(os.Stderr, append([]any{"[ERROR]"}, values...)...) }
type Context struct {
    context.Context
    Config map[string]any
    InputFiles []InputFile
    Secrets Secrets
    Logger Logger
}
// DecodeInput converts a JSON input into a user-defined struct without losing integer precision.
func DecodeInput(input any, target any) error {
    data, err := json.Marshal(input)
    if err != nil { return err }
    return json.Unmarshal(data, target)
}
func readJSON(path string, target any) error {
    file, err := os.Open(path)
    if err != nil { return err }
    defer file.Close()
    decoder := json.NewDecoder(file)
    decoder.UseNumber()
    if err := decoder.Decode(target); err != nil { return err }
    var extra any
    if err := decoder.Decode(&extra); err != io.EOF { return errors.New("invalid trailing JSON") }
    return nil
}
func inputFiles(workspace string) ([]InputFile, error) {
    fail := errors.New("DLR_INPUT_ERROR:input_artifact_not_ready")
    match := regexp.MustCompile(`^dlr-exec-([1-9][0-9]*)$`).FindStringSubmatch(filepath.Base(workspace))
    if !filepath.IsAbs(workspace) || match == nil { return nil, fail }
    executionID, err := strconv.ParseInt(match[1], 10, 64)
    if err != nil { return nil, fail }
    inputDir := filepath.Join(workspace, "input")
    manifestPath := filepath.Join(workspace, "input_manifest.json")
    for _, path := range []string{inputDir, manifestPath} {
        info, err := os.Lstat(path)
        if err != nil || info.Mode() & os.ModeSymlink != 0 { return nil, fail }
        if path == inputDir && !info.IsDir() { return nil, fail }
        if path == manifestPath && !info.Mode().IsRegular() { return nil, fail }
    }
    var manifest map[string]json.RawMessage
    if readJSON(manifestPath, &manifest) != nil || len(manifest) != 2 { return nil, fail }
    var manifestID int64
    var entries []map[string]json.RawMessage
    if json.Unmarshal(manifest["execution_id"], &manifestID) != nil || manifestID != executionID ||
        json.Unmarshal(manifest["files"], &entries) != nil || string(manifest["files"]) == "null" || len(entries) > 8 {
        return nil, fail
    }
    files := make([]InputFile, 0, len(entries))
    mountPattern := regexp.MustCompile(`^input-([0-9]{2})(?:\.[a-z0-9]{1,10})?$`)
    hashPattern := regexp.MustCompile(`^[0-9a-f]{64}$`)
    for ordinal, raw := range entries {
        if len(raw) != 7 { return nil, fail }
        var id, index, size int64
        var mount, original, contentType, hash string
        fields := map[string]any{"artifact_id": &id, "ordinal": &index, "mount_name": &mount,
            "original_filename": &original, "content_type": &contentType, "size_bytes": &size, "sha256": &hash}
        for key, target := range fields {
            value, ok := raw[key]
            if !ok || string(value) == "null" || json.Unmarshal(value, target) != nil { return nil, fail }
        }
        parsed := mountPattern.FindStringSubmatch(mount)
        if parsed == nil || id < 1 || index != int64(ordinal) || size < 0 || !hashPattern.MatchString(hash) ||
            strings.ContainsAny(original, "\x00/\\") || original == "" || len(original) > 255 || contentType == "" {
            return nil, fail
        }
        mountOrdinal, _ := strconv.Atoi(parsed[1])
        if mountOrdinal != ordinal { return nil, fail }
        path := filepath.Join(inputDir, mount)
        info, err := os.Lstat(path)
        if err != nil || !info.Mode().IsRegular() { return nil, fail }
        file, err := os.Open(path)
        if err != nil { return nil, fail }
        opened, err := file.Stat()
        file.Close()
        if err != nil || !opened.Mode().IsRegular() || !os.SameFile(info, opened) { return nil, fail }
        files = append(files, InputFile{ordinal, path, original, contentType, size, hash})
    }
    return files, nil
}
func run() (err error) {
    defer func() {
        if recovered := recover(); recovered != nil {
            err = fmt.Errorf("adapter panic: %v\n%s", recovered, debug.Stack())
        }
    }()
    if len(os.Args) != 2 { return errors.New("expected workspace argument") }
    workspace := os.Args[1]
    var input any
    var config map[string]any
    if err = readJSON(filepath.Join(workspace, "input.json"), &input); err != nil { return err }
    if err = readJSON(filepath.Join(workspace, "runtime_config.json"), &config); err != nil { return err }
    files, err := inputFiles(workspace)
    if err != nil { return err }
    ctx := &Context{context.Background(), config, files, Secrets{}, Logger{}}
    output, err := Handle(ctx, input)
    if err != nil { return err }
    encoded, err := json.Marshal(output)
    if err != nil { return fmt.Errorf("adapter output is not JSON-serializable: %w", err) }
    return os.WriteFile(filepath.Join(workspace, "output.json"), encoded, 0600)
}
func main() {
    if err := run(); err != nil { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
}
"""
