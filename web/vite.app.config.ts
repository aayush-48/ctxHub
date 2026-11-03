import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// The in-chat views (MCP Apps). Hosts render a ui:// resource as one HTML document in a
// sandboxed iframe that cannot load other files, so everything is inlined into index.html,
// which the server serves as ui://ctx/app.html.
export default defineConfig({
  root: "mcp-app",
  plugins: [react(), viteSingleFile()],
  build: {
    outDir: "../../server/src/ctx_server/static/mcp_app",
    emptyOutDir: true,
  },
});
