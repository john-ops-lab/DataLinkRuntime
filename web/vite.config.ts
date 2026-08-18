/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    // Heavy Console render tests (drawer cross-Tab sync, whole-console locale
    // switches) can exceed the 5s default under parallel CI load.
    testTimeout: 15000,
  },
});
