import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, setAuthToken } from "./api";
import { i18n, resources } from "./i18n";
import { uploadManagedInputArtifact } from "./managed-input-client";
import type {
  Artifact,
  ExecutionInputSnapshot,
  InputConfig,
  ManagedInputArtifact,
  ManagedInputSettings,
  ManagedInputSettingsUpdate,
} from "./types";

const settingsUpdate: ManagedInputSettingsUpdate = {
  default_retention_seconds: 86_400,
  max_file_bytes: 104857600,
  platform_quota_bytes: 10 * 1024 * 1024 * 1024,
  adapter_quota_bytes: 1024 * 1024 * 1024,
  allow_manual_delete: true,
  max_custom_retention_seconds: 2_592_000,
  min_free_space_bytes: 1024 * 1024 * 1024,
  staged_ttl_seconds: 3_600,
};

const settings: ManagedInputSettings = {
  id: 1,
  ...settingsUpdate,
  usage: {
    platform_actual_bytes: 0,
    platform_reserved_bytes: 0,
    platform_total_bytes: 0,
    adapters: [],
  },
  over_quota: false,
  platform_over_quota: false,
  adapter_over_quota: [],
  created_at: "2026-08-28T00:00:00Z",
  updated_at: "2026-08-28T00:00:00Z",
};

const stagedArtifact: ManagedInputArtifact = {
  id: 9,
  original_filename: "report.csv",
  content_type: "text/csv",
  size_bytes: 4,
  sha256: "a".repeat(64),
  status: "STAGED",
  created_at: "2026-08-28T00:00:00Z",
  expires_at: null,
};

const safeSnapshot: ExecutionInputSnapshot = {
  source_type: "managed_files",
  revision: 4,
  artifacts: [
    {
      ordinal: 0,
      original_filename: "report.csv",
      content_type: "text/csv",
      size_bytes: 4,
      sha256: stagedArtifact.sha256,
    },
  ],
};

afterEach(() => {
  setAuthToken(null);
  vi.unstubAllGlobals();
  document.cookie = "dlr_account_csrf=; Max-Age=0; path=/";
});

