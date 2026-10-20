import { defineConfig, devices } from '@playwright/test';

const PORT = 4173;
const BASE_URL = `http://127.0.0.1:${PORT}`;

/**
 * E2E 配置：构建后以静态预览服务启动 dist/，覆盖 Chromium 与 WebKit。
 * 测试用例由 S5 提供（tests/e2e/**）。
 */
export default defineConfig({
  testDir: 'tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  timeout: 45_000,
  expect: { timeout: 8_000 },
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `npm run build && npm run preview -- --host 127.0.0.1 --port ${PORT}`,
    url: BASE_URL,
    // 始终构建并启动本次 E2E 自己的服务，避免复用旧构建（S7 回归中发现过陈旧静态服务）。
    reuseExistingServer: false,
    timeout: 180_000,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
