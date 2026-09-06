import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, clearTemplateVariantCache } from "../api";
import { i18n } from "../i18n";
import type {
  TemplateScenarioDetail,
  TemplateScenarioListResponse,
  TemplateScenarioSummary,
  TemplateTheme,
  TemplateVariant,
} from "../types";
import TemplateGalleryPage, { type TemplateCopyRequest } from "./TemplateGalleryPage";

vi.mock("@monaco-editor/react", () => ({
  default: ({ value, language, options }: { value: string; language: string; options?: { readOnly?: boolean } }) => (
    <textarea
      data-testid="template-monaco"
      data-language={language}
      value={value}
      readOnly={options?.readOnly}
      onChange={() => undefined}
    />
  ),
}));

const themes: TemplateTheme[] = [
  ["cloud-cmdb", "云与 CMDB", "Cloud & CMDB", 7],
  ["api-events", "API 与事件", "API & Events", 3],
  ["file-data", "文件与数据", "Files & Data", 3],
  ["databases", "数据库", "Databases", 2],
  ["storage-transfer", "存储与传输", "Storage & Transfer", 2],
].map(([slug, zh, en, count], index) => ({
  slug: String(slug),
  name: { "zh-CN": String(zh), en: String(en) },
  description: { "zh-CN": `${zh}说明`, en: `${en} description` },
  sort_order: (index + 1) * 10,
  scenario_count: Number(count),
}));

function scenario(overrides: Partial<TemplateScenarioSummary> = {}): TemplateScenarioSummary {
  return {
    slug: "rest-single-request",
    theme_slug: "api-events",
    title: { "zh-CN": "REST 单次请求", en: "Single REST request" },
    summary: { "zh-CN": "调用一个有界 REST API。", en: "Call one bounded REST API." },
    vendor: "DLR",
    adapter_type: "task",
    protocols: ["HTTP", "JSON"],
    tags: ["REST", "API"],
    logo_key: "rest-request",
    template_version: "1.0.0",
    updated_at: "2026-09-05",
    variants: [
      { language: "python", available: true },
      { language: "javascript", available: true },
      { language: "java", available: true },
    ],
    ...overrides,
  };
}

function detail(overrides: Partial<TemplateScenarioDetail> = {}): TemplateScenarioDetail {
  return {
    ...scenario(),
    details: { "zh-CN": "用于受控的单次请求。", en: "For a controlled single request." },
    ...overrides,
  };
}

function variant(language: "python" | "javascript" | "java" = "python"): TemplateVariant {
  return {
    scenario_slug: "rest-single-request",
    theme_slug: "api-events",
    title: { "zh-CN": "REST 单次请求", en: "Single REST request" },
    language,
    adapter_type: "task",
    template_version: "1.0.0",
    code: `${language} recipe source`,
    requirements: `${language}-dependency==1.0.0`,
    input_skeleton: { fixture: `${language}-input` },
    output_example: { fixture: `${language}-result` },
    runtime_config: { fixture: `${language}-runtime-config` },
  };
}

function listResponse(items: TemplateScenarioSummary[]): TemplateScenarioListResponse {
  return { items, page: 1, page_size: 12, total: items.length };
}

