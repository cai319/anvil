/**
 * E2E-SECOND-01 ~ 02
 * 覆盖 AC-05/22/41：「我后手」时 AI 自动首手、思考期锁定、完整对局。
 */
import { expect, test } from '@playwright/test';
import {
  LOWER_CAMP,
  clickCell,
  coordKey,
  forceClickCell,
  getLegalMoves,
  openApp,
  playFullGame,
  setAiMode,
  snapshot,
  startGame,
  tupleKey,
  waitForPhase,
  waitForPly,
} from './helpers';

test.describe('人类后手（AI 先手）', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
  });

  test('E2E-SECOND-01 后手开局：AI 自动首手 ≤2000ms，思考期人类不可落子', async ({
    page,
  }) => {
    // 延迟 1000ms，留出稳定的“思考中”观察窗口；仍满足 ≤2000ms
    await setAiMode(page, 'delay', { delayMs: 1000 });
    await page.getByTestId('side-second').click();

    await page.getByTestId('start-game').click();
    await expect(page.getByTestId('board')).toBeVisible();

    // 立即进入 AI_THINKING
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
    await expect(page.getByTestId('ai-thinking')).toBeVisible();
    await expect(page.getByTestId('turn-text')).toContainText('AI 思考中');
    await expect(page.getByTestId('turn-text')).toContainText('红方');
    await expect(page.getByTestId('move-count')).toHaveText('0');

    // FR-28 / AC-32：计时从「进入 AI_THINKING」到落子应用完成，不含 SETUP 点击与页面操作开销。
    const startedAt = Date.now();

    // 思考期间点击棋盘：不改变任何状态
    const before = await snapshot(page);
    await forceClickCell(page, [0, 0, 0]);
    await forceClickCell(page, [-4, 8, -4]);
    await forceClickCell(page, [1, -5, 4]);
    const after = await snapshot(page);
    expect(after.phase).toBe('AI_THINKING');
    expect(after.ply).toBe(0);
    expect(after.pieces).toEqual(before.pieces);
    expect(after.selection).toBeNull();

    // AI 自动首手完成
    await waitForPly(page, 1, 2000);
    const elapsed = Date.now() - startedAt;
    expect(elapsed).toBeLessThanOrEqual(2000);

    await waitForPhase(page, 'HUMAN_TURN', 1000);
    await expect(page.getByTestId('move-count')).toHaveText('1');
    await expect(page.getByTestId('turn-text')).toHaveText('你的回合 · 蓝方');
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'HUMAN_TURN');

    const state = await snapshot(page);
    expect(state.humanColor).toBe('B');
    expect(state.aiColor).toBe('R');
    expect(state.turn).toBe('B');

    // 恰好一枚红子离开初始的下营
    const reds = state.pieces.filter((piece) => piece.color === 'R');
    expect(reds).toHaveLength(10);
    const lower = new Set(LOWER_CAMP.map(tupleKey));
    const moved = reds.filter((piece) => !lower.has(coordKey(piece.pos)));
    expect(moved).toHaveLength(1);

    // 人类走子后进入 AI 回合，ply=2
    const moves = await getLegalMoves(page);
    expect(moves.length).toBeGreaterThan(0);
    const move = moves[0];
    await clickCell(page, move.piece);
    for (const step of move.path) {
      await clickCell(page, step);
    }
    await expect(page.getByTestId('move-count')).toHaveText('2');
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
  });

  test('E2E-SECOND-02 后手完整对局：AI 首手 → 人类操作 → GAME_OVER', async ({ page }) => {
    test.setTimeout(240_000);
    await setAiMode(page, 'delay', { delayMs: 0 });
    await startGame(page, { difficulty: 'normal', side: 'second' });

    // 等待 AI 首手完成
    await waitForPhase(page, 'HUMAN_TURN', 5000);
    expect((await snapshot(page)).ply).toBe(1);

    const final = await playFullGame(page, 300);
    expect(final.phase).toBe('GAME_OVER');
    await expect(page.getByTestId('game-result')).toBeVisible();
    const title = (await page.getByTestId('game-result-title').textContent())?.trim();
    expect(['红方获胜', '蓝方获胜', '平局']).toContain(title);

    // 重开沿用「我后手」：新局应再次由 AI 自动先走
    await page.getByTestId('result-restart-button').click();
    await expect(page.getByTestId('game-result')).toBeHidden();
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
    await waitForPly(page, 1, 5000);
    await expect(page.getByTestId('turn-text')).toHaveText('你的回合 · 蓝方');
  });
});
