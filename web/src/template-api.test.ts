import { afterEach, beforeEach, expect, it, vi } from "vitest";

import {
  api,
  clearTemplateVariantCache,
  setAuthToken,
  templateScenarioQueryString,
} from "./api";
import type { TemplateVariant } from "./types";

const variant: TemplateVariant = {
  scenario_slug: "rest-single-request",
  theme_slug: "api-events",
  title: { "zh-CN": "REST 单次请求", en: "Single REST request" },
  language: "python",
  adapter_type: "task",
  template_version: "1.0.0",
  behavior_contract_version: "dlr-recipe/v1",
  maturity: "syntax-verified",
  code: "def handle(context, input):\n    return input\n",
  requirements: "httpx==0.28.1",
  install_notes: { "zh-CN": "安装依赖", en: "Install dependencies" },
  input_skeleton: {},
  input_contract: {},
  output_contract: {},
  runtime_config: {},
  runtime_guidance: { "zh-CN": "保持有界", en: "Keep requests bounded" },
  sources: [],
};

beforeEach(() => {
  clearTemplateVariantCache();
  setAuthToken("test-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAuthToken(null);
});

it("encodes every Scenario filter with stable pagination", () => {
  const query = templateScenarioQueryString({
    theme: "api-events",
    q: "REST & webhook",
    vendor: "DLR recipes",
    adapter_type: "task",
    protocol: "HTTP/JSON",
    language: "javascript",
    maturity: "syntax-verified",
    page: 2,
    page_size: 12,
  });
  expect(Object.fromEntries(new URLSearchParams(query))).toEqual({
    theme: "api-events",
    q: "REST & webhook",
    vendor: "DLR recipes",
    adapter_type: "task",
    protocol: "HTTP/JSON",
    language: "javascript",
    maturity: "syntax-verified",
    page: "2",
    page_size: "12",
  });
});

it("caches only the selected language Variant by slug, version, and language", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return new Response(JSON.stringify(variant), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);

  const first = await api.getTemplateVariant("rest-single-request", "1.0.0", "python");
  const second = await api.getTemplateVariant("rest-single-request", "1.0.0", "python");

  expect(first.code).toContain("def handle");
  expect(second).toBe(first);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0][0]).toBe("/api/templates/scenarios/rest-single-request/variants/python");
});

it("rejects and evicts a Variant whose identity does not match the requested version", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      ...variant,
      scenario_slug: "different-scenario",
      template_version: "2.0.0",
      language: "java",
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }))
    .mockResolvedValueOnce(new Response(JSON.stringify(variant), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(api.getTemplateVariant("rest-single-request", "1.0.0", "python"))
    .rejects.toMatchObject({ code: "template_variant_mismatch", status: 409 });
  await expect(api.getTemplateVariant("rest-single-request", "1.0.0", "python"))
    .resolves.toMatchObject({ scenario_slug: "rest-single-request", template_version: "1.0.0", language: "python" });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("uses the dedicated instantiate endpoint and never Adapter clone", async () => {
  const adapter = {
    id: 42,
    name: "copied-recipe",
    description: "",
    language: "java",
    adapter_type: "task",
    run_mode: "manual",
    latest_version_id: 77,
    created_at: "2026-09-05T00:00:00Z",
    updated_at: "2026-09-05T00:00:00Z",
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return new Response(JSON.stringify(adapter), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);

  await api.instantiateTemplate("rest-single-request", "java", {
    name: "copied-recipe",
    expected_template_version: "1.0.0",
  });

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/templates/scenarios/rest-single-request/variants/java/instantiate");
  expect(init?.method).toBe("POST");
  expect(String(url)).not.toContain("clone");
  expect(JSON.parse(String(init?.body))).toEqual({
    name: "copied-recipe",
    expected_template_version: "1.0.0",
  });
});
