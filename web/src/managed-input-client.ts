/** Browser transport for one Managed Input Artifact upload.
 *
 * This deliberately does not use the JSON API helper: the browser must build
 * the multipart boundary, while the account cookie and CSRF header still need
 * to accompany the same-origin request.
 */

import { ApiError, getAuthToken, handleUnauthorized } from "./api";
import type { ManagedInputArtifact } from "./types";

export interface ManagedInputUploadProgress {
  loaded: number;
  total: number | null;
}

export interface ManagedInputUploadOptions {
  onProgress?: (progress: ManagedInputUploadProgress) => void;
  signal?: AbortSignal;
}

export interface ManagedInputUploadRequest extends ManagedInputUploadOptions {
  adapterId: number;
  file: File;
}

function accountCsrfToken(): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const raw = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("dlr_account_csrf="))
    ?.slice("dlr_account_csrf=".length);
  if (!raw) {
    return null;
  }
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

function structuredError(status: number, responseText: string): ApiError {
  let code =
    status === 0 ? "network_error" : status === 413 ? "input_file_too_large" : "input_upload_failed";
  let params: Record<string, unknown> = {};
  try {
    const body: unknown = JSON.parse(responseText);
    if (typeof body === "object" && body !== null) {
      const detail = (body as Record<string, unknown>).detail;
      const error =
        typeof detail === "object" && detail !== null
          ? (detail as Record<string, unknown>)
          : (body as Record<string, unknown>);
      const candidate = error.code;
      if (typeof candidate === "string" && /^[a-z][a-z0-9_]{1,63}$/.test(candidate)) {
        code = candidate;
      }
      const candidateParams = error.params;
      if (
        typeof candidateParams === "object" &&
        candidateParams !== null &&
        !Array.isArray(candidateParams)
      ) {
        params = candidateParams as Record<string, unknown>;
      }
    }
  } catch {
    // A proxy can return an empty or non-JSON body. Keep the stable fallback.
  }
  const message =
    code === "network_error" ? "Control is unreachable" : "Managed Input upload failed";
  return new ApiError(status, code, message, params);
}

function normalizeRequest(
  adapterIdOrRequest: number | ManagedInputUploadRequest,
  file?: File,
  options?: ManagedInputUploadOptions,
): ManagedInputUploadRequest {
  if (typeof adapterIdOrRequest === "number") {
    if (file === undefined) {
      throw new TypeError("A file is required for Managed Input upload");
    }
    return { adapterId: adapterIdOrRequest, file, ...options };
  }
  return adapterIdOrRequest;
}

/** Upload one file while keeping binary transport separate from JSON APIs. */
export function uploadManagedInputArtifact(
  adapterId: number,
  file: File,
  options?: ManagedInputUploadOptions,
): Promise<ManagedInputArtifact>;
export function uploadManagedInputArtifact(
  request: ManagedInputUploadRequest,
): Promise<ManagedInputArtifact>;
export function uploadManagedInputArtifact(
  adapterIdOrRequest: number | ManagedInputUploadRequest,
  file?: File,
  options?: ManagedInputUploadOptions,
): Promise<ManagedInputArtifact> {
  const request = normalizeRequest(adapterIdOrRequest, file, options);
  return new Promise<ManagedInputArtifact>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;

    const finish = (callback: () => void): void => {
      if (settled) {
        return;
      }
      settled = true;
      request.signal?.removeEventListener("abort", abortRequest);
      callback();
    };

    const abortRequest = (): void => {
      if (!settled) {
        xhr.abort();
      }
    };

    xhr.open("POST", `/api/adapters/${request.adapterId}/input-artifacts`);
    xhr.withCredentials = true;
    const token = getAuthToken();
    if (token !== null) {
      xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    }
    const csrf = accountCsrfToken();
    if (csrf !== null) {
      xhr.setRequestHeader("X-CSRF-Token", csrf);
    }
    xhr.upload.onprogress = (event) => {
      request.onProgress?.({
        loaded: event.loaded,
        total: event.total > 0 ? event.total : null,
      });
    };
    xhr.onload = () => {
      if (xhr.status === 401) {
        handleUnauthorized();
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        finish(() => reject(structuredError(xhr.status, xhr.responseText)));
        return;
      }
      let body: unknown;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        finish(() => reject(new ApiError(xhr.status, "input_upload_failed", "Managed Input upload failed")));
        return;
      }
      finish(() => resolve(body as ManagedInputArtifact));
    };
    xhr.onerror = () => {
      finish(() => reject(structuredError(0, "")));
    };
    xhr.onabort = () => {
      finish(() => reject(new ApiError(0, "input_upload_interrupted", "Input upload was interrupted")));
    };

    if (request.signal?.aborted) {
      xhr.abort();
      return;
    }
    request.signal?.addEventListener("abort", abortRequest, { once: true });

    const formData = new FormData();
    formData.append("file", request.file, request.file.name);
    // Do not set Content-Type: XMLHttpRequest adds the multipart boundary.
    xhr.send(formData);
  });
}

/** Descriptive alias for callers that use the API resource name. */
export const uploadInputArtifact = uploadManagedInputArtifact;
