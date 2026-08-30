/* Write a deterministic, path-safe source-tree receipt for the D3 handoff. */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { join, relative } from "node:path";

const repoRoot = process.env.DLR_D3_REPO_ROOT ?? process.cwd();
const output = process.env.DLR_D3_SOURCE_OUTPUT ?? "docs/evidence/issue127-d3/source-tree.json";

function git(args) {
  return execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" });
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function listSourceFiles() {
  return git(["ls-files", "-co", "--exclude-standard", "-z"])
    .split("\0")
    .filter(Boolean)
    .filter((file) => !file.startsWith("docs/evidence/issue127-d3/"))
    .filter((file) => !file.startsWith("web/dist/"))
    .filter((file) => !file.includes("/node_modules/"));
}

const files = listSourceFiles();
const entries = [];
for (const file of files) {
  const buffer = await readFile(join(repoRoot, file));
  entries.push({ path: file, sha256: sha256(buffer) });
}
entries.sort((left, right) => left.path.localeCompare(right.path));
const workingTreeMaterial = entries.map((entry) => `${entry.path}\0${entry.sha256}\n`).join("");
const trackedDiff = git(["diff", "--binary", "HEAD", "--"]);
const status = git(["status", "--porcelain=v1"])
  .split("\n")
  .filter(Boolean)
  .map((line) => line.slice(0, 3) + line.slice(3));

const receipt = {
  wave: "Issue #127 D3",
  base_sha: git(["rev-parse", "HEAD"]).trim(),
  base_tree_sha: git(["rev-parse", "HEAD^{tree}"]).trim(),
  tracked_diff_sha256: sha256(trackedDiff),
  working_tree_sha256: sha256(workingTreeMaterial),
  source_file_count: entries.length,
  source_files: entries,
  git_status_porcelain: status,
  compose_project: "dlr-i127-d3-141",
  ao_session: "datalinkruntime-141-d3",
  human_acceptance: "待人工验收",
};

const outputPath = join(repoRoot, output);
await writeFile(outputPath, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
console.log(JSON.stringify({
  base_sha: receipt.base_sha,
  base_tree_sha: receipt.base_tree_sha,
  tracked_diff_sha256: receipt.tracked_diff_sha256,
  working_tree_sha256: receipt.working_tree_sha256,
  source_file_count: receipt.source_file_count,
  human_acceptance: receipt.human_acceptance,
}));
