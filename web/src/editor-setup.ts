/** Bundle Monaco locally so the console works without internet access.
 *
 * Imported once from main.tsx; must run before any Editor is mounted.
 */

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import { typescriptDefaults, ModuleKind, ModuleResolutionKind, ScriptTarget } from "monaco-editor/languages/features/typescript/register";
import { DLR_TYPES } from "./runtime-types";
import cssWorker from "monaco-editor/language/css/css.worker?worker";
import editorWorker from "monaco-editor/editor/editor.worker?worker";
import htmlWorker from "monaco-editor/language/html/html.worker?worker";
import jsonWorker from "monaco-editor/language/json/json.worker?worker";
import tsWorker from "monaco-editor/language/typescript/ts.worker?worker";
import "monaco-editor/languages/definitions/python/register";

self.MonacoEnvironment = {
  getWorker: (_moduleId, label) => {
    if (label === "json") {
      return new jsonWorker();
    }
    if (label === "css" || label === "scss" || label === "less") {
      return new cssWorker();
    }
    if (label === "html" || label === "handlebars" || label === "razor") {
      return new htmlWorker();
    }
    if (label === "typescript" || label === "javascript") {
      return new tsWorker();
    }
    return new editorWorker();
  },
};

typescriptDefaults.setCompilerOptions({
  strict: true,
  target: ScriptTarget.ESNext,
  module: ModuleKind.ESNext,
  moduleResolution: ModuleResolutionKind.NodeJs,
  allowNonTsExtensions: true,
  esModuleInterop: true,
});
// Third-party declarations are checked on the selected Worker, where dependencies exist.
typescriptDefaults.setDiagnosticsOptions({ diagnosticCodesToIgnore: [2307, 2688, 7016] });
typescriptDefaults.addExtraLib(DLR_TYPES, "file:///dlr-runtime.d.ts");
loader.config({ monaco });