beforeEach(async () => {
  clearTemplateVariantCache();
  await i18n.changeLanguage("zh-CN");
  vi.spyOn(api, "listTemplateThemes").mockResolvedValue(themes);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("defaults to All, lists available languages, and omits maturity", async () => {
  vi.spyOn(api, "listTemplateScenarios").mockResolvedValue(listResponse([scenario()]));

  render(
    <TemplateGalleryPage
      scenarioSlug={null}
      busy={false}
      onOpenScenario={() => undefined}
      onBackToGallery={() => undefined}
      onInstantiate={async () => true}
    />,
  );

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByRole("heading", { name: "REST 单次请求" })).toBeTruthy();
  expect(screen.getAllByRole("tab")).toHaveLength(6);
  expect(screen.getByRole("tab", { name: "全部 · 17" }).getAttribute("aria-selected")).toBe("true");
  expect(api.listTemplateScenarios).toHaveBeenCalledWith(expect.objectContaining({ theme: undefined, page_size: 12 }), expect.any(AbortSignal));
  expect(screen.queryByText("成熟度")).toBeNull();
  expect(screen.getByText("Python")).toBeTruthy();
  expect(screen.getByText("JavaScript")).toBeTruthy();
  expect(screen.getByText("Java")).toBeTruthy();
  expect(document.querySelectorAll("img, image")).toHaveLength(0);
  const activeThemePanel = screen.getByRole("tabpanel");
  expect(within(activeThemePanel).getByLabelText("模板筛选")).toBeTruthy();
  expect(within(activeThemePanel).getByRole("heading", { name: "REST 单次请求" })).toBeTruthy();
});

it("does not re-enter loading when the unchanged initial search debounce settles", async () => {
  vi.useFakeTimers();
  const listSpy = vi.spyOn(api, "listTemplateScenarios").mockResolvedValue(listResponse([scenario()]));

  render(
    <TemplateGalleryPage
      scenarioSlug={null}
      busy={false}
      onOpenScenario={() => undefined}
      onBackToGallery={() => undefined}
      onInstantiate={async () => true}
    />,
  );

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByRole("heading", { name: "REST 单次请求" })).toBeTruthy();
  await act(async () => vi.advanceTimersByTimeAsync(SEARCH_TEST_DELAY));
  expect(screen.getByRole("heading", { name: "REST 单次请求" })).toBeTruthy();
  expect(listSpy.mock.calls.filter(([query]) => query.page_size === 12)).toHaveLength(1);
});

it("debounces search and prevents an older response from replacing the newest result", async () => {
  vi.useFakeTimers();
  const pending: Array<{
    query: string;
    resolve: (response: TemplateScenarioListResponse) => void;
  }> = [];
  vi.spyOn(api, "listTemplateScenarios").mockImplementation((query) => {
    if (query.page_size === 48) return Promise.resolve(listResponse([scenario()]));
    return new Promise((resolve) => {
      pending.push({ query: query.q ?? "", resolve });
    });
  });

  render(
    <TemplateGalleryPage
      scenarioSlug={null}
      busy={false}
      onOpenScenario={() => undefined}
      onBackToGallery={() => undefined}
      onInstantiate={async () => true}
    />,
  );
  await act(async () => { await Promise.resolve(); });
  expect(pending).toHaveLength(1);
  await act(async () => pending[0].resolve(listResponse([scenario()])));

  const search = screen.getByRole("textbox", { name: "搜索模板" });
  fireEvent.change(search, { target: { value: "old" } });
  await act(async () => vi.advanceTimersByTimeAsync(SEARCH_TEST_DELAY));
  fireEvent.change(search, { target: { value: "new" } });
  await act(async () => vi.advanceTimersByTimeAsync(SEARCH_TEST_DELAY));
  expect(pending.map((entry) => entry.query)).toEqual(["", "old", "new"]);

  await act(async () => pending[2].resolve(listResponse([scenario({ slug: "new-result", title: { "zh-CN": "新结果", en: "New result" } })])));
  expect(screen.getByRole("heading", { name: "新结果" })).toBeTruthy();
  await act(async () => pending[1].resolve(listResponse([scenario({ slug: "old-result", title: { "zh-CN": "旧结果", en: "Old result" } })])));
  expect(screen.queryByRole("heading", { name: "旧结果" })).toBeNull();
  expect(screen.getByRole("heading", { name: "新结果" })).toBeTruthy();
});

it("preloads complete vendor and protocol facets independently of visible pagination", async () => {
  const pageTwoOnly = scenario({
    slug: "page-two-only",
    theme_slug: "databases",
    vendor: "Page Two Vendor",
    protocols: ["RARE-PROTOCOL"],
  });
  const listSpy = vi.spyOn(api, "listTemplateScenarios").mockImplementation(async (query) => {
    if (query.page_size === 48 && query.theme === "databases") {
      return listResponse([pageTwoOnly]);
    }
    return listResponse([scenario()]);
  });

  render(
    <TemplateGalleryPage
      scenarioSlug={null}
      busy={false}
      onOpenScenario={() => undefined}
      onBackToGallery={() => undefined}
      onInstantiate={async () => true}
    />,
  );

  await screen.findByRole("heading", { name: "REST 单次请求" });
  await waitFor(() => expect(listSpy.mock.calls.filter(([query]) => query.page_size === 48)).toHaveLength(5));
  expect(listSpy.mock.calls.filter(([query]) => query.page_size === 48).map(([query]) => query.theme).sort())
    .toEqual(themes.map((theme) => theme.slug).sort());

  const protocol = screen.getByRole("combobox", { name: "协议" });
  fireEvent.mouseDown(protocol.closest(".ant-select")?.querySelector(".ant-select-selector") ?? protocol);
  await waitFor(() => {
    const labels = Array.from(document.querySelectorAll<HTMLElement>(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content",
    )).map((option) => option.textContent);
    expect(labels).toContain("RARE-PROTOCOL");
  });
  fireEvent.mouseDown(document.body);
  await waitFor(() => expect(document.querySelector(
    ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
  )).toBeNull());

  const vendor = screen.getByRole("combobox", { name: "厂商" });
  fireEvent.mouseDown(vendor.closest(".ant-select")?.querySelector(".ant-select-selector") ?? vendor);
  expect(await screen.findByRole("option", { name: "Page Two Vendor" })).toBeTruthy();
});

