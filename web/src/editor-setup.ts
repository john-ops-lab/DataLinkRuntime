/** Bundle Monaco locally so the console works without internet access.
 *
 * Imported once from main.tsx; must run before any Editor is mounted.
 */

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
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

loader.config({ monaco });
