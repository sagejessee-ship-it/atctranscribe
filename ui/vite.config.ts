/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the browser talks to Vite; /api and /audio go to the review
// edge server (`aerochorus ui serve`), which proxies the control plane and
// streams read-only source audio. Production: the edge serves ui/dist itself.
const edge = process.env.AEROCHORUS_EDGE_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: edge, changeOrigin: true },
      "/audio": { target: edge, changeOrigin: true },
      "/edge": { target: edge, changeOrigin: true },
    },
  },
  // One bundle (~170 kB gzip) served from the local edge. Manual vendor chunks
  // broke React's init order (radix/tanstack split), so they are deliberately absent.
  build: { outDir: "dist", sourcemap: true, chunkSizeWarningLimit: 800 },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test-setup.ts"],
  },
});
