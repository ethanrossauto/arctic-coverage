import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the Python API runs under uvicorn on 8000 and Vite proxies to it, so the
// browser only ever talks to one origin. That keeps dev and production identical
// from the frontend's point of view: same paths, no CORS, no environment switch.
//   terminal 1: .venv/bin/uvicorn api.index:app --reload --port 8000
//   terminal 2: npm run dev
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  // ⚠️ MapLibre must be excluded from Vite's dependency pre-bundling. MapLibre
  // ships a web worker that does all geometry parsing, and the optimizer rewrites
  // the main entry without emitting the worker chunk, so the worker request 404s.
  // The map then renders NOTHING while the rest of the page works perfectly and no
  // exception is thrown. Found by a Playwright test asserting zero failed
  // requests, after an evening of screenshots failed to explain a blank canvas.
  optimizeDeps: { exclude: ["maplibre-gl"] },
  worker: { format: "es" },
  build: { outDir: "dist", sourcemap: true },
});
