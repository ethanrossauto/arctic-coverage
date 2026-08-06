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
  build: { outDir: "dist", sourcemap: true },
});
