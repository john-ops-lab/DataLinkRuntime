/** M5.5.10 unified log helpers shared by the Workbench live-log Tab and the
 * execution history detail. */

/** Merged display content: the unified stdout-channel stream plus any legacy
 * stderr content (pre-M5.5.10 rows), so one view always carries everything. */
export function unifiedLogContent(stdout: string, stderr: string): string {
  if (stderr === "") {
    return stdout;
  }
  if (stdout === "") {
    return stderr;
  }
  return `${stdout}\n${stderr}`;
}
