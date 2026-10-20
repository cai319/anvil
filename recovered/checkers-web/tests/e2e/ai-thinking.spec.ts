/**
 * E2E-LOCK-01 ~ 02
 * 覆盖 AC-22：AI 思考期间所有棋盘输入被忽略，结束后恢复人类操作。
 */
import { expect, test } from '@playwright/test';
import {
  clickCell,
  forceClickCell,
  openApp,
  setAiMode,
  snapshot,
  startGame,
  waitForPhase,
} from './helpers';

async function enterAiThinking(page: import('@playwright/test').Page): Promise<void> {
  await setAiMode(page, 'delay', { delayMs: 1200 });
  await startGame(page, { difficulty: 'normal', side: 'first' });
  await clickCell(page, [1, -5, 4]);
  await clickCell(page, [1, -4, 3]);
  await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
}

test.describe('AI 思考态禁止落子', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
  });

  test('E2E-LOCK-01 思考中点击棋盘/棋子均不改变状态，AI 只落一子', async ({ page }) => {
    await enterAiThinking(page);

    await expect(page.getByTestId('ai-thinking')).toBeVisible();
    await expect(page.getByTestId('turn-text')).toContainText('AI 思考中');
    await expect(page.getByTestId('board')).toHaveAttribute('aria-busy', 'true');
    await expect(page.getByTestId('move-count')).toHaveText('1');

    const before = await snapshot(page);
    await forceClickCell(page, [0, 0, 0]);
    await forceClickCell(page, [-4, 8, -4]);
    await forceClickCell(page, [2, -5, 3]);
    await forceClickCell(page, [1, -4, 3]);
    await page.waitForTimeout(200);

    const during = await snapshot(page);
    expect(during.phase).toBe('AI_THINKING');
    expect(during.ply).toBe(before.ply);
    expect(during.pieces).toEqual(before.pieces);
    expect(during.selection).toBeNull();
    // 思考期点击静默忽略，不弹提示
    await expect(page.getByTestId('hint')).toHaveText('');

    await waitForPhase(page, 'HUMAN_TURN', 3000);
    const after = await snapshot(page);
    expect(after.ply).toBe(before.ply + 1);
    await expect(page.getByTestId('move-count')).toHaveText(String(before.ply + 1));
    await expect(page.getByTestId('ai-thinking')).toBeHidden();
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'HUMAN_TURN');
  });

  test('E2E-LOCK-02 思考中强制点击隐藏的「结束回合」无效', async ({ page }) => {
    await enterAiThinking(page);

    await expect(page.getByTestId('end-turn')).toBeHidden();
    const before = await snapshot(page);
    await page.getByTestId('end-turn').click({ force: true }).catch(() => undefined);
    await page.waitForTimeout(100);

    const after = await snapshot(page);
    expect(after.phase).toBe('AI_THINKING');
    expect(after.ply).toBe(before.ply);
    expect(after.pieces).toEqual(before.pieces);
    await expect(page.getByTestId('hint')).toHaveText('');

    await waitForPhase(page, 'HUMAN_TURN', 3000);
    expect((await snapshot(page)).ply).toBe(before.ply + 1);
  });
});
