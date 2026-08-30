"""Embedded Node.js harness for the JavaScript Adapter contract."""

SOURCE = r"""import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const workspace = process.argv[2];
const adapterPath = process.argv[3];
const manifestFields = new Set([
  "artifact_id",
  "ordinal",
  "mount_name",
  "original_filename",
  "content_type",
  "size_bytes",
  "sha256",
]);

class InputManifestError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function failInput(code) {
  throw new InputManifestError(code);
}

function isInteger(value, positive = false) {
  return Number.isInteger(value) && (!positive || value > 0);
}

function validateInputFile(filePath) {
  let descriptor;
  try {
    // Openability only; the Worker owns size/SHA verification.
    const info = fs.lstatSync(filePath);
    if (info.isSymbolicLink() || !info.isFile()) failInput("input_artifact_not_ready");
    const noFollow = fs.constants.O_NOFOLLOW ?? 0;
    descriptor = fs.openSync(filePath, fs.constants.O_RDONLY | noFollow);
    const opened = fs.fstatSync(descriptor);
    if (!opened.isFile()) failInput("input_artifact_not_ready");
  } catch (error) {
    if (error instanceof InputManifestError) throw error;
    failInput("input_artifact_not_ready");
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* best effort */ }
    }
  }
}

function inputFilesFromManifest() {
  if (!path.isAbsolute(workspace)) failInput("input_artifact_not_ready");
  const workspaceMatch = path.basename(workspace).match(/^dlr-exec-([1-9][0-9]*)$/);
  if (workspaceMatch === null) failInput("input_artifact_not_ready");
  const executionId = Number(workspaceMatch[1]);
  const inputDir = path.join(workspace, "input");
  const manifestPath = path.join(workspace, "input_manifest.json");
  let inputInfo;
  let manifestInfo;
  let manifest;
  try {
    inputInfo = fs.lstatSync(inputDir);
    manifestInfo = fs.lstatSync(manifestPath);
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch {
    failInput("input_artifact_not_ready");
  }
  if (
    inputInfo.isSymbolicLink() || !inputInfo.isDirectory()
    || manifestInfo.isSymbolicLink() || !manifestInfo.isFile()
    || manifest === null || Array.isArray(manifest) || typeof manifest !== "object"
    || Object.keys(manifest).length !== 2
    || !Object.hasOwn(manifest, "execution_id")
    || !Object.hasOwn(manifest, "files")
    || manifest.execution_id !== executionId
    || !Array.isArray(manifest.files) || manifest.files.length > 8
  ) {
    failInput("input_artifact_not_ready");
  }
  const files = [];
  manifest.files.forEach((raw, expectedOrdinal) => {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
      failInput("input_artifact_not_ready");
    }
    const keys = Object.keys(raw);
    if (keys.length !== manifestFields.size || keys.some((key) => !manifestFields.has(key))) {
      failInput("input_artifact_not_ready");
    }
    const mountMatch = typeof raw.mount_name === "string"
      ? raw.mount_name.match(/^input-([0-9]{2})(?:\.[a-z0-9]{1,10})?$/)
      : null;
    if (
      !isInteger(raw.artifact_id, true)
      || raw.ordinal !== expectedOrdinal
      || raw.ordinal < 0 || raw.ordinal > 7
      || mountMatch === null || Number(mountMatch[1]) !== expectedOrdinal
      || typeof raw.original_filename !== "string"
      || typeof raw.content_type !== "string"
      || !isInteger(raw.size_bytes) || raw.size_bytes < 0
      || typeof raw.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(raw.sha256)
    ) {
      failInput("input_artifact_not_ready");
    }
    const target = path.join(inputDir, raw.mount_name);
    if (path.dirname(target) !== inputDir || !path.isAbsolute(target)) {
      failInput("input_artifact_not_ready");
    }
    validateInputFile(target);
    files.push(Object.freeze({
      ordinal: expectedOrdinal,
      path: target,
      originalName: raw.original_filename,
      contentType: raw.content_type,
      sizeBytes: raw.size_bytes,
      sha256: raw.sha256,
    }));
  });
  return Object.freeze(files);
}

try {
  const input = JSON.parse(fs.readFileSync(path.join(workspace, "input.json"), "utf8"));
  const config = JSON.parse(fs.readFileSync(path.join(workspace, "runtime_config.json"), "utf8"));
  const inputFiles = inputFilesFromManifest();
  const context = {
    config,
    inputFiles,
    secrets: { get(key) { return process.env[`DLR_SECRET_${key}`] ?? null; } },
    logger: {
      info(...args) { console.log("[INFO]", ...args); },
      warn(...args) { console.warn("[WARN]", ...args); },
      error(...args) { console.error("[ERROR]", ...args); },
    },
  };
  const module = await import(pathToFileURL(adapterPath).href);
  if (typeof module.handle !== "function") {
    throw new Error("adapter must export a handle(context, input) function");
  }
  const output = await module.handle(context, input);
  const serialized = JSON.stringify(output);
  if (serialized === undefined) {
    throw new Error("adapter output is not JSON-serializable");
  }
  fs.writeFileSync(path.join(workspace, "output.json"), serialized, "utf8");
} catch (error) {
  if (error instanceof InputManifestError) {
    // Diagnostic only. The Worker preflight owns the structured error code.
    console.error(`DLR_INPUT_ERROR:${error.code}`);
  } else {
    console.error(error?.stack ?? String(error));
  }
  process.exitCode = 1;
}
"""
