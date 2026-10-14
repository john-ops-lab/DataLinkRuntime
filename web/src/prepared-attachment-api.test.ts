import { afterEach, expect, it, vi } from "vitest";
import { ApiError, api, setAuthToken } from "./api";

const scope = "12345678-1234-4234-8234-123456789abc";
afterEach(() => { setAuthToken(null); vi.unstubAllGlobals(); });

it("uses adapter-owned capability metadata and does not bypass access errors", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: "adapter_read_only" } }), { status: 403 }));
  vi.stubGlobal("fetch", fetch);
  await expect(api.getAiAttachmentCapabilities(7)).rejects.toBeInstanceOf(ApiError);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0]?.[0]).toBe("/api/adapters/7/ai/attachment-capabilities");
});

it("uses the old capability route only when an older Control lacks the new route", async () => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ limits: {}, providers: [], supported_content_types: [] })));
  vi.stubGlobal("fetch", fetch);
  await api.getAiAttachmentCapabilities(7);
  expect(fetch.mock.calls[1]?.[0]).toBe("/api/ai/attachment-capabilities");
});

it("passes cancellation and exact scope/ref routes while keeping source bytes out of GET/DELETE", async () => {
  setAuthToken("synthetic-client-token");
  const fetch = vi.fn().mockImplementation(async (_path: string, init: RequestInit) =>
    init.method === "DELETE" ? new Response(null, { status: 204 }) : new Response(JSON.stringify({ status: "ready" })));
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  const attachment = { filename: "fixture.txt", content_type: "text/plain", data_base64: "eA==" };
  await api.prepareAiAttachment(7, scope, attachment, signal);
  const post = fetch.mock.calls[0]?.[1] as RequestInit;
  expect(JSON.parse(String(post.body))).toEqual({ scope_id: scope, attachment });
  expect(post.signal).toBe(signal);
  expect(post.credentials).toBe("same-origin");
  await api.getPreparedAiAttachment(7, scope, "opaque-reference-id", signal);
  await api.releasePreparedAiAttachment(7, scope, "opaque-reference-id");
  await api.releaseAiAttachmentScope(7, scope);
  expect(fetch.mock.calls[1]?.[0]).toBe(`/api/adapters/7/ai/attachments/opaque-reference-id?scope_id=${scope}`);
  expect((fetch.mock.calls[1]?.[1] as RequestInit).body).toBeUndefined();
  expect((fetch.mock.calls[2]?.[1] as RequestInit).method).toBe("DELETE");
  expect((fetch.mock.calls[3]?.[1] as RequestInit).keepalive).toBe(true);
  expect(fetch.mock.calls[3]?.[0]).toBe(`/api/adapters/7/ai/attachments/scopes/${scope}`);
});
