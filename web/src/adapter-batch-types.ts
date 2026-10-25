/** Durable, secret-free adapter batch receipts (Issue #223). */
export const ADAPTER_BATCH_LIMIT = 100;
export type AdapterBatchAction = "start" | "stop";
export type AdapterBatchMode = "manual" | "schedule" | "webhook" | "subscription";
export type AdapterBatchItemAction = "run_once" | "cancel_executions" | "enable_schedule" | "disable_schedule"
  | "enable_webhook" | "disable_webhook" | "enable_subscription" | "disable_subscription";
export type AdapterBatchStatus = "pending" | "accepted" | "stopping" | "succeeded" | "no_change" | "failed" | "unknown";
export interface AdapterBatchTarget { adapter_id: number; expected_token: string }
export interface AdapterBatchRequest { id: string; action: AdapterBatchAction; targets: AdapterBatchTarget[] }
export interface AdapterBatchItem {
  adapter_id: number;
  adapter_name: string | null;
  mode: AdapterBatchMode | null;
  action: AdapterBatchItemAction | null;
  status: AdapterBatchStatus;
  error_code: string | null;
  retryable: boolean;
  execution_ids: number[];
  executions: { id: number; status: string; cancel_requested: boolean }[];
  subscription_id: number | null;
  subscription_state: string | null;
}
export interface AdapterBatchOperation {
  id: string;
  action: AdapterBatchAction;
  created_at: string;
  items: AdapterBatchItem[];
}
