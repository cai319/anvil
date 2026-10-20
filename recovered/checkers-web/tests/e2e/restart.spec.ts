/**
 * E2E-RESTART-01 ~ 05
 * 覆盖 AC-17/24/25/26：重开、返回设置、AI 思考中中止与旧结果丢弃。
 */
import { expect, test } from '@playwright/test';
import {
  clickCell,
  expectInitialBoard,
  fixtureAInput,
  loadFixture,
  openApp,
  setAiMode,
  snapshot,
  startGame,
  waitForPhase,
} from './helpers';

test.describe('重开与返回设置', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
  });

  test('E2E-RESTART-01 人类回合重开：gameId+1、初始布局、ply=0', async ({ page }) => {
    await startGame(page, { difficulty: 'normal', side: 'first' });
    await clickCell(page, [1, -5, 4]);
    await clickCell(page, [1, -4, 3]);
    await waitForPhase(page, 'HUMAN_TURN', 5000);

    const before = await snapshot(page);
    expect(before.ply).toBe(2);

    await page.getByTestId('restart-button').click();
    await expect(page.getByTestId('board')).toHaveAttribute(
      'data-game-id',
      String(before.gameId + 1),
    );
    await expect(page.getByTestId('move-count')).toHaveText('0');
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'HUMAN_TURN');
    await expectInitialBoard(page);

    const after = await snapshot(page);
    expect(after.gameId).toBe(before.gameId + 1);
    expect(after.lastMove).toBeNull();
    expect(after.config).toEqual({ difficulty: 'normal', humanSide: 'first' });
  });

  test('E2E-RESTART-02 AI 思考中重开：旧 AI 结果不会落入新局', async ({ page }) => {
    await setAiMode(page, 'delay', { delayMs: 1500 });
    await startGame(page, { difficulty: 'normal', side: 'first' });
    await clickCell(page, [1, -5, 4]);
    await clickCell(page, [1, -4, 3]);
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');

    // 等 AI 请求真正进入思考（min delay 350ms 之后）再重开
    await page.waitForTimeout(450);
    const before = await snapshot(page);
    await page.getByTestId('restart-button').click();

    await expect(page.getByTestId('board')).toHaveAttribute(
      'data-game-id',
      String(before.gameId + 1),
    );
    await expect(page.getByTestId('move-count')).toHaveText('0');

    // 旧 AI 本应在 ~1850ms 后返回；等待 2000ms 验证其被丢弃
    await page.waitForTimeout(2000);
    const after = await snapshot(page);
    expect(after.gameId).toBe(before.gameId + 1);
    expect(after.ply).toBe(0);
    expect(after.phase).toBe('HUMAN_TURN');
    await expectInitialBoard(page);
    await expect(page.getByTestId('move-count')).toHaveText('0');
  });

  test('E2E-RESTART-03 返回设置保留难度与先后手；再次开始沿用', async ({ page }) => {
    await page.getByTestId('difficulty-hard').click();
    await page.getByTestId('side-second').click();
    await page.getByTestId('start-game').click();
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
    await expect(page.getByTestId('turn-text')).toContainText('AI 思考中');

    await page.getByTestId('back-to-setup').click();
    await expect(page.getByTestId('setup-panel')).toBeVisible();
    await expect(page.getByTestId('board')).toBeHidden();

    // 不修改选项，直接再次开始
    await page.getByTestId('start-game').click();
    await expect(page.getByTestId('board')).toBeVisible();
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
    const state = await snapshot(page);
    expect(state.config).toEqual({ difficulty: 'hard', humanSide: 'second' });
    expect(state.humanColor).toBe('B');
    expect(state.ply).toBe(0);
  });

  test('E2E-RESTART-04 终局结果展示与弹层重开', async ({ page }) => {
    await loadFixture(page, fixtureAInput());
    await waitForPhase(page, 'HUMAN_TURN');

    await clickCell(page, [-3, 4, -1]);
    await clickCell(page, [-4, 5, -1]);
    await expect(page.getByTestId('game-result')).toBeVisible();
    await expect(page.getByTestId('game-result-title')).toHaveText('红方获胜');
    await expect(page.getByTestId('game-result-subtitle')).toHaveText('恭喜，你赢了');
    await expect(page.getByTestId('result-back-to-setup')).toBeVisible();

    await page.getByTestId('result-restart-button').click();
    await expect(page.getByTestId('game-result')).toBeHidden();
    await expect(page.getByTestId('move-count')).toHaveText('0');
    await expectInitialBoard(page);
  });

  test('E2E-RESTART-05 AI 思考中返回设置：中止并丢弃旧结果', async ({ page }) => {
    await setAiMode(page, 'delay', { delayMs: 1500 });
    await startGame(page, { difficulty: 'normal', side: 'first' });
    await clickCell(page, [1, -5, 4]);
    await clickCell(page, [1, -4, 3]);
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');

    await page.waitForTimeout(450);
    await page.getByTestId('back-to-setup').click();
    await expect(page.getByTestId('setup-panel')).toBeVisible();
    await expect(page.getByTestId('board')).toBeHidden();

    // 旧 AI 结果晚到：仍停留在 SETUP，不产生任何落子
    await page.waitForTimeout(2000);
    await expect(page.getByTestId('setup-panel')).toBeVisible();
    await expect(page.getByTestId('board')).toBeHidden();

    // 再次开始：全新局面（ply=0，人类先手）
    await page.getByTestId('start-game').click();
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'HUMAN_TURN');
    await expect(page.getByTestId('move-count')).toHaveText('0');
    await expectInitialBoard(page);
  });
});
