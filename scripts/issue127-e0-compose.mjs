/*
 * Issue #127 E0 runtime gate.
 *
 * This probe runs against the retained isolated Compose application.  It
 * exercises the public API and the real Worker/GC loops, while receipts only
 * contain safe IDs, status/error codes, counts and boolean facts.  It never
 * serializes response bodies containing credentials, storage keys, paths or
 * cookies.
 */

import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

const PROJECT = process.env.COMPOSE_PROJECT_NAME ?? "dlr-i127-e0-141";
const SESSION = process.env.AO_SESSION_ID ?? "datalinkruntime-141-e0";
const BASE_URL = process.env.DLR_E0_BASE_URL ?? "http://127.0.0.1:8924";
const ADMIN_TOKEN = process.env.DLR_ADMIN_TOKEN ?? process.env.DLR_D3_ADMIN_TOKEN;
const OUTPUT_DIR = process.env.DLR_E0_OUTPUT_DIR ?? "docs/evidence/issue127-e0";
const RUN_TAG = process.env.DLR_E0_RUN_TAG ?? String(Date.now());
const REPO_ROOT = process.cwd();

if (!ADMIN_TOKEN) throw new Error("DLR_ADMIN_TOKEN is required");

const result = {
  schema: "issue127-e0-compose-runtime-v1",
  project: PROJECT,
  ao_session: SESSION,
  base_url: BASE_URL,
  managed_files_enabled: null,
  fixtures: [],
  checks: [],
  race: null,
  resource_ownership: null,
  sensitive_scan: null,
  machine_gate: "UNRUN",
  human_acceptance: "待人工验收",
};

function safeError(error) {
  return String(error instanceof Error ? error.message : error)
    .replaceAll(ADMIN_TOKEN, "<redacted-token>")
    .replaceAll("/var/lib/dlr", "<redacted-runtime-root>")
    .replaceAll(REPO_ROOT, "<redacted-repo-root>");
}

function docker(args, options = {}) {
  return execFileSync("docker", args, {
    encoding: "utf8",
    timeout: options.timeout ?? 30_000,
    maxBuffer: 4 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function compose(args, options = {}) {
  return docker(["compose", "-p", PROJECT, ...args], options);
}

function sql(query) {
  const output = compose(
    ["exec", "-T", "postgres", "psql", "-U", "dlr", "-d", "dlr", "-At", "-F", "\t", "-c", query],
    { timeout: 30_000 },
  );
  return output.trim();
}

function parseRows(raw) {
  return raw.split("\n").filter(Boolean).map((line) => line.split("\t"));
}

async function api(path, options = {}) {
  const headers = {
    Authorization: `Bearer ${ADMIN_TOKEN}`,
    ...(options.headers ?? {}),
  };
  const response = await fetch(`${BASE_URL}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body,
  });
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  return { status: response.status, body };
}

function codeOf(response) {
  return response.body?.detail?.code ?? null;
}

function check(name, condition, details = {}) {
  const item = { name, ...details, status: condition ? "PASS" : "FAIL" };
  result.checks.push(item);
  if (!condition) throw new Error(`${name} failed`);
  return item;
}

function fixture(name, adapterId, details = {}) {
  result.fixtures.push({ name, adapter_id: adapterId, ...details });
}

async function createTask(name, language, code, timeoutSeconds = 60) {
  const created = await api("/api/adapters", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description: "Issue 127 E0 runtime fixture", language, adapter_type: "task", timeout_seconds: timeoutSeconds }),
  });
  if (created.status === 409 && codeOf(created) === "adapter_name_conflict") {
    const existing = await api("/api/adapters");
    const match = (existing.body ?? []).find((item) => item.name === name);
    if (!match) throw new Error(`create ${language}: name conflict without existing fixture`);
    if (match.run_mode === "schedule") {
      const disabled = await api(`/api/adapters/${match.id}/schedule`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: false, cron: "* * * * *", timezone: "UTC" }),
      });
      if (disabled.status !== 200) throw new Error(`reset ${language} schedule: ${disabled.status}/${codeOf(disabled)}`);
      const manual = await api(`/api/adapters/${match.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_mode: "manual" }),
      });
      if (manual.status !== 200) throw new Error(`reset ${language} run mode: ${manual.status}/${codeOf(manual)}`);
    }
    return { id: match.id, revision: 1 };
  }
  if (created.status !== 201) throw new Error(`create ${language}: ${created.status}/${codeOf(created)}`);
  const adapterId = created.body.id;
  const version = await api(`/api/adapters/${adapterId}/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, requirements: "", runtime_config: { issue127_e0: true } }),
  });
  if (version.status !== 201) throw new Error(`version ${language}: ${version.status}/${codeOf(version)}`);
  return { id: adapterId, revision: 1 };
}

async function upload(adapterId, filename, content) {
  const form = new FormData();
  form.append("file", new Blob([content], { type: "text/plain" }), filename);
  const response = await api(`/api/adapters/${adapterId}/input-artifacts`, {
    method: "POST",
    body: form,
  });
  if (response.status !== 201) throw new Error(`upload ${filename}: ${response.status}/${codeOf(response)}`);
  return response.body;
}

async function saveManaged(adapterId, artifactIds, retention = { mode: "system_default", seconds: null }) {
  const current = await api(`/api/adapters/${adapterId}/input-config`);
  if (current.status !== 200) throw new Error(`read input config: ${current.status}`);
  const response = await api(`/api/adapters/${adapterId}/input-config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_revision: current.body.revision,
      source_type: "managed_files",
      artifact_ids: artifactIds,
      retention,
    }),
  });
  if (response.status !== 200) throw new Error(`save managed config: ${response.status}/${codeOf(response)}`);
  return response.body;
}

