import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the server package so one container serves API, MCP and viewer.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "../server/src/ctx_server/static/app",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8080",
      "/authorize": "http://localhost:8080",
      "/token": "http://localhost:8080",
    },
  },
});
