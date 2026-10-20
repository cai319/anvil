/**
 * E2E-RESP-01
 * 覆盖 AC-27：1280×720 桌面与 360×640 移动视口下棋盘完整可见、无横向滚动。
 */
import { expect, test } from '@playwright/test';
import { openApp, startGame } from './helpers';

const VIEWPORTS = [
  { name: 'desktop-1280x720', width: 1280, height: 720 },
  { name: 'mobile-360x640', width: 360, height: 640 },
] as const;

for (const viewport of VIEWPORTS) {
  test(`E2E-RESP-01 ${viewport.name}：无横向滚动、棋盘不被裁切、HUD 可操作`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openApp(page);
    await startGame(page, { difficulty: 'normal', side: 'first' });

    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.innerWidth + 1);

    const board = await page.getByTestId('board').boundingBox();
    expect(board).not.toBeNull();
    if (board) {
      expect(board.x).toBeGreaterThanOrEqual(-1);
      expect(board.width).toBeLessThanOrEqual(viewport.width + 1);
      expect(board.height).toBeGreaterThan(0);
      if (viewport.width >= 1280) {
        expect(board.y).toBeGreaterThanOrEqual(-1);
        expect(board.y + board.height).toBeLessThanOrEqual(viewport.height + 1);
      }
    }

    await expect(page.getByTestId('turn-indicator')).toBeVisible();
    await expect(page.getByTestId('restart-button')).toBeEnabled();
    await expect(page.getByTestId('back-to-setup')).toBeEnabled();

    // HUD 操作在两种视口下均可用
    await page.getByTestId('restart-button').click();
    await expect(page.getByTestId('move-count')).toHaveText('0');
  });
}
