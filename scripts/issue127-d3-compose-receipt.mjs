/* Capture only exact D3 Compose ownership facts before teardown. */

import { execFileSync } from "node:child_process";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";

const project = "dlr-i127-d3-141";
const session = "datalinkruntime-141-d3";
const output = process.env.DLR_D3_COMPOSE_OUTPUT ?? "docs/evidence/issue127-d3/compose-resources.json";

function docker(args) {
  return execFileSync("docker", args, { encoding: "utf8" });
}

function jsonLines(args) {
  return docker(args)
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function inspect(name) {
  return JSON.parse(docker(["inspect", name]))[0];
}

const containerRows = jsonLines([
  "ps", "-a", "--filter", `label=com.docker.compose.project=${project}`, "--format", "{{json .}}",
]);
const containers = containerRows.map((row) => {
  const item = inspect(row.Names);
  return {
    name: item.Name.replace(/^\//, ""),
    image: item.Config.Image,
    image_id: item.Image,
    service: item.Config.Labels?.["com.docker.compose.service"] ?? null,
    ao_session: item.Config.Labels?.["ao.session"] ?? null,
    compose_project: item.Config.Labels?.["com.docker.compose.project"] ?? null,
    status: item.State.Status,
    health: item.State.Health?.Status ?? null,
    ports: Object.values(item.NetworkSettings.Ports ?? {})
      .flatMap((bindings) => bindings ?? [])
      .map((binding) => `${binding.HostPort}->container`),
    mounts: (item.Mounts ?? []).map((mount) => ({
      type: mount.Type,
      destination_class: mount.Destination.includes("platform-logs")
        ? "platform_logs"
        : mount.Destination.includes("artifacts")
          ? "artifact_store"
          : mount.Destination.includes("postgres")
            ? "postgres_data"
            : mount.Destination.includes("journal")
              ? "worker_journal"
              : mount.Destination.includes("runtime")
                ? "worker_runtime"
                : "other_managed_mount",
      read_only: mount.RW === false,
    })),
  };
});

const volumeRows = jsonLines([
  "volume", "ls", "--filter", `label=com.docker.compose.project=${project}`, "--format", "{{json .}}",
]);
const volumes = volumeRows.map((row) => {
  const item = inspect(row.Name);
  return {
    name: item.Name,
    driver: item.Driver,
    labels: {
      compose_project: item.Labels?.["com.docker.compose.project"] ?? null,
      compose_volume: item.Labels?.["com.docker.compose.volume"] ?? null,
    },
  };
});

const networkRows = jsonLines([
  "network", "ls", "--filter", `label=com.docker.compose.project=${project}`, "--format", "{{json .}}",
]);
const networks = networkRows.map((row) => {
  const item = inspect(row.Name);
  return {
    name: item.Name,
    driver: item.Driver,
    labels: { compose_project: item.Labels?.["com.docker.compose.project"] ?? null },
    attached_containers: Object.keys(item.Containers ?? {}).length,
  };
});

const imageRows = jsonLines(["image", "ls", "--format", "{{json .}}"]);
const images = imageRows
  .filter((row) => row.Repository?.startsWith(`${project}-`))
  .map((row) => ({ repository: row.Repository, tag: row.Tag, image_id: row.ID, size: row.Size }));

const receipt = {
  project,
  ao_session: session,
  ports: { web: 8923, account: 9023 },
  managed_files_enabled_during_reclose: true,
  containers,
  volumes,
  networks,
  images,
  cleanup_scope: {
    compose_command: `docker compose -p ${project} down --volumes --remove-orphans`,
    image_references: images.map((image) => `${image.repository}:${image.tag}`),
    host_log_directory: "omitted from receipt; exact D3-only path was session-owned",
  },
  unrelated_resources_touched: false,
  human_acceptance: "待人工验收",
};

await writeFile(join(process.cwd(), output), `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
console.log(JSON.stringify({
  project,
  ao_session: session,
  containers: containers.length,
  volumes: volumes.length,
  networks: networks.length,
  images: images.length,
  human_acceptance: receipt.human_acceptance,
}));
