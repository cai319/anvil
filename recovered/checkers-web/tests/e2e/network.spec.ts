/**
 * E2E-NET-01
 * 覆盖 AC-43：纯前端、运行时零外部请求、无 CDN/远程字体/后端 API。
 */
import { expect, test } from '@playwright/test';
import {
  clickCell,
  openApp,
  setAiMode,
  startGame,
  waitForPhase,
} from './helpers';

test('E2E-NET-01 从加载到对局、AI 落子、重开：全部请求同源', async ({ page }) => {
  const urls: string[] = [];
  page.on('request', (request) => urls.push(request.url()));

  await openApp(page);
  await setAiMode(page, 'delay', { delayMs: 0 });
  await startGame(page, { difficulty: 'normal', side: 'first' });

  // 人类走子 → AI 落子
  await clickCell(page, [1, -5, 4]);
  await clickCell(page, [1, -4, 3]);
  await waitForPhase(page, 'HUMAN_TURN', 5000);

  // 重开一次，覆盖更多资源加载路径
  await page.getByTestId('restart-button').click();
  await expect(page.getByTestId('move-count')).toHaveText('0');

  expect(urls.length).toBeGreaterThan(0);
  const origin = new URL(page.url()).origin;
  for (const url of urls) {
    if (url.startsWith('data:') || url.startsWith('blob:')) continue;
    expect(new URL(url).origin, `external request detected: ${url}`).toBe(origin);
  }
});