describe("Issue #127 D0 public contracts", () => {
  it("keeps InputConfig, clone state and snapshot facts safe and file-free", () => {
    const cloneInput: InputConfig = {
      adapter_id: 12,
      revision: 1,
      source_type: "managed_files",
      json_value: null,
      retention: { mode: "system_default", seconds: null },
      artifacts: [],
      valid_for_run: false,
      invalid_reason: "managed_files_empty",
    };
    const publicArtifact: Artifact = stagedArtifact;

    expect(cloneInput.artifacts).toEqual([]);
    expect(publicArtifact).not.toHaveProperty("storage_key");
    expect(publicArtifact).not.toHaveProperty("path");
    expect(publicArtifact).not.toHaveProperty("token");
    expect(safeSnapshot.artifacts[0]).not.toHaveProperty("artifact_id");
    expect(safeSnapshot.artifacts[0]).not.toHaveProperty("storage_key");
    expect(safeSnapshot.artifacts[0]).not.toHaveProperty("path");
  });

  it("exposes the settings and staged-artifact JSON resources", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/system/managed-input-settings" && init?.method === undefined) {
        return { ok: true, status: 200, json: async () => settings };
      }
      if (url === "/api/system/managed-input-settings" && init?.method === "PUT") {
        return { ok: true, status: 200, json: async () => settings };
      }
      if (url === "/api/system/managed-input-capability") {
        return { ok: true, status: 200, json: async () => ({ managed_files_enabled: false, ready: false, default_retention_seconds: 86_400, max_custom_retention_seconds: 2_592_000, allow_manual_delete: true, allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"] }) };
      }
      if (url === "/api/adapters/12/input-artifacts?status=staged") {
        return { ok: true, status: 200, json: async () => [stagedArtifact] };
      }
      if (url === "/api/adapters/12/input-artifacts/9" && init?.method === "DELETE") {
        return { ok: true, status: 204, json: async () => undefined };
      }
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.getManagedInputSettings()).resolves.toEqual(settings);
    await expect(api.updateManagedInputSettings(settingsUpdate)).resolves.toEqual(settings);
    await expect(api.getManagedInputCapability()).resolves.toEqual({
      managed_files_enabled: false,
      ready: false,
      default_retention_seconds: 86_400,
      max_custom_retention_seconds: 2_592_000,
      allow_manual_delete: true,
      allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
    });
    await expect(api.listInputArtifacts(12)).resolves.toEqual([stagedArtifact]);
    await expect(api.deleteInputArtifact(12, 9)).resolves.toBeUndefined();
  });

  it("uses an independent multipart request with same-origin credentials and CSRF", async () => {
    class FakeXmlHttpRequest {
      static last: FakeXmlHttpRequest | null = null;
      readonly upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
      withCredentials = false;
      status = 201;
      responseText = JSON.stringify(stagedArtifact);
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      method = "";
      url = "";
      headers: Record<string, string> = {};
      body: BodyInit | null = null;

      constructor() {
        FakeXmlHttpRequest.last = this;
      }

      open(method: string, url: string): void {
        this.method = method;
        this.url = url;
      }

      setRequestHeader(name: string, value: string): void {
        this.headers[name] = value;
      }

      send(body: BodyInit | null): void {
        this.body = body;
        this.upload.onprogress?.({ loaded: 4, total: 4 } as ProgressEvent);
        this.onload?.();
      }

      abort(): void {
        this.onabort?.();
      }
    }

    setAuthToken("fixture-auth");
    document.cookie = "dlr_account_csrf=fixture-csrf; path=/";
    vi.stubGlobal("XMLHttpRequest", FakeXmlHttpRequest);
    const progress = vi.fn();

    await expect(
      uploadManagedInputArtifact(12, new File(["data"], "report.csv", { type: "text/csv" }), {
        onProgress: progress,
      }),
    ).resolves.toEqual(stagedArtifact);

    const request = FakeXmlHttpRequest.last;
    expect(request?.method).toBe("POST");
    expect(request?.url).toBe("/api/adapters/12/input-artifacts");
    expect(request?.withCredentials).toBe(true);
    expect(request?.headers.Authorization).toBe("Bearer fixture-auth");
    expect(request?.headers["X-CSRF-Token"]).toBe("fixture-csrf");
    expect(request?.headers["Content-Type"]).toBeUndefined();
    expect(request?.body).toBeInstanceOf(FormData);
    expect(progress).toHaveBeenCalledWith({ loaded: 4, total: 4 });
  });

  it("maps multipart failures to safe structured ApiError values", async () => {
    class FailingXmlHttpRequest {
      readonly upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
      withCredentials = false;
      status = 413;
      responseText = JSON.stringify({
        detail: {
          code: "input_file_too_large",
          message: "raw filename and deployment details must not reach the browser",
          params: { max_bytes: 104857600 },
        },
      });
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;

      open(): void {}

      setRequestHeader(): void {}

      send(): void {
        this.onload?.();
      }

      abort(): void {
        this.onabort?.();
      }
    }

    vi.stubGlobal("XMLHttpRequest", FailingXmlHttpRequest);
    const rejected = uploadManagedInputArtifact(
      12,
      new File(["data"], "report.csv", { type: "text/csv" }),
    ).catch((error: unknown) => error);
    const error = await rejected;

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 413,
      code: "input_file_too_large",
      params: { max_bytes: 104857600 },
    });
    expect((error as ApiError).message).not.toContain("raw filename");
  });

  it("provides D0 input, history and stable error keys in both locales", () => {
    const runtimeKeys = ["task.input.fileTypeNotAllowed", "task.input.historySnapshot"];
    for (const key of runtimeKeys) {
      expect(i18n.exists(key, { lng: "zh-CN", ns: "runtime" })).toBe(true);
      expect(i18n.exists(key, { lng: "en", ns: "runtime" })).toBe(true);
    }
    for (const key of ["managedInput.title", "managedInput.upload", "managedInput.settingsInvalid"]) {
      expect(i18n.exists(key, { lng: "zh-CN", ns: "settings" })).toBe(true);
      expect(i18n.exists(key, { lng: "en", ns: "settings" })).toBe(true);
    }
    for (const code of [
      "input_file_type_not_allowed",
      "input_file_too_large",
      "input_upload_failed",
      "upload_session_expired",
      "input_artifact_not_ready",
      "input_artifact_checksum_mismatch",
      "managed_input_settings_invalid",
      "worker_protocol_incompatible",
      "workspace_cleanup_unknown",
    ]) {
      expect(resources.en.common.errors).toHaveProperty(code);
      expect(resources["zh-CN"].common.errors).toHaveProperty(code);
      expect((resources.en.common.errors as Record<string, string>)[code]).not.toBe(code);
      expect((resources["zh-CN"].common.errors as Record<string, string>)[code]).not.toBe(code);
    }
  });
});