it("degrades a failed facet preload to current-page filter values", async () => {
  vi.spyOn(api, "listTemplateScenarios").mockImplementation((query) => (
    query.page_size === 48
      ? Promise.reject(new ApiError(0, "network_error", "offline"))
      : Promise.resolve(listResponse([scenario()]))
  ));

  render(
    <TemplateGalleryPage
      scenarioSlug={null}
      busy={false}
      onOpenScenario={() => undefined}
      onBackToGallery={() => undefined}
      onInstantiate={async () => true}
    />,
  );

  await screen.findByRole("heading", { name: "REST 单次请求" });
  await waitFor(() => expect(screen.getByTestId("template-gallery").getAttribute("aria-busy")).toBe("false"));
  const vendor = screen.getByRole("combobox", { name: "厂商" });
  fireEvent.mouseDown(vendor.closest(".ant-select")?.querySelector(".ant-select-selector") ?? vendor);
  expect(await screen.findByRole("option", { name: "DLR" })).toBeTruthy();
});

const SEARCH_TEST_DELAY = 251;

describe("Scenario detail and copy", () => {
  beforeEach(() => {
    vi.spyOn(api, "listTemplateScenarios").mockResolvedValue(listResponse([scenario()]));
    vi.spyOn(api, "getTemplateScenario").mockResolvedValue(detail());
    vi.spyOn(api, "getTemplateVariant").mockImplementation(async (_slug, _version, language) => variant(language));
  });

  function renderDetail(
    onInstantiate: (request: TemplateCopyRequest) => Promise<boolean> = async () => true,
  ) {
    return render(
      <TemplateGalleryPage
        scenarioSlug="rest-single-request"
        busy={false}
        onOpenScenario={() => undefined}
        onBackToGallery={() => undefined}
        onInstantiate={onInstantiate}
      />,
    );
  }

  it("loads Python first and switches all selected-language assets without requesting Java", async () => {
    renderDetail();
    expect(await screen.findByDisplayValue("python recipe source")).toBeTruthy();
    expect((within(screen.getByRole("tabpanel")).getByTestId("template-monaco") as HTMLTextAreaElement).value)
      .toBe("python recipe source");
    expect(screen.getByText(/"fixture": "python-input"/)).toBeTruthy();
    expect(screen.getByText(/"fixture": "python-result"/)).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "JavaScript" }));
    expect(await screen.findByDisplayValue("javascript recipe source")).toBeTruthy();
    expect((within(screen.getByRole("tabpanel")).getByTestId("template-monaco") as HTMLTextAreaElement).value)
      .toBe("javascript recipe source");
    expect(screen.getByText(/"fixture": "javascript-input"/)).toBeTruthy();
    expect(screen.getByText(/"fixture": "javascript-result"/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "python source" })).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "Python" }));
    expect(await screen.findByDisplayValue("python recipe source")).toBeTruthy();
    expect(api.getTemplateVariant).toHaveBeenCalledTimes(3);
    expect(vi.mocked(api.getTemplateVariant).mock.calls.map((call) => call[2])).toEqual(["python", "javascript", "python"]);
  });

  it("selects the first available language and hides empty input examples", async () => {
    vi.mocked(api.getTemplateScenario).mockResolvedValue(detail({
      variants: [{ language: "java", available: true }],
    }));
    vi.mocked(api.getTemplateVariant).mockResolvedValue({ ...variant("java"), input_skeleton: {} });
    renderDetail();
    await screen.findByDisplayValue("java recipe source");
    expect(api.getTemplateVariant).toHaveBeenCalledWith("rest-single-request", "1.0.0", "java");
    expect(screen.queryByRole("tab", { name: "Python" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "JavaScript" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "输入示例" })).toBeNull();
    expect(screen.getByRole("heading", { name: "返回结果示例" })).toBeTruthy();
    for (const name of ["输入", "输出", "安全边界", "运行模式", "来源与许可证", "Runtime 建议配置", "各语言成熟度"]) {
      expect(screen.queryByRole("heading", { name })).toBeNull();
    }
  });

  it("keeps every scrollable Recipe fact reachable from the keyboard", async () => {
    const { container } = renderDetail();
    await screen.findByDisplayValue("python recipe source");

    const facts = Array.from(container.querySelectorAll(".template-recipe-facts pre"));
    expect(facts).toHaveLength(3);
    expect(facts.every((fact) => fact.getAttribute("tabindex") === "0")).toBe(true);
  });

  it("announces detail and Variant loading through the busy main region", async () => {
    let resolveDetail: ((value: TemplateScenarioDetail) => void) | undefined;
    let resolveVariant: ((value: TemplateVariant) => void) | undefined;
    vi.mocked(api.getTemplateScenario).mockImplementation(() => new Promise((resolve) => {
      resolveDetail = resolve;
    }));
    vi.mocked(api.getTemplateVariant).mockImplementation(() => new Promise((resolve) => {
      resolveVariant = resolve;
    }));
    renderDetail();

    const loadingMain = screen.getByRole("main");
    expect(loadingMain.getAttribute("aria-busy")).toBe("true");
    expect(within(loadingMain).getByRole("status").textContent).toBe("正在加载模板详情…");

    await act(async () => resolveDetail?.(detail()));
    const detailMain = await screen.findByTestId("template-detail");
    expect(detailMain.getAttribute("aria-busy")).toBe("true");
    expect(within(detailMain).getByRole("status").textContent).toBe("正在加载所选语言模板…");

    await act(async () => resolveVariant?.(variant()));
    await screen.findByDisplayValue("python recipe source");
    expect(detailMain.getAttribute("aria-busy")).toBe("false");
  });

  it("disables copying while a newly selected language is still loading", async () => {
    let resolveJavascript: ((value: TemplateVariant) => void) | undefined;
    vi.mocked(api.getTemplateVariant).mockImplementation((_slug, _version, selectedLanguage) => {
      if (selectedLanguage === "python") return Promise.resolve(variant("python"));
      return new Promise((resolve) => { resolveJavascript = resolve; });
    });
    renderDetail();
    expect(await screen.findByDisplayValue("python recipe source")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "JavaScript" }));
    await act(async () => { await Promise.resolve(); });
    expect((screen.getByRole("button", { name: "复制为适配器" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByDisplayValue("python recipe source")).toBeNull();

    await act(async () => resolveJavascript?.(variant("javascript")));
    expect(await screen.findByDisplayValue("javascript recipe source")).toBeTruthy();
    expect((screen.getByRole("button", { name: "复制为适配器" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("distinguishes not-found from a recoverable detail failure and retries", async () => {
    vi.mocked(api.getTemplateScenario)
      .mockRejectedValueOnce(new ApiError(503, "template_catalog_unavailable", "offline"))
      .mockResolvedValueOnce(detail());
    const first = renderDetail();

    expect(await screen.findByText("模板详情加载失败")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByDisplayValue("python recipe source")).toBeTruthy();
    expect(api.getTemplateScenario).toHaveBeenCalledTimes(2);
    first.unmount();

    vi.mocked(api.getTemplateScenario).mockRejectedValueOnce(
      new ApiError(404, "template_scenario_not_found", "missing"),
    );
    const second = renderDetail();
    expect(await screen.findByText("没有找到这个模板")).toBeTruthy();
    expect(within(second.container).queryByRole("button", { name: "重试" })).toBeNull();
  });

  it("retries a failed Variant without changing the selected language", async () => {
    let javascriptAttempts = 0;
    vi.mocked(api.getTemplateVariant).mockImplementation(async (_slug, _version, selectedLanguage) => {
      if (selectedLanguage === "javascript" && javascriptAttempts++ === 0) {
        throw new ApiError(503, "template_variant_unavailable", "offline");
      }
      return variant(selectedLanguage);
    });
    renderDetail();
    await screen.findByDisplayValue("python recipe source");
    fireEvent.click(screen.getByRole("tab", { name: "JavaScript" }));

    expect((await screen.findAllByText("该语言模板加载失败")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByDisplayValue("javascript recipe source")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "JavaScript" }).getAttribute("aria-selected")).toBe("true");
    expect(javascriptAttempts).toBe(2);
  });

  it("keeps Gallery state hidden through detail and restores focus to the triggering card", async () => {
    vi.mocked(api.listTemplateScenarios).mockImplementation(async (query) => ({
      items: [scenario({ theme_slug: query.theme })],
      page: query.page ?? 1,
      page_size: query.page_size ?? 12,
      total: query.page_size === 48 ? 1 : 13,
    }));

    const backModes: boolean[] = [];
    function RoutedGallery() {
      const [slug, setSlug] = useState<string | null>(null);
      return (
        <TemplateGalleryPage
          scenarioSlug={slug}
          busy={false}
          onOpenScenario={setSlug}
          onBackToGallery={(useBrowserBack) => {
            backModes.push(useBrowserBack);
            setSlug(null);
          }}
          onInstantiate={async () => true}
        />
      );
    }

    render(<RoutedGallery />);
    await screen.findByRole("heading", { name: "REST 单次请求" });
    fireEvent.click(screen.getByRole("tab", { name: /API 与事件/ }));
    const search = screen.getByRole("textbox", { name: "搜索模板" });
    fireEvent.change(search, { target: { value: "REST" } });
    await waitFor(() => expect(vi.mocked(api.listTemplateScenarios).mock.calls.some(
      ([query]) => query.q === "REST",
    )).toBe(true));

    const vendor = screen.getByRole("combobox", { name: "厂商" });
    fireEvent.mouseDown(document.body);
    fireEvent.mouseDown(vendor.closest(".ant-select")?.querySelector(".ant-select-selector") ?? vendor);
    let vendorOption: HTMLElement | undefined;
    await waitFor(() => {
      vendorOption = Array.from(document.querySelectorAll<HTMLElement>(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content",
      )).find((option) => option.textContent === "DLR");
      expect(vendorOption).not.toBeUndefined();
    });
    fireEvent.click(vendorOption?.closest(".ant-select-item-option") ?? vendorOption as HTMLElement);
    await waitFor(() => expect(vi.mocked(api.listTemplateScenarios).mock.calls.some(
      ([query]) => query.vendor === "DLR",
    )).toBe(true));
    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(vi.mocked(api.listTemplateScenarios).mock.calls.some(
      ([query]) => query.page === 2 && query.vendor === "DLR",
    )).toBe(true));

    const trigger = screen.getByRole("link", { name: "查看详情" });
    fireEvent.click(trigger);
    const detailMain = await screen.findByTestId("template-detail");
    expect(screen.getByTestId("template-gallery").hidden).toBe(true);
    await waitFor(() => expect(document.activeElement).toBe(detailMain));
    fireEvent.click(screen.getByRole("button", { name: "返回模板广场" }));

    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(backModes).toEqual([true]);
    expect(screen.getByTestId("template-gallery").hidden).toBe(false);
    expect((screen.getByRole("textbox", { name: "搜索模板" }) as HTMLInputElement).value).toBe("REST");
    expect(screen.getByRole("tab", { name: /API 与事件/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("combobox", { name: "厂商" }).closest(".ant-select")?.textContent).toContain("DLR");
    expect(document.querySelector(".ant-pagination-item-active")?.textContent).toBe("2");
  });

  it("requests an in-place Gallery fallback for a directly opened detail", async () => {
    const onBackToGallery = vi.fn();
    render(
      <TemplateGalleryPage
        scenarioSlug="rest-single-request"
        busy={false}
        onOpenScenario={() => undefined}
        onBackToGallery={onBackToGallery}
        onInstantiate={async () => true}
      />,
    );
    await screen.findByTestId("template-detail");
    fireEvent.click(screen.getByRole("button", { name: "返回模板广场" }));
    expect(onBackToGallery).toHaveBeenCalledWith(false);
  });

  it("validates the copy name and keeps a conflicting value in the open dialog", async () => {
    const onInstantiate = vi.fn(async () => {
      throw new ApiError(409, "adapter_name_conflict", "conflict");
    });
    renderDetail(onInstantiate);
    await screen.findByDisplayValue("python recipe source");
    fireEvent.click(screen.getByRole("button", { name: "复制为适配器" }));
    fireEvent.click(screen.getByRole("button", { name: "复制并编辑" }));
    expect(await screen.findByText("请输入适配器名称")).toBeTruthy();
    expect(onInstantiate).not.toHaveBeenCalled();

    const input = screen.getByRole("textbox", { name: "适配器名称" });
    fireEvent.change(input, { target: { value: "生产资产同步" } });
    fireEvent.click(screen.getByRole("button", { name: "复制并编辑" }));
    expect(await screen.findByText("已有同名适配器，请换一个名称。")).toBeTruthy();
    expect((input as HTMLInputElement).value).toBe("生产资产同步");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("trims valid copy fields and declares the name as required", async () => {
    const onInstantiate = vi.fn(async () => true);
    renderDetail(onInstantiate);
    await screen.findByDisplayValue("python recipe source");
    fireEvent.click(screen.getByRole("button", { name: "复制为适配器" }));
    const input = screen.getByRole("textbox", { name: "适配器名称" });
    expect(input.getAttribute("required")).not.toBeNull();
    expect(input.getAttribute("aria-required")).toBe("true");
    fireEvent.change(input, { target: { value: "  生产资产同步  " } });
    fireEvent.change(screen.getByRole("textbox", { name: "描述（可选）" }), { target: { value: "  每小时同步  " } });
    fireEvent.click(screen.getByRole("button", { name: "复制并编辑" }));
    await act(async () => { await Promise.resolve(); });
    expect(onInstantiate).toHaveBeenCalledWith(expect.objectContaining({
      name: "生产资产同步",
      description: "每小时同步",
      draft: { code: "python recipe source", requirements: "python-dependency==1.0.0", runtime_config: { fixture: "python-runtime-config" } },
    }));
  });

  it("rejects 129-character names before POST", async () => {
    const onInstantiate = vi.fn(async () => true);
    renderDetail(onInstantiate);
    await screen.findByDisplayValue("python recipe source");
    fireEvent.click(screen.getByRole("button", { name: "复制为适配器" }));
    fireEvent.change(screen.getByRole("textbox", { name: "适配器名称" }), {
      target: { value: "名".repeat(129) },
    });
    fireEvent.click(screen.getByRole("button", { name: "复制并编辑" }));
    expect(await screen.findByText("名称不能超过 128 个字符")).toBeTruthy();
    expect(onInstantiate).not.toHaveBeenCalled();
  });

  it("keeps entered values after a general instantiate failure", async () => {
    const onInstantiate = vi.fn(async () => { throw new Error("offline"); });
    renderDetail(onInstantiate);
    await screen.findByDisplayValue("python recipe source");
    fireEvent.click(screen.getByRole("button", { name: "复制为适配器" }));
    const input = screen.getByRole("textbox", { name: "适配器名称" });
    fireEvent.change(input, { target: { value: "稍后重试" } });
    fireEvent.click(screen.getByRole("button", { name: "复制并编辑" }));
    expect(await screen.findByText("复制失败，请保留当前输入后重试。")).toBeTruthy();
    expect((input as HTMLInputElement).value).toBe("稍后重试");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("allows only one instantiate request while the first submission is in flight", async () => {
    let resolveRequest: ((value: boolean) => void) | undefined;
    const onInstantiate = vi.fn(() => new Promise<boolean>((resolve) => { resolveRequest = resolve; }));
    renderDetail(onInstantiate);
    await screen.findByDisplayValue("python recipe source");
    fireEvent.click(screen.getByRole("button", { name: "复制为适配器" }));
    fireEvent.change(screen.getByRole("textbox", { name: "适配器名称" }), { target: { value: "单飞复制" } });
    const submit = screen.getByRole("button", { name: "复制并编辑" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(onInstantiate).toHaveBeenCalledTimes(1);
    await act(async () => resolveRequest?.(false));
  });

  it("keeps file templates browsable and copyable without a Managed Input capability gate", async () => {
    vi.mocked(api.getTemplateScenario).mockResolvedValue(detail({
      slug: "csv-to-json",
      theme_slug: "file-data",
      title: { "zh-CN": "CSV 转 JSON", en: "CSV to JSON" },
      logo_key: "file-csv",
    }));
    render(
      <TemplateGalleryPage
        scenarioSlug="csv-to-json"
        busy={false}
        onOpenScenario={() => undefined}
        onBackToGallery={() => undefined}
        onInstantiate={async () => true}
      />,
    );
    await screen.findByDisplayValue("python recipe source");
    expect((screen.getByRole("button", { name: "复制为适配器" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
