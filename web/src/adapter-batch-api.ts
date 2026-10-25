import type { AdapterBatchOperation, AdapterBatchRequest } from "./adapter-batch-types";

type Request = <T>(path: string, init?: RequestInit) => Promise<T>;

export function createAdapterBatchApi(request: Request) {
  return {
    prepareAdapterBatch: (payload: AdapterBatchRequest): Promise<AdapterBatchOperation> =>
      request("/api/adapter-batches", { method: "POST", body: JSON.stringify(payload) }),
    getAdapterBatch: (id: string): Promise<AdapterBatchOperation> =>
      request(`/api/adapter-batches/${encodeURIComponent(id)}`),
    executeAdapterBatch: (id: string): Promise<AdapterBatchOperation> =>
      request(`/api/adapter-batches/${encodeURIComponent(id)}/execute`, { method: "POST" }),
    retryAdapterBatch: (id: string, adapterIds: number[]): Promise<AdapterBatchOperation> =>
      request(`/api/adapter-batches/${encodeURIComponent(id)}/retry`, {
        method: "POST", body: JSON.stringify({ adapter_ids: adapterIds }),
      }),
  };
}
