import type { AdapterLanguage } from "./types";

/**
 * Read-only examples for the public Worker Context contract.
 *
 * These strings intentionally contain no Control API call, Worker endpoint,
 * editor mutation or Adapter persistence operation.  The only input source
 * is the language-specific Context file collection.
 */
const EXAMPLES: Record<AdapterLanguage, string> = {
  python: `from pathlib import Path


def handle(context, input):
    files = []
    for item in context.input_files:
        files.append({
            "ordinal": item.ordinal,
            "name": item.original_name,
            "content_type": item.content_type,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
            "content": Path(item.path).read_text(encoding="utf-8"),
        })
    return {"input": input, "files": files}
`,
  javascript: `import fs from "node:fs";

export function handle(context, input) {
  const files = context.inputFiles.map((item) => ({
    ordinal: item.ordinal,
    name: item.originalName,
    contentType: item.contentType,
    sizeBytes: item.sizeBytes,
    sha256: item.sha256,
    content: fs.readFileSync(item.path, "utf8"),
  }));
  return { input, files };
}
`,
  typescript: `import fs from "node:fs";

export function handle(context: Context, input: unknown) {
  const files = context.inputFiles.map((item) => ({
    ordinal: item.ordinal,
    name: item.originalName,
    contentType: item.contentType,
    sizeBytes: item.sizeBytes,
    sha256: item.sha256,
    content: fs.readFileSync(item.path, "utf8"),
  }));
  return { input, files };
}
`,
  go: `package main

import "os"

func Handle(ctx *Context, input any) (any, error) {
    files := make([]map[string]any, 0, len(ctx.InputFiles))
    for _, item := range ctx.InputFiles {
        data, err := os.ReadFile(item.Path)
        if err != nil { return nil, err }
        files = append(files, map[string]any{"name": item.OriginalName, "content": string(data), "sha256": item.SHA256})
    }
    return map[string]any{"input": input, "files": files}, nil
}
`,
  java: `import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class Adapter {
    public Object handle(Context context, Object input) throws Exception {
        List<Map<String, Object>> files = new ArrayList<>();
        for (InputFile item : context.inputFiles) {
            Map<String, Object> file = new LinkedHashMap<>();
            file.put("ordinal", item.ordinal);
            file.put("name", item.originalName);
            file.put("contentType", item.contentType);
            file.put("sizeBytes", item.sizeBytes);
            file.put("sha256", item.sha256);
            file.put("content", Files.readString(item.path));
            files.add(file);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("input", input);
        result.put("files", files);
        return result;
    }
}
`,
};

export function managedInputExample(language: AdapterLanguage): string {
  return EXAMPLES[language];
}
