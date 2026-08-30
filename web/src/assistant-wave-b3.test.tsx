/**
 * M5.7 Wave B3: Composer attachment regressions (Issue #80).
 *
 * The Composer surface is built on the official assistant-ui External Store
 * Runtime AttachmentAdapter plus the Composer attachment primitives
 * (AddAttachment / AttachmentDropzone / Attachments). Client-side validation
 * mirrors the stable B2 server bounds (type/ext table, per-file/total/count
 * limits) but never replaces the server checks; server rejections localize
 * through the stable ai_attachment_* codes in zh-CN/en.
 *
 * The B2 wire contract: attachments: [{filename, content_type, data_base64}]
 * frozen into the round snapshot at send time — bounded by the B2 total cap,
 * never rendered, logged or persisted — so Regenerate and the failed-round
 * retry reuse the original files without reading the current Composer.
 *
 * Candidate / Diff / Apply / stale / Secret / late-response / adapter
 * isolation / running gate / a11y / i18n-parity regressions live here and
 * keep the Wave A/B1 contracts unchanged.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import { api, ApiError } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "./i18n";
import type {
  Adapter,
  AiAssistResponse,
  AiAttachmentCapabilities,
  AiCandidate,
} from "./types";

vi.mock("@monaco-editor/react", () => ({
  default: function Editor() {
    return <textarea data-testid="code-editor" readOnly />;
  },
  DiffEditor: function DiffEditor(props: { original?: string; modified?: string }) {
    return (
      <div
        data-testid="diff-editor"
        data-original={props.original ?? ""}
        data-modified={props.modified ?? ""}
      />
    );
  },
  loader: {
    init: () => Promise.resolve({ editor: { setTheme: () => undefined } }),
  },
}));

function makeAdapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 1,
    name: "adapter-a",
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    timeout_seconds: 300,
    runtime_worker_id: 1,
    latest_version_id: 10,
    created_at: "2026-08-11T00:00:00Z",
    updated_at: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

const workingCopy = {
  code: "def handle(context, input):\n    return input\n",
  requirements: "依赖：无",
  runtimeConfigText: "{}",
};

function aiResponse(message: string, candidate: AiCandidate | null): AiAssistResponse {
  return { message, provider: "openai", model: "test-model", candidate };
}

function makeCandidate(overrides: Partial<AiCandidate> = {}): AiCandidate {
  return {
    summary: "方案一",
    code: "def handle(context, input):\n    return \"new\"\n",
    requirements: "依赖：无",
    runtime_config: {},
    required_secret_keys: [],
    ...overrides,
  };
}

const attachmentCapabilities: AiAttachmentCapabilities = {
  limits: {
    max_attachments: 8,
    max_file_bytes: 6 * 1024 * 1024,
    max_total_bytes: 12 * 1024 * 1024,
    max_parsed_chars_per_file: 64 * 1024,
    max_parsed_total_chars: 256 * 1024,
    parse_timeout_seconds: 30,
  },
  supported_content_types: [
    "application/json",
    "application/octet-stream",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/x-yaml",
    "application/xml",
    "application/javascript",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/csv",
    "text/javascript",
    "text/markdown",
    "text/plain",
    "text/x-yaml",
    "text/xml",
  ],
  providers: [
    { provider: "openai", images_native: true, files_native: false },
    { provider: "deepseek", images_native: false, files_native: false },
    { provider: "kimi", images_native: false, files_native: false },
    { provider: "minimax", images_native: false, files_native: false },
    { provider: "custom_openai_compatible", images_native: false, files_native: false },
  ],
};

type PanelProps = ComponentProps<typeof AiAssistantPanel>;

function renderPanel(overrides: Partial<PanelProps> = {}) {
  const base: PanelProps = {
    open: true,
    adapter: makeAdapter(),
    selectedVersionId: 10,
    selectedVersionSeq: 1,
    workingCopy: { ...workingCopy },
    contentReady: true,
    busy: false,
    contextSnippets: [],
    theme: "vs-dark",
    onOpen: vi.fn(),
    onClose: vi.fn(),
    onApply: vi.fn(),
    onRemoveContextSnippet: vi.fn(),
    onClearContextSnippets: vi.fn(),
    ...overrides,
  };
  const view = render(<AiAssistantPanel {...base} />);
  return {
    view,
    rerender: (next: Partial<PanelProps>) =>
      view.rerender(<AiAssistantPanel {...base} {...next} />),
  };
}

function makeFile(name: string, type: string, content = "attachment-body-sentinel"): File {
  return new File([content], name, { type });
}

function makeBigFile(name: string, type: string, bytes: number): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

async function addFiles(...files: File[]) {
  const before = screen.queryAllByTestId("ai-attachment-item").length;
  fireEvent.change(screen.getByTestId("ai-attachment-input"), {
    target: { files },
  });
  await waitFor(() =>
    expect(screen.queryAllByTestId("ai-attachment-item")).toHaveLength(before + files.length),
  );
}

function attachmentPayloadOf(
  mock: { mock: { calls: unknown[][] } },
  index: number,
): {
  message: string;
  attachments?: { filename: string; content_type: string; data_base64: string }[];
  recent_messages: { role: "user" | "assistant"; content: string }[];
} {
  return mock.mock.calls[index][1] as {
    message: string;
    attachments?: { filename: string; content_type: string; data_base64: string }[];
    recent_messages: { role: "user" | "assistant"; content: string }[];
  };
}

beforeEach(() => {
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);
  vi.spyOn(api, "getAiAttachmentCapabilities").mockResolvedValue(attachmentCapabilities);
});

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  vi.restoreAllMocks();
});

describe("selection / drop / delete / clear", () => {
  it("adds image, PDF, DOCX and text/code attachments with accessible names, type and size", async () => {
    const image = makeFile("photo.png", "image/png");
    const pdf = makeFile("spec.pdf", "application/pdf");
    const docx = makeFile(
      "report.docx",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    );
    const text = makeFile("notes.txt", "text/plain");
    const code = makeFile("config.json", "application/json", '{"key": "value"}');
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();

    await addFiles(image, pdf, docx, text, code);
    expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(5);

    // 名称 + 类型 + 大小均展示（可访问：name/title 与 aria-label）。
    const names = screen
      .getAllByTestId("ai-attachment-name")
      .map((node) => node.textContent);
    expect(names).toEqual(["photo.png", "spec.pdf", "report.docx", "notes.txt", "config.json"]);
    const meta = screen
      .getAllByTestId("ai-attachment-meta")
      .map((node) => node.textContent);
    expect(meta[0]).toContain("图片");
    expect(meta[1]).toContain("PDF");
    expect(meta[2]).toContain("DOCX");
    expect(meta[3]).toContain("文本");
    expect(meta[4]).toContain("代码");
    expect(meta[0]).toContain("B"); // 大小标签（字节）
    expect(screen.getAllByTestId("ai-attachment-ready")).toHaveLength(5);
    const removeButtons = screen.getAllByTestId("ai-attachment-remove");
    expect(removeButtons[0].getAttribute("aria-label")).toBe("删除附件 photo.png");
    expect(removeButtons[0].tagName).toBe("BUTTON");

    // 删除单个附件：其余保留。
    fireEvent.click(removeButtons[1]);
    await waitFor(() => expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(4));
    expect(screen.queryByText("spec.pdf")).toBeNull();
    expect(screen.getByText("photo.png")).toBeTruthy();

    // 清空：逐个删除直至为空。
    for (const remove of [...screen.getAllByTestId("ai-attachment-remove")]) {
      fireEvent.click(remove);
    }
    await waitFor(() => expect(screen.queryByTestId("ai-attachment-item")).toBeNull());
  });

  it("adds dropped files through the dropzone", async () => {
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();
    fireEvent.drop(screen.getByTestId("ai-attachment-dropzone"), {
      dataTransfer: { files: [makeFile("a.txt", "text/plain"), makeFile("b.md", "text/markdown")] },
    });
    await waitFor(() => expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(2));
    expect(screen.getByText("a.txt")).toBeTruthy();
    expect(screen.getByText("b.md")).toBeTruthy();
  });

  it("exposes the add button, input and remove controls to keyboard users", async () => {
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();
    const add = screen.getByTestId("ai-attachment-add") as HTMLButtonElement;
    expect(add.tagName).toBe("BUTTON");
    expect(add.getAttribute("aria-label")).toContain("添加附件");
    add.focus();
    expect(document.activeElement).toBe(add);
    expect(add.tabIndex).toBe(0);
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent).toContain("XLS");
    expect(tooltip.textContent).toContain("XLSX");
    expect(tooltip.textContent).not.toContain("不支持 XLS");

    await addFiles(makeFile("k.txt", "text/plain"));
    const remove = screen.getByTestId("ai-attachment-remove") as HTMLButtonElement;
    remove.focus();
    expect(document.activeElement).toBe(remove);
    expect(remove.tabIndex).toBe(0);
    // Enter 由原生 button 激活语义触发（jsdom 不合成激活，真实浏览器覆盖）。
    fireEvent.click(remove);
    await waitFor(() => expect(screen.queryByTestId("ai-attachment-item")).toBeNull());
  });
});

describe("Issue #117 Batch 6: one-line attachment guidance", () => {
  it.each(["zh-CN", "en"] as const)(
    "keeps the complete %s limits and privacy guidance in one visible row while attachment behavior stays stable",
    async (locale) => {
      vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
      await applySystemLocale(locale);
      renderPanel();

      const guidance = await screen.findByTestId("ai-composer-guidance");
      const hint = screen.getByTestId("ai-attachment-hint");
      const privacy = screen.getByTestId("ai-attachment-privacy");
      const expected = locale === "zh-CN"
        ? "最多上传8个附件，单附件最大6MB，请勿上传敏感信息。"
        : "Upload up to 8 attachments, max 6MB each. Do not upload sensitive information.";

      expect(hint.textContent).toBe(expected);
      expect(privacy.textContent).toBe(
        locale === "zh-CN" ? "请勿上传敏感信息。" : "Do not upload sensitive information.",
      );
      expect(hint.getAttribute("aria-label")).toBe(expected);
      expect(hint.getAttribute("title")).toBe(expected);
      expect(guidance.textContent).toBe(expected);
      expect(guidance.getAttribute("aria-label")).toBe(expected);
      expect(guidance.querySelector("br")).toBeNull();
      expect(guidance.querySelectorAll(":scope > div, :scope > p")).toHaveLength(0);

      // Valid picker upload still adds one row.
      fireEvent.change(screen.getByTestId("ai-attachment-input"), {
        target: { files: [makeFile("picker.txt", "text/plain")] },
      });
      await waitFor(() => expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(1));

      // Valid drag and drop still adds another row.
      fireEvent.drop(screen.getByTestId("ai-attachment-dropzone"), {
        dataTransfer: { files: [makeFile("drop.txt", "text/plain")] },
      });
      await waitFor(() => expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(2));

      // Existing validation still rejects an unsupported type without adding a row.
      fireEvent.change(screen.getByTestId("ai-attachment-input"), {
        target: { files: [makeFile("blocked.exe", "application/x-msdownload")] },
      });
      await screen.findByTestId("ai-attachment-error");
      expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(2);

      // Existing accessible remove behavior still removes only the selected row.
      fireEvent.click(screen.getAllByTestId("ai-attachment-remove")[0]);
      await waitFor(() => expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(1));
      expect(screen.getByTestId("ai-attachment-name").textContent).toContain("drop.txt");
    },
  );
});

describe("client-side bounds mirror the B2 contract", () => {
  it("never attaches pasted files (paste bypass closed)", async () => {
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();
    fireEvent.paste(screen.getByTestId("ai-message-input"), {
      clipboardData: { files: [makeFile("pasted.txt", "text/plain"), makeFile("pasted.png", "image/png")] },
    });
    await waitFor(() => {
      // Paste must not create attachment rows (validated picker/dropzone
      // are the only entry points) and must not raise an error.
      expect(screen.queryByTestId("ai-attachment-item")).toBeNull();
    });
    expect(screen.queryByTestId("ai-attachment-error")).toBeNull();
  });

  it("keeps the full draft when attachment resolution fails (no data loss)", async () => {
    const file = makeFile("notes.txt", "text/plain", "draft-body");
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValue(aiResponse("回复", null));
    renderPanel();
    await addFiles(file);
    const input = screen.getByTestId("ai-message-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "请分析" } });

    // Simulate a browser file-read failure during body resolution (real
    // browsers surface reader.error as a DOMException).
    const readSpy = vi
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        Object.defineProperty(this, "error", {
          value: new DOMException("read failed", "NotFoundError"),
          configurable: true,
        });
        this.onerror?.(new ProgressEvent("error") as ProgressEvent<FileReader>);
      });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("附件读取失败");
    // The draft is fully preserved: text and attachment row stay put, and
    // nothing was sent.
    expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toBe("请分析");
    expect(screen.queryAllByTestId("ai-attachment-item")).toHaveLength(1);
    expect(assistAdapter).not.toHaveBeenCalled();

    // After the transient failure clears, the same draft sends successfully.
    readSpy.mockRestore();
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByText("回复");
    expect(assistAdapter).toHaveBeenCalledTimes(1);
    expect(attachmentPayloadOf(assistAdapter, 0).attachments?.[0]?.data_base64).toBe(
      btoa("draft-body"),
    );
  });

  it("follows a narrowed server capability table for the runtime accept check", async () => {
    vi.spyOn(api, "getAiAttachmentCapabilities").mockResolvedValue({
      ...attachmentCapabilities,
      supported_content_types: ["text/plain"],
    });
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();
    await screen.findByTestId("ai-attachment-hint");

    // text/plain is still accepted...
    fireEvent.drop(screen.getByTestId("ai-attachment-dropzone"), {
      dataTransfer: { files: [makeFile("ok.txt", "text/plain")] },
    });
    await waitFor(() => expect(screen.queryAllByTestId("ai-attachment-item")).toHaveLength(1));
    // ...but application/json is outside the narrowed table: rejected with
    // actionable copy and never added as a row.
    fireEvent.drop(screen.getByTestId("ai-attachment-dropzone"), {
      dataTransfer: { files: [makeFile("config.json", "application/json")] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("不支持的文件类型");
    expect(screen.queryAllByTestId("ai-attachment-item")).toHaveLength(1);
  });

  it("serializes double-clicks while attachment bodies are being resolved", async () => {
    const file = makeFile("notes.txt", "text/plain", "race-body");
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValue(aiResponse("回复", null));
    renderPanel();
    await addFiles(file);
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "请分析" },
    });

    // Hold the file read open so the send stays in the resolution window
    // (draft still visible, `sending` still false).
    let resolveRead: ((dataUrl: string) => void) | undefined;
    const pendingRead = new Promise<string>((resolve) => {
      resolveRead = resolve;
    });
    const readSpy = vi
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        // eslint-disable-next-line @typescript-eslint/no-this-alias -- test mock needs the FileReader instance inside the deferred callback
        const target = this;
        void pendingRead.then((dataUrl) => {
          Object.defineProperty(target, "result", { value: dataUrl, configurable: true });
          target.onload?.(new ProgressEvent("load") as ProgressEvent<FileReader>);
        });
      });

    fireEvent.click(screen.getByTestId("ai-send"));
    fireEvent.click(screen.getByTestId("ai-send"));
    fireEvent.click(screen.getByTestId("ai-send"));

    await act(async () => {
      resolveRead?.("data:text/plain;base64,cmFjZS1ib2R5");
      await pendingRead;
    });
    await screen.findByText("回复");

    // Exactly one request and one user message — the re-entry guard drops
    // the duplicate clicks instead of duplicating the round.
    expect(assistAdapter).toHaveBeenCalledTimes(1);
    expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
    readSpy.mockRestore();
  });

  it("keeps the draft when the runtime config is invalid (consumption happens after freezing)", async () => {
    const file = makeFile("notes.txt", "text/plain", "draft-body");
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValue(aiResponse("回复", null));
    renderPanel({
      workingCopy: { ...workingCopy, runtimeConfigText: "not-json{" },
    });
    await addFiles(file);
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "请分析" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));

    await screen.findByTestId("ai-panel-error");
    expect(screen.getByTestId("ai-panel-error").textContent).toContain("运行参数");
    // The draft is preserved: text and attachment row stay put, nothing sent.
    expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toBe("请分析");
    expect(screen.queryAllByTestId("ai-attachment-item")).toHaveLength(1);
    expect(assistAdapter).not.toHaveBeenCalled();
  });

  it("rejects oversized, count-exceeded and total-exceeded files with actionable localized copy", async () => {
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();

    // 单文件超限（6 MiB + 1 B）。
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [makeBigFile("big.txt", "text/plain", 6 * 1024 * 1024 + 1)] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("6 MiB");
    expect(screen.queryByTestId("ai-attachment-item")).toBeNull();

    // 数量上限：8 个合法文件 + 第 9 个被拒。
    for (let index = 0; index < 8; index += 1) {
      await addFiles(makeFile(`f${index}.txt`, "text/plain"));
    }
    expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(8);
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [makeFile("f8.txt", "text/plain")] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("8 个");
    expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(8);

    // 清空后用测试能力覆盖值验证总大小错误：两个 5 MiB 合法，再拖入 3 MiB 超过 12 MiB。
    for (const remove of [...screen.getAllByTestId("ai-attachment-remove")]) {
      fireEvent.click(remove);
    }
    await waitFor(() => expect(screen.queryByTestId("ai-attachment-item")).toBeNull());
    await addFiles(
      makeBigFile("m1.txt", "text/plain", 5 * 1024 * 1024),
      makeBigFile("m2.txt", "text/plain", 5 * 1024 * 1024),
    );
    fireEvent.drop(screen.getByTestId("ai-attachment-dropzone"), {
      dataTransfer: { files: [makeBigFile("m3.txt", "text/plain", 3 * 1024 * 1024)] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("12 MiB");
    expect(screen.getAllByTestId("ai-attachment-item")).toHaveLength(2);
  });

  it("rejects unsupported, fake/mistyped and path-injection filenames without adding rows", async () => {
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();

    // 类型不支持（.exe 不在 B2 白名单）。
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [makeFile("tool.exe", "application/x-msdownload")] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("不支持的文件类型");
    expect(screen.queryByTestId("ai-attachment-item")).toBeNull();

    // 伪类型：PDF 扩展名 + text/plain 声明 → MIME/扩展名不一致。
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [makeFile("fake.pdf", "text/plain")] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("不支持的文件类型");

    // 文件名注入：路径分隔符 / 隐藏文件。
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [makeFile("../evil.txt", "text/plain")] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("文件名无效");
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [makeFile(".hidden.txt", "text/plain")] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("文件名无效");

    // 空文件：服务端以 ai_attachment_invalid 拒绝，客户端提前给出可行动文案。
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [new File([], "empty.txt", { type: "text/plain" })] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("文件内容为空");
    expect(screen.queryByTestId("ai-attachment-item")).toBeNull();

    // 成功添加后错误提示清除。
    await addFiles(makeFile("ok.txt", "text/plain"));
    expect(screen.queryByTestId("ai-attachment-error")).toBeNull();
  });
});

describe("stable server error codes localize zh-CN/en", () => {
  const codes: [string, number, string][] = [
    ["ai_attachment_invalid", 422, "附件数据无效"],
    ["ai_attachment_filename_invalid", 422, "附件文件名无效"],
    ["ai_attachment_type_unsupported", 422, "附件类型不支持"],
    ["ai_attachment_too_large", 422, "超过单文件大小上限"],
    ["ai_attachment_total_too_large", 422, "总大小超过上限"],
    ["ai_attachment_count_exceeded", 422, "附件数量超过上限"],
    ["ai_attachment_image_unsupported", 422, "不支持图片输入"],
    ["ai_attachment_parse_failed", 422, "附件解析失败"],
    ["ai_attachment_no_text", 422, "没有可提取的文本层"],
    ["ai_attachment_unsafe_archive", 422, "附件内容不安全"],
    ["ai_attachment_parse_timeout", 504, "附件解析超时"],
  ];

  it.each(codes)(
    "localizes the stable %s error without echoing upstream text or base64",
    async (code, status, expectedZh) => {
      const body = "SENSITIVE-BASE64-BODY-SHOULD-NEVER-REACH-THE-UI";
      const upstreamMessage = "upstream english diagnostic must never surface";
      const assistAdapter = vi
        .spyOn(api, "assistAdapter")
        .mockRejectedValue(new ApiError(status, code, upstreamMessage));
      renderPanel();
      await addFiles(makeFile("notes.txt", "text/plain", body));
      fireEvent.change(screen.getByTestId("ai-message-input"), {
        target: { value: "请分析" },
      });
      fireEvent.click(screen.getByTestId("ai-send"));

      await screen.findByTestId("ai-panel-error");
      const error = screen.getByTestId("ai-panel-error").textContent ?? "";
      expect(error).toContain(expectedZh);
      expect(error).toContain(code);
      // 不回显 base64、附件原文与上游英文诊断。
      expect(error).not.toContain(body);
      expect(error).not.toContain("SENSITIVE-BASE64");
      expect(error).not.toContain(upstreamMessage);
      expect(document.body.textContent).not.toContain(body);
      // 失败后附件轮次保留可重试。
      expect(screen.getByTestId("ai-retry").textContent).toBe("重试");
      // 请求载荷按 B2 契约发送（base64 只存在于请求与冻结快照）。
      expect(assistAdapter).toHaveBeenCalledTimes(1);
      const payload = attachmentPayloadOf(assistAdapter, 0);
      expect(payload.attachments?.[0]?.data_base64).toBeDefined();
      expect(payload.recent_messages).not.toContain(body);
    },
  );

  it("localizes attachment errors in English with the stable code", async () => {
    await applySystemLocale("en");
    vi.spyOn(api, "assistAdapter").mockRejectedValue(
      new ApiError(422, "ai_attachment_image_unsupported", "upstream english"),
    );
    renderPanel();
    await addFiles(makeFile("photo.png", "image/png"));
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "Look at the image" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByTestId("ai-panel-error");

    expect(screen.getByTestId("ai-panel-error").textContent).toContain(
      "does not support image input",
    );
    expect(screen.getByTestId("ai-panel-error").textContent).toContain(
      "Error code: ai_attachment_image_unsupported",
    );
    expect(document.body.textContent).not.toContain("upstream english");
    expect(screen.getByTestId("ai-retry").textContent).toBe("Retry");
  });
});

describe("wire contract and privacy", () => {
  it("sends the B2 attachment payload and never leaks base64/original text into recent_messages or the DOM", async () => {
    const secretBody = "TOP-SECRET-ATTACHMENT-BODY";
    const file = makeFile("secret.txt", "text/plain", secretBody);
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValue(aiResponse("已分析附件", null));
    renderPanel();

    await addFiles(file);
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "请分析附件" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByText("已分析附件");

    const payload = attachmentPayloadOf(assistAdapter, 0);
    expect(payload.message).toBe("请分析附件");
    expect(payload.attachments).toHaveLength(1);
    expect(payload.attachments?.[0]).toEqual({
      filename: "secret.txt",
      content_type: "text/plain",
      data_base64: btoa(secretBody),
    });
    // recent_messages 只含可见 user/assistant 文本，绝不夹带附件。
    for (const recent of payload.recent_messages) {
      expect(recent).toHaveProperty("role");
      expect(recent).toHaveProperty("content");
      expect(Object.keys(recent).sort()).toEqual(["content", "role"]);
    }
    expect(JSON.stringify(payload.recent_messages)).not.toContain(secretBody);
    expect(JSON.stringify(payload.recent_messages)).not.toContain("data_base64");
    // 成功清 Composer：附件行与输入均清空。
    await waitFor(() => expect(screen.queryByTestId("ai-attachment-item")).toBeNull());
    expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).value).toBe("");
    // base64 / 原文不进 DOM。
    expect(document.body.textContent).not.toContain(secretBody);
    expect(document.body.textContent).not.toContain(btoa(secretBody));
  });

  it("never creates object URLs (nothing to leak on success/failure/remove/switch)", async () => {
    // jsdom has no URL.createObjectURL; define stubs first so the spies pin
    // that DLR never creates or revokes any object URL.
    if (typeof URL.createObjectURL !== "function") {
      URL.createObjectURL = () => "blob:mock";
    }
    if (typeof URL.revokeObjectURL !== "function") {
      URL.revokeObjectURL = () => {};
    }
    const createObjectURL = vi.spyOn(URL, "createObjectURL");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL");
    vi.spyOn(api, "assistAdapter").mockRejectedValue(
      new ApiError(502, "ai_provider_unreachable", "unreachable"),
    );
    const panel = renderPanel();

    await addFiles(makeFile("a.txt", "text/plain"), makeFile("b.png", "image/png"));
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "发送" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByTestId("ai-panel-error");
    // 失败：无任何对象 URL 创建。
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    // 删除与 Adapter 切换同样不产生对象 URL。
    panel.rerender({ adapter: makeAdapter({ id: 2, name: "adapter-b" }) });
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });
});

describe("Regenerate / retry semantics with attachments", () => {
  it("a failed send keeps the round retryable with the frozen attachments and no duplicate user message", async () => {
    const file = makeFile("plan.txt", "text/plain", "frozen-plan");
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockRejectedValueOnce(new ApiError(502, "ai_provider_unreachable", "unreachable"))
      .mockResolvedValueOnce(aiResponse("重试成功", null));
    renderPanel();

    await addFiles(file);
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "分析计划" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByTestId("ai-panel-error");

    // 失败轮：user 消息保留、Composer 清空、出现“重试”入口。
    expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
    expect(screen.queryByTestId("ai-attachment-item")).toBeNull();
    const retry = screen.getByTestId("ai-retry") as HTMLButtonElement;
    expect(retry.textContent).toBe("重试");
    expect(retry.getAttribute("aria-label")).toBe(
      "重试该轮请求（复用发送时的原始代码、上下文与附件）",
    );

    fireEvent.click(retry);
    await screen.findByText("重试成功");

    // 重试复用冻结快照（含附件），不重复 user 消息，不读取当前 Composer。
    expect(assistAdapter).toHaveBeenCalledTimes(2);
    const first = attachmentPayloadOf(assistAdapter, 0);
    const second = attachmentPayloadOf(assistAdapter, 1);
    expect(second.message).toBe("分析计划");
    expect(second.attachments).toEqual(first.attachments);
    expect(second.attachments?.[0]?.data_base64).toBe(btoa("frozen-plan"));
    expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
    expect(screen.getAllByTestId("ai-message-assistant")).toHaveLength(1);
    // 成功后“重试”入口消失，恢复标准 Regenerate。
    expect(screen.queryByTestId("ai-retry")).toBeNull();
    expect(screen.getByTestId("ai-regenerate")).toBeTruthy();
  });

  it("regenerate reuses the original round attachments and ignores the current Composer attachments", async () => {
    const original = makeFile("original.txt", "text/plain", "original-body");
    const later = makeFile("later.txt", "text/plain", "later-body");
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValueOnce(aiResponse("第一轮回复", null))
      .mockResolvedValueOnce(aiResponse("重新生成回复", null));
    renderPanel();

    await addFiles(original);
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "第一轮" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByText("第一轮回复");
    const first = attachmentPayloadOf(assistAdapter, 0);
    expect(first.attachments).toHaveLength(1);
    expect(first.attachments?.[0]?.data_base64).toBe(btoa("original-body"));

    // 发送后在 Composer 中新增附件（不发送）：Regenerate 不得读取它。
    await addFiles(later);
    fireEvent.click(screen.getByTestId("ai-regenerate"));
    await screen.findByText("重新生成回复");

    const second = attachmentPayloadOf(assistAdapter, 1);
    expect(second.attachments).toHaveLength(1);
    expect(second.attachments?.[0]?.data_base64).toBe(btoa("original-body"));
    expect(JSON.stringify(second)).not.toContain("later-body");
    // 当前 Composer 附件不被消费，仍保留。
    expect(screen.getByText("later.txt")).toBeTruthy();
    // 无重复 user 消息。
    expect(screen.getAllByTestId("ai-message-user")).toHaveLength(1);
  });

  it("sends attachments through the Enter path (runtime composer send) with the same wire contract", async () => {
    const file = makeFile("enter.txt", "text/plain", "enter-body");
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValue(aiResponse("回车回复", null));
    renderPanel();
    await addFiles(file);
    const input = screen.getByTestId("ai-message-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "回车带附件" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await screen.findByText("回车回复");

    const payload = attachmentPayloadOf(assistAdapter, 0);
    expect(payload.message).toBe("回车带附件");
    expect(payload.attachments?.[0]?.data_base64).toBe(btoa("enter-body"));
    await waitFor(() => expect(screen.queryByTestId("ai-attachment-item")).toBeNull());
  });
});

describe("Candidate / Diff / Apply / Secret / stale / late-response regressions with attachments", () => {
  it("keeps Candidate → Diff → Apply and Secret warnings on an attachment round", async () => {
    const onApply = vi.fn();
    const candidate = makeCandidate({ required_secret_keys: ["DB_PASS"] });
    vi.spyOn(api, "listAdapterBindings").mockResolvedValue([
      { env_key: "DB_PASS", credential_id: 1, field: "token" },
    ]);
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("已生成", candidate));
    renderPanel({ onApply });

    await addFiles(makeFile("code.txt", "text/plain", "code-body"));
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "带附件生成" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByTestId("ai-candidate-summary");
    expect(screen.queryByTestId("ai-missing-secret-keys")).toBeNull(); // 已绑定

    fireEvent.click(screen.getByTestId("ai-view-diff"));
    await screen.findByTestId("version-diff");
    expect(screen.getByTestId("diff-editor").getAttribute("data-original")).toBe(
      workingCopy.code,
    );
    fireEvent.click(screen.getByTestId("diff-apply-candidate"));
    expect(onApply).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByTestId("version-diff")).toBeNull());
    await screen.findByTestId("ai-candidate-applied");
  });

  it("keeps candidate=null rounds attachment-safe and turns stale after editor changes", async () => {
    const candidate = makeCandidate();
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValueOnce(aiResponse("普通回复", null))
      .mockResolvedValueOnce(aiResponse("候选回复", candidate));
    const panel = renderPanel();
    await addFiles(makeFile("a.txt", "text/plain"));
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "问题一" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByText("普通回复");
    expect(screen.queryByTestId("ai-candidate")).toBeNull();

    // 编辑器变化后 Regenerate：新 Candidate 相对当前编辑器为 stale。
    panel.rerender({ workingCopy: { ...workingCopy, code: "edited-after-send\n" } });
    fireEvent.click(screen.getByTestId("ai-regenerate"));
    await screen.findByText("候选回复");
    await screen.findByTestId("ai-candidate-stale");
    expect(assistAdapter).toHaveBeenCalledTimes(2);
  });

  it("drops a late attachment-round response after the panel unmounted", async () => {
    let resolveAssist: ((response: AiAssistResponse) => void) | undefined;
    const pendingAssist = new Promise<AiAssistResponse>((resolve) => {
      resolveAssist = resolve;
    });
    vi.spyOn(api, "assistAdapter").mockImplementationOnce(() => pendingAssist);
    const { view } = renderPanel();
    await addFiles(makeFile("late.txt", "text/plain"));
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "慢请求" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByTestId("ai-loading");
    view.unmount();

    await act(async () => {
      resolveAssist?.({ message: "迟到回复", provider: "openai", model: "m", candidate: null });
      await pendingAssist;
    });
    expect(screen.queryByText("迟到回复")).toBeNull();
  });

  it("clears composer attachments and releases historical bodies on Adapter switch", async () => {
    const file = makeFile("switch.txt", "text/plain", "switch-body");
    const assistAdapter = vi
      .spyOn(api, "assistAdapter")
      .mockResolvedValue(aiResponse("A 回复", null));
    const panel = renderPanel();
    await addFiles(file);
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "A 问题" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByText("A 回复");
    expect(attachmentPayloadOf(assistAdapter, 0).attachments).toHaveLength(1);

    // 切换 Adapter：Composer 附件清空，历史快照附件体释放。
    panel.rerender({ adapter: makeAdapter({ id: 2, name: "adapter-b" }) });
    await waitFor(() => expect(screen.queryByTestId("ai-attachment-item")).toBeNull());
    // Regenerate 不跨 Adapter：不发请求（round 的 adapter 守卫）。
    const regenerate = screen.getByTestId("ai-regenerate") as HTMLButtonElement;
    fireEvent.click(regenerate);
    await waitFor(() => expect(assistAdapter).toHaveBeenCalledTimes(1));
    expect(document.body.textContent).not.toContain("switch-body");
  });
});

describe("i18n parity and width gates", () => {
  it("shows zh-CN copy, switches live to English, and keeps key parity", async () => {
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();
    await addFiles(makeFile("a.txt", "text/plain"));
    expect(screen.getByTestId("ai-attachment-add").textContent).toBe("添加附件");
    expect(screen.getByTestId("ai-attachment-ready").textContent).toBe("已就绪");
    expect(screen.getByTestId("ai-attachment-hint").textContent).toContain("最多上传8个附件");
    expect(screen.getByTestId("ai-attachment-privacy").textContent).toContain("敏感信息");

    await applySystemLocale("en");
    await waitFor(() => {
      expect(screen.getByTestId("ai-attachment-add").textContent).toBe("Attach files");
    });
    expect(screen.getByTestId("ai-attachment-ready").textContent).toBe("Ready");
    expect(screen.getByTestId("ai-attachment-hint").textContent).toContain("Upload up to 8");
    expect(screen.getByTestId("ai-attachment-privacy").textContent).toContain(
      "sensitive information",
    );
    expect(
      screen.getByTestId("ai-attachment-remove").getAttribute("aria-label"),
    ).toBe("Remove attachment a.txt");
    expect(screen.queryByText("assistant.")).toBeNull();
  });

  it("renders the attachment composer at every tracked width with a long filename", async () => {
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    const longName = `${"very-long-filename-".repeat(8)}notes.txt`;
    for (const width of [1280, 1440, 1680, 1920]) {
      Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
      const { view } = renderPanel();
      await addFiles(makeFile(longName, "text/plain"));
      const name = screen.getByTestId("ai-attachment-name");
      expect(name.textContent).toBe(longName);
      expect(name.getAttribute("title")).toBe(longName);
      expect(screen.getByTestId("ai-attachment-add")).toBeTruthy();
      expect(screen.getByTestId("ai-message-input")).toBeTruthy();
      expect(document.body.textContent).not.toContain("assistant.");
      view.unmount();
    }
    Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true });
  });

  it("falls back to the canonical B2 bounds when the capability endpoint is unreachable", async () => {
    vi.spyOn(api, "getAiAttachmentCapabilities").mockRejectedValue(
      new ApiError(0, "network_error", "Control is unreachable"),
    );
    vi.spyOn(api, "assistAdapter").mockResolvedValue(aiResponse("回复", null));
    renderPanel();

    // 端点失败后仍按权威默认上限工作：合法文件可添加，超限文件被拒。
    await addFiles(makeFile("ok.txt", "text/plain"));
    expect(screen.getByTestId("ai-attachment-hint").textContent).toContain("6MB");
    fireEvent.change(screen.getByTestId("ai-attachment-input"), {
      target: { files: [makeBigFile("big.txt", "text/plain", 6 * 1024 * 1024 + 1)] },
    });
    await screen.findByTestId("ai-attachment-error");
    expect(screen.getByTestId("ai-attachment-error").textContent).toContain("6 MiB");
  });
});
