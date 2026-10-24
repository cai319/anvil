import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 版本只维护 dev/VERSION；构建时注入前端，避免每次发布手改页面文件。
const version = readFileSync(
  fileURLToPath(new URL("../VERSION", import.meta.url)),
  "utf8",
).trim();

// Proxy API + SSE to the backend during dev（design §10：127.0.0.1:7317）。
export default defineConfig({
  plugins: [react()],
  define: {
    __ANVIL_VERSION__: JSON.stringify(version),
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7317",
        changeOrigin: true,
      },
    },
  },
});
