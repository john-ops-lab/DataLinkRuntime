/** Bundle Monaco locally so the console works without internet access.
 *
 * Imported once from main.tsx; must run before any Editor is mounted.
 */

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/editor/editor.worker?worker";
import "monaco-editor/languages/definitions/python/register";

self.MonacoEnvironment = {
  getWorker: () => new editorWorker(),
};

loader.config({ monaco });
