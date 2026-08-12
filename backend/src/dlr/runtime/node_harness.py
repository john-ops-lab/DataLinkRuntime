"""Embedded Node.js harness for the JavaScript Adapter contract."""

SOURCE = r"""import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const workspace = process.argv[2];
const adapterPath = process.argv[3];
const input = JSON.parse(fs.readFileSync(path.join(workspace, "input.json"), "utf8"));
const config = JSON.parse(fs.readFileSync(path.join(workspace, "runtime_config.json"), "utf8"));

const context = {
  config,
  secrets: { get(key) { return process.env[`DLR_SECRET_${key}`] ?? null; } },
  logger: {
    info(...args) { console.log(...args); },
    warn(...args) { console.warn(...args); },
    error(...args) { console.error(...args); },
  },
};

try {
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
  console.error(error?.stack ?? String(error));
  process.exitCode = 1;
}
"""
