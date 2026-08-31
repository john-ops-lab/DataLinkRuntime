/*
 * Create a reproducible source-tree receipt for the Issue #127 E0 handoff.
 *
 * The receipt deliberately excludes evidence and runtime/build output.  It is
 * therefore safe to write the receipt below docs/evidence/issue127-e0 without
 * changing the content tree that it describes.
 */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

const repoRoot = process.env.DLR_E0_REPO_ROOT ?? process.cwd();
const output = process.env.DLR_E0_SOURCE_OUTPUT ?? "docs/evidence/issue127-e0/source-candidate/source-tree.json";

function git(args) {
  return execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" });
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function isExcluded(file) {
  return file.startsWith("docs/evidence/")
    || file.startsWith(".tmp-platform-logs/")
    || file.startsWith("web/node_modules/")
    || file.startsWith("web/dist/")
    || file.includes("/node_modules/")
    || file.includes("/__pycache__/")
    || file.endsWith("/.pytest_cache");
}

function listSourceFiles() {
  return git(["ls-files", "-co", "--exclude-standard", "-z"])
    .split("\0")
    .filter(Boolean)
    .filter((file) => !isExcluded(file))
    .sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
}

const files = listSourceFiles();
const entries = [];
for (const file of files) {
  const content = await readFile(join(repoRoot, file));
  entries.push({ path: file, bytes: content.byteLength, sha256: sha256(content) });
}

const contentTree = entries.map((entry) => `${entry.path}\0${entry.sha256}\n`).join("");
const trackedDiff = git(["diff", "--binary", "HEAD", "--"]);
const status = git(["status", "--porcelain=v1"])
  .split("\n")
  .filter(Boolean)
  .map((line) => line.slice(0, 3) + line.slice(3));

const receipt = {
  schema: "issue127-e0-source-tree-v1",
  algorithm: "sort repository-relative paths by bytewise lexical order; hash each file with SHA-256; hash the UTF-8 stream path + NUL + file_sha256 + LF",
  excluded_prefixes: [
    "docs/evidence/",
    ".tmp-platform-logs/",
    "web/node_modules/",
    "web/dist/",
    "**/node_modules/",
    "**/__pycache__/",
  ],
  base_sha: git(["rev-parse", "HEAD"]).trim(),
  base_tree_sha: git(["rev-parse", "HEAD^{tree}"]).trim(),
  tracked_diff_sha256: sha256(trackedDiff),
  working_tree_sha256: sha256(contentTree),
  source_file_count: entries.length,
  source_files: entries,
  git_status_porcelain: status,
  compose_project: process.env.COMPOSE_PROJECT_NAME ?? "dlr-i127-e0-141",
  ao_session: process.env.AO_SESSION_ID ?? "datalinkruntime-141-e0",
  human_acceptance: "待人工验收",
};

const outputPath = join(repoRoot, output);
await mkdir(dirname(outputPath), { recursive: true });
await writeFile(outputPath, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
console.log(JSON.stringify({
  base_sha: receipt.base_sha,
  base_tree_sha: receipt.base_tree_sha,
  tracked_diff_sha256: receipt.tracked_diff_sha256,
  working_tree_sha256: receipt.working_tree_sha256,
  source_file_count: receipt.source_file_count,
  human_acceptance: receipt.human_acceptance,
}));
