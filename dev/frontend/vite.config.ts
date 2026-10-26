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
  build: {
    rollupOptions: {
      output: {
        // React Flow 只在模板/工作流图页面使用；拆出独立 vendor chunk，
        // 避免单个入口包超过 Vite 500 kB 告警阈值，也便于浏览器缓存。
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@xyflow")) return "vendor-flow";
          // react-dom 依赖 scheduler；react 生态放同一 chunk，避免循环 chunk。
          if (/node_modules\/(react|react-dom|scheduler|react-is|use-sync-external-store)\//.test(id)) {
            return "vendor-react";
          }
          return "vendor";
        },
      },
    },
  },
});
