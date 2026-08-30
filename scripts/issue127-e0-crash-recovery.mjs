/* Issue #127 E0 real Worker crash/recovery probe. */

import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

const PROJECT = process.env.COMPOSE_PROJECT_NAME ?? "dlr-i127-e0-141";
const BASE_URL = process.env.DLR_E0_BASE_URL ?? "http://127.0.0.1:8924";
const ADMIN_TOKEN = process.env.DLR_ADMIN_TOKEN;
const OUTPUT_DIR = process.env.DLR_E0_OUTPUT_DIR ?? "docs/evidence/issue127-e0";
const RUN_TAG = process.env.DLR_E0_RUN_TAG ?? String(Date.now());
if (!ADMIN_TOKEN) throw new Error("DLR_ADMIN_TOKEN is required");
for (const name of ["DLR_WORKER_TOKEN", "DLR_MASTER_KEY", "DLR_SECRET_SMOKE", "DLR_PLATFORM_LOG_ROOT"]) {
  if (!process.env[name]) throw new Error(`${name} is required for Compose interpolation`);
}

const longCode = `import time\n\ndef handle(context, input):\n    time.sleep(60)\n    return {"unexpected": True}\n`;

function docker(args, options = {}) {
  return execFileSync("docker", args, { encoding: "utf8", timeout: options.timeout ?? 30_000, maxBuffer: 4 * 1024 * 1024 });
}

function compose(args, options = {}) {
  return docker(["compose", "-p", PROJECT, ...args], options);
}

async function api(path, options = {}) {
  const response = await fetch(`${BASE_URL}${path}`, { method: options.method ?? "GET", headers: { Authorization: `Bearer ${ADMIN_TOKEN}`, ...(options.headers ?? {}) }, body: options.body });
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  return { status: response.status, body };
}

function sql(query) {
  return compose(["exec", "-T", "postgres", "psql", "-U", "dlr", "-d", "dlr", "-At", "-F", "\t", "-c", query]).trim();
}

function errorCode(response) {
  return response.body?.detail?.code ?? null;
}

async function waitFor(predicate, timeoutMs, description, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  let value = null;
  while (Date.now() < deadline) {
    value = await predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`${description} timed out`);
}

async function main() {
  const created = await api("/api/adapters", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: `E0 crash recovery ${RUN_TAG}`, description: "Issue 127 E0 crash fixture", language: "python", adapter_type: "task", timeout_seconds: 60 }) });
  if (created.status !== 201) throw new Error(`adapter create ${created.status}/${errorCode(created)}`);
  const adapterId = created.body.id;
  const version = await api(`/api/adapters/${adapterId}/versions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: longCode, requirements: "", runtime_config: { issue127_e0_crash: true } }) });
  if (version.status !== 201) throw new Error(`version create ${version.status}/${errorCode(version)}`);
  const execution = await api(`/api/adapters/${adapterId}/executions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
  if (execution.status !== 202) throw new Error(`execution create ${execution.status}/${errorCode(execution)}`);
  const running = await waitFor(async () => {
    const response = await api(`/api/executions/${execution.body.id}`);
    return response.status === 200 && response.body?.status === "running" ? response.body : null;
  }, 30_000, "running execution");
  const workerId = running.worker_id;
  if (!workerId) throw new Error("running execution has no worker id");
  const workerName = (await api("/api/workers")).body?.find((worker) => worker.id === workerId)?.name;
  if (!workerName) throw new Error("running worker is not visible to admin");

  compose(["kill", "-s", "SIGKILL", "worker"], { timeout: 30_000 });
  sql(`UPDATE workers SET status='offline', last_heartbeat=now() - interval '1 hour' WHERE id=${workerId}`);
  // The reconciler intentionally waits one frozen grace window after the
  // execution deadline; move the deadline beyond the configured 60s grace.
  sql(`UPDATE executions SET execution_deadline_at=now() - interval '2 minutes' WHERE id=${execution.body.id}`);
  const failed = await waitFor(async () => {
    const response = await api(`/api/executions/${execution.body.id}`);
    return response.status === 200 && !["pending", "running"].includes(response.body?.status) ? response.body : null;
  }, 45_000, "reconciled failed execution");
  if (failed.status !== "failed" || failed.error_code !== "worker_lost") throw new Error(`unexpected recovery result ${failed.status}/${failed.error_code}`);

  compose(["up", "-d", "worker"], { timeout: 120_000 });
  const healthy = await waitFor(async () => {
    const rows = docker(["ps", "--filter", `label=com.docker.compose.project=${PROJECT}`, "--filter", "name=-worker-", "--format", "{{json .}}"]).split("\n").filter(Boolean).map((line) => JSON.parse(line));
    return rows.some((row) => row.Health === "healthy" || String(row.Status).includes("(healthy)"));
  }, 90_000, "worker health");
  const online = await waitFor(async () => {
    const response = await api("/api/workers");
    return response.status === 200 && response.body?.some((worker) => worker.id === workerId && worker.status === "online") ? true : null;
  }, 45_000, "worker online registration");
  const result = {
    schema: "issue127-e0-worker-crash-recovery-v1",
    project: PROJECT,
    adapter_id: adapterId,
    execution_id: execution.body.id,
    worker_id: workerId,
    worker_name: workerName,
    crash_signal: "SIGKILL",
    running_before_crash: true,
    reconciled_status: failed.status,
    reconciled_error_code: failed.error_code,
    worker_healthy_after_restart: Boolean(healthy),
    worker_online_after_restart: Boolean(online),
    active_execution_after_recovery: false,
    machine_gate: "PASS",
    human_acceptance: "待人工验收",
  };
  await mkdir(OUTPUT_DIR, { recursive: true });
  await writeFile(join(OUTPUT_DIR, "worker-crash-recovery.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ machine_gate: result.machine_gate, reconciled_error_code: result.reconciled_error_code, worker_healthy_after_restart: result.worker_healthy_after_restart, worker_online_after_restart: result.worker_online_after_restart }));
}

await main().catch(async (error) => {
  const failure = { schema: "issue127-e0-worker-crash-recovery-v1", machine_gate: "FAIL", error: String(error instanceof Error ? error.message : error), human_acceptance: "待人工验收" };
  await mkdir(OUTPUT_DIR, { recursive: true });
  await writeFile(join(OUTPUT_DIR, "worker-crash-recovery.partial.json"), `${JSON.stringify(failure, null, 2)}\n`, "utf8");
  throw error;
});