async function waitExecution(executionId, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  let current = null;
  while (Date.now() < deadline) {
    const response = await api(`/api/executions/${executionId}`);
    if (response.status === 200) {
      current = response.body;
      if (!current || !["pending", "running"].includes(current.status)) return current;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`execution ${executionId} did not reach terminal state (${current?.status ?? "missing"})`);
}

async function createExecution(adapterId) {
  const response = await api(`/api/adapters/${adapterId}/executions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (response.status !== 202) throw new Error(`execution create: ${response.status}/${codeOf(response)}`);
  return response.body;
}

async function runLanguage(language, code, expectedName, expectedContent, name = `E0 ${language} ${RUN_TAG}`) {
  const adapter = await createTask(name, language, code);
  const artifact = await upload(adapter.id, `${language}-context.txt`, expectedContent);
  const config = await saveManaged(adapter.id, [artifact.id]);
  const execution = await createExecution(adapter.id);
  const terminal = await waitExecution(execution.id);
  const output = terminal.output ?? {};
  const observedName = output.name ?? output.original_name ?? output.originalName ?? output.file?.original_name ?? output.file?.originalName;
  const observedContent = output.content ?? output.file?.content;
  check(`runtime-${language}`, terminal.status === "succeeded" && observedName === expectedName && observedContent === expectedContent, {
    adapter_id: adapter.id,
    execution_id: execution.id,
    execution_status: terminal.status,
    source_type: terminal.input_source_type,
    input_files: config.artifacts.length,
  });
  fixture(`language-${language}`, adapter.id, { execution_id: execution.id, artifact_id: artifact.id });
  return { adapter, artifact, execution, terminal };
}

const PYTHON_CODE = `from pathlib import Path\n\ndef handle(context, input):\n    item = context.input_files[0]\n    return {"name": item.original_name, "content": Path(item.path).read_text(), "input": input}\n`;
const JAVASCRIPT_CODE = `import fs from "node:fs";\n\nexport function handle(context, input) {\n  const item = context.inputFiles[0];\n  return { name: item.originalName, content: fs.readFileSync(item.path, "utf8"), input };\n}\n`;
const JAVA_CODE = `import java.nio.file.Files;\nimport java.util.LinkedHashMap;\nimport java.util.Map;\n\npublic class Adapter {\n    public Object handle(Context context, Object input) throws Exception {\n        InputFile item = context.inputFiles.get(0);\n        Map<String, Object> output = new LinkedHashMap<>();\n        output.put("name", item.originalName);\n        output.put("content", Files.readString(item.path));\n        output.put("input", input);\n        return output;\n    }\n}\n`;

async function scheduleProbe(pythonFixture) {
  const adapterId = pythonFixture.adapter.id;
  const patch = await api(`/api/adapters/${adapterId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_mode: "schedule" }),
  });
  if (patch.status !== 200) throw new Error(`schedule mode: ${patch.status}/${codeOf(patch)}`);
  const disabled = await api(`/api/adapters/${adapterId}/schedule`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: false, cron: "* * * * *", timezone: "UTC" }),
  });
  if (disabled.status !== 200) throw new Error(`schedule disabled setup: ${disabled.status}`);
  const enabled = await api(`/api/adapters/${adapterId}/schedule`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: true, cron: "* * * * *", timezone: "UTC" }),
  });
  if (enabled.status !== 200) throw new Error(`schedule enable: ${enabled.status}`);
  sql(`UPDATE adapter_schedules SET next_run_at = now() - interval '2 minutes' WHERE adapter_id = ${adapterId}`);
  const before = await api(`/api/adapters/${adapterId}/executions`);
  const beforeIds = new Set((before.body?.items ?? []).map((item) => item.id));
  let scheduled = null;
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const history = await api(`/api/adapters/${adapterId}/executions`);
    scheduled = (history.body?.items ?? []).find((item) => !beforeIds.has(item.id) && item.trigger === "schedule");
    if (scheduled && !["pending", "running"].includes(scheduled.status)) break;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  check("schedule-run", scheduled?.trigger === "schedule" && scheduled.status === "succeeded", { execution_id: scheduled?.id ?? null, execution_status: scheduled?.status ?? null });
  const runNow = await createExecution(adapterId);
  const runNowTerminal = await waitExecution(runNow.id);
  check("schedule-run-now", runNowTerminal.trigger === "manual" && runNowTerminal.status === "succeeded", { execution_id: runNow.id, execution_status: runNowTerminal.status });
  const reclosed = await api(`/api/adapters/${adapterId}/schedule`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: false, cron: "* * * * *", timezone: "UTC" }),
  });
  if (reclosed.status !== 200) throw new Error(`schedule disable: ${reclosed.status}`);
  await api(`/api/adapters/${adapterId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_mode: "manual" }),
  });
}

async function deleteAdapterAndWait(adapterId) {
  const response = await api(`/api/adapters/${adapterId}?stop=true`, { method: "DELETE" });
  if (![204, 202].includes(response.status)) throw new Error(`fixture adapter delete: ${response.status}/${codeOf(response)}`);
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    const row = parseRows(sql(`SELECT count(*) FILTER (WHERE status='DELETED'), count(*) FROM artifact_deletion_jobs WHERE former_adapter_id=${adapterId}`))[0];
    if (row?.[0] === row?.[1] && row?.[1] !== "0") return;
    if ((await api(`/api/adapters/${adapterId}`)).status === 404 && row?.[1] === "0") return;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`fixture adapter ${adapterId} deletion did not complete`);
}

async function quotaProbeSafe() {
  const before = await api("/api/system/managed-input-settings");
  if (before.status !== 200) throw new Error(`settings read: ${before.status}`);
  const settings = before.body;
  const payload = {};
  for (const key of ["default_retention_seconds", "max_file_bytes", "platform_quota_bytes", "adapter_quota_bytes", "allow_manual_delete", "max_custom_retention_seconds", "min_free_space_bytes", "staged_ttl_seconds"]) payload[key] = settings[key];
  // The minimum quota is 1 MiB, while the proxy's request limit is also
  // about 1 MiB.  Two sub-megabyte committed files followed by a third small
  // file therefore prove cumulative quota enforcement without a proxy 413.
  payload.adapter_quota_bytes = 2 * 1024 * 1024;
  const changed = await api("/api/system/managed-input-settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (changed.status !== 200) throw new Error(`quota setting: ${changed.status}/${codeOf(changed)}`);
  const adapter = await createTask(`E0 quota ${RUN_TAG}`, "python", "def handle(context, input):\n    return input\n");
  const first = await upload(adapter.id, "quota-near-limit.txt", "x".repeat(900_000));
  const second = await upload(adapter.id, "quota-second-file.txt", "y".repeat(900_000));
  const form = new FormData();
  form.append("file", new Blob(["z".repeat(100_000)], { type: "text/plain" }), "quota-over-limit.txt");
  const rejected = await api(`/api/adapters/${adapter.id}/input-artifacts`, { method: "POST", body: form });
  check("quota-enforcement", rejected.status === 409 && ["adapter_input_quota_exceeded", "platform_input_quota_exceeded"].includes(codeOf(rejected)), { adapter_id: adapter.id, response_status: rejected.status, error_code: codeOf(rejected) });
  payload.adapter_quota_bytes = settings.adapter_quota_bytes;
  const restore = await api("/api/system/managed-input-settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (restore.status !== 200) throw new Error(`quota restore: ${restore.status}/${codeOf(restore)}`);
  await deleteAdapterAndWait(adapter.id);
  return { adapter_id: adapter.id, first_artifact_id: first.id, second_artifact_id: second.id, rejected_status: rejected.status, rejected_code: codeOf(rejected) };
}

async function expiryProbe() {
  const adapter = await createTask(`E0 expiry ${RUN_TAG}`, "python", "def handle(context, input):\n    return input\n");
  const beforeCapacity = Number(parseRows(sql("SELECT actual_bytes FROM managed_input_capacity WHERE id=1"))[0]?.[0] ?? -1);
  const artifact = await upload(adapter.id, "expiry-staged.txt", "expiry fixture\n");
  sql(`UPDATE managed_input_artifacts SET expires_at = now() - interval '1 second' WHERE id = ${artifact.id}`);
  let row = null;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    row = parseRows(sql(`SELECT status, (SELECT actual_bytes FROM managed_input_capacity WHERE id=1), (SELECT reserved_bytes FROM managed_input_capacity WHERE id=1) FROM managed_input_artifacts WHERE id=${artifact.id}`))[0];
    if (row?.[0] === "DELETED") break;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  check("expiry-gc", row?.[0] === "DELETED" && Number(row?.[1] ?? -1) === beforeCapacity && row?.[2] === "0", { adapter_id: adapter.id, artifact_id: artifact.id, artifact_status: row?.[0] ?? null, capacity_actual_before: beforeCapacity, capacity_actual_after: Number(row?.[1] ?? -1), capacity_reserved_bytes: Number(row?.[2] ?? -1) });
}

async function deleteProbe() {
  const adapter = await createTask(`E0 delete ${RUN_TAG}`, "python", "def handle(context, input):\n    return input\n");
  const artifact = await upload(adapter.id, "delete-fixture.txt", "delete fixture\n");
  await saveManaged(adapter.id, [artifact.id]);
  const response = await api(`/api/adapters/${adapter.id}?stop=true`, { method: "DELETE" });
  if (![204, 202].includes(response.status)) throw new Error(`adapter delete: ${response.status}/${codeOf(response)}`);
  let row = null;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    row = parseRows(sql(`SELECT count(*) FILTER (WHERE status='DELETED'), count(*) FROM artifact_deletion_jobs WHERE former_adapter_id=${adapter.id}`))[0];
    if (row?.[0] === "1") break;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  const adapterGone = (await api(`/api/adapters/${adapter.id}`)).status === 404;
  check("adapter-delete-gc", adapterGone && row?.[0] === "1" && row?.[1] === "1", { adapter_id: adapter.id, delete_status: response.status, deletion_jobs_completed: Number(row?.[0] ?? -1), deletion_jobs_total: Number(row?.[1] ?? -1) });
}

function ownershipProbe() {
  const rows = docker(["ps", "-a", "--filter", `label=com.docker.compose.project=${PROJECT}`, "--format", "{{json .}}"]).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const containers = rows.map((row) => {
    const item = JSON.parse(docker(["inspect", row.Names]))[0];
    return { name: item.Name.replace(/^\//, ""), service: item.Config.Labels?.["com.docker.compose.service"] ?? null, ao_session: item.Config.Labels?.["ao.session"] ?? null, status: item.State.Status, health: item.State.Health?.Status ?? null, mounts: (item.Mounts ?? []).map((mount) => ({ destination_class: mount.Destination.includes("artifacts") ? "artifact_store" : mount.Destination.includes("postgres") ? "postgres_data" : mount.Destination.includes("journal") ? "worker_journal" : mount.Destination.includes("runtime") ? "worker_runtime" : mount.Destination.includes("platform-logs") ? "platform_logs" : "other", read_only: mount.RW === false })) };
  });
  const controlCount = containers.filter((item) => item.service === "control").length;
  const controlArtifactMounts = containers.filter((item) => item.service === "control").flatMap((item) => item.mounts).filter((mount) => mount.destination_class === "artifact_store").length;
  const workerArtifactMounts = containers.filter((item) => item.service === "worker").flatMap((item) => item.mounts).filter((mount) => mount.destination_class === "artifact_store").length;
  const valid = containers.length === 5 && containers.every((item) => item.ao_session === SESSION && item.health === "healthy") && controlCount === 1 && controlArtifactMounts === 1 && workerArtifactMounts === 0;
  check("single-control-resource-ownership", valid, { container_count: containers.length, control_count: controlCount, control_artifact_mounts: controlArtifactMounts, worker_artifact_mounts: workerArtifactMounts, containers });
  result.resource_ownership = { containers, expected_session: SESSION, control_count: controlCount, control_artifact_mounts: controlArtifactMounts, worker_artifact_mounts: workerArtifactMounts };
}

function sensitiveScan() {
  const logs = compose(["logs", "--no-color", "--tail", "1000", "control", "worker", "web", "account-web"], { timeout: 60_000 });
  const needles = [process.env.DLR_ADMIN_TOKEN, process.env.DLR_WORKER_TOKEN, process.env.DLR_MASTER_KEY, process.env.DLR_SECRET_SMOKE, REPO_ROOT, process.env.DLR_PLATFORM_LOG_ROOT].filter(Boolean);
  const matches = needles.filter((needle) => logs.includes(needle));
  result.sensitive_scan = { inspected_services: ["control", "worker", "web", "account-web"], matches: matches.length, scanned_line_count: logs.split("\n").length };
  check("sensitive-value-scan", matches.length === 0, result.sensitive_scan);
}

async function main() {
  await mkdir(OUTPUT_DIR, { recursive: true });
  const capability = await api("/api/system/managed-input-capability");
  check("capability", capability.status === 200 && capability.body?.managed_files_enabled === true && capability.body?.ready === true, { response_status: capability.status, managed_files_enabled: capability.body?.managed_files_enabled ?? null, ready: capability.body?.ready ?? null });
  result.managed_files_enabled = capability.body?.managed_files_enabled ?? null;
  const adminSettings = await api("/api/system/managed-input-settings");
  check("safe-admin-settings", adminSettings.status === 200 && adminSettings.body?.usage && !["artifact_store_root", "storage_path", "admin_token", "worker_token", "master_key"].some((key) => key in (adminSettings.body ?? {})), { response_status: adminSettings.status, has_usage: Boolean(adminSettings.body?.usage), forbidden_fields: ["artifact_store_root", "storage_path", "admin_token", "worker_token", "master_key"].filter((key) => key in (adminSettings.body ?? {})) });

  const pythonFixture = await runLanguage("python", PYTHON_CODE, "python-context.txt", "E0 python fixture\n", "D3 managed input acceptance");
  const javascriptFixture = await runLanguage("javascript", JAVASCRIPT_CODE, "javascript-context.txt", "E0 javascript fixture\n");
  const javaFixture = await runLanguage("java", JAVA_CODE, "java-context.txt", "E0 java fixture\n");
  fixture("browser-source", pythonFixture.adapter.id, { purpose: "Playwright target adapter" });
  await scheduleProbe(pythonFixture);
  await quotaProbeSafe();
  await expiryProbe();
  await deleteProbe();
  ownershipProbe();
  sensitiveScan();

  result.machine_gate = result.checks.every((item) => item.status === "PASS") ? "PASS" : "FAIL";
  await writeFile(join(OUTPUT_DIR, "compose-runtime.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ machine_gate: result.machine_gate, checks: result.checks.length, fixtures: result.fixtures.length }));
  if (result.machine_gate !== "PASS") throw new Error("E0 Compose runtime gate failed");
}

await main().catch(async (error) => {
  result.machine_gate = "FAIL";
  result.error = safeError(error);
  await mkdir(OUTPUT_DIR, { recursive: true });
  await writeFile(join(OUTPUT_DIR, "compose-runtime.partial.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
  throw error;
});
