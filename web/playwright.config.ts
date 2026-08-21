import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: "./test-results/m5-10-wave-a",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: [
    ["list"],
    ["json", { outputFile: "./test-results/m5-10-wave-a/results.json" }],
  ],
  use: {
    baseURL: process.env.DLR_BROWSER_BASE_URL ?? "http://127.0.0.1:4173",
    browserName: "chromium",
    colorScheme: "light",
    screenshot: "off",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
