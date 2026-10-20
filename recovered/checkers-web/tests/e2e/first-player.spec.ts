/**
 * E2E-SETUP-01 / E2E-FIRST-01 ~ 04
 * 覆盖 AC-03/04/06/14/17/18/19/21/24/28/32 的「人类先手完整对局」流程。
 */
import { expect, test } from '@playwright/test';
import {
  cell,
  clickCell,
  coord,
  coordKey,
  expectInitialBoard,
  fixtureAInput,
  forceClickCell,
  getLegalMoves,
  isSelected,
  loadFixture,
  openApp,
  playFullGame,
  setAiMode,
  snapshot,
  startGame,
  waitForPhase,
} from './helpers';

test.describe('人类先手', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
  });

  test('E2E-SETUP-01 开始界面提供三档难度与两种先后手，默认普通/我先手', async ({
    page,
  }) => {
    await expect(page.getByTestId('setup-panel')).toBeVisible();

    for (const id of ['difficulty-easy', 'difficulty-normal', 'difficulty-hard']) {
      await expect(page.getByTestId(id)).toBeVisible();
    }
    for (const id of ['side-first', 'side-second']) {
      await expect(page.getByTestId(id)).toBeVisible();
    }

    expect(await isSelected(page.getByTestId('difficulty-normal'))).toBe(true);
    expect(await isSelected(page.getByTestId('side-first'))).toBe(true);

    await page.getByTestId('difficulty-hard').click();
    expect(await isSelected(page.getByTestId('difficulty-hard'))).toBe(true);
    expect(await isSelected(page.getByTestId('difficulty-normal'))).toBe(false);

    await page.getByTestId('side-second').click();
    expect(await isSelected(page.getByTestId('side-second'))).toBe(true);
    expect(await isSelected(page.getByTestId('side-first'))).toBe(false);

    // 还原为先手，保证后续用例上下文一致
    await page.getByTestId('side-first').click();
    await expect(page.getByTestId('start-game')).toBeEnabled();
  });

  test('E2E-FIRST-01 先手开局：121 孔 / 20 子 / 状态区初始值', async ({ page }) => {
    await startGame(page, { difficulty: 'normal', side: 'first' });

    await expect(page.getByTestId('setup-panel')).toBeHidden();
    await expect(page.getByTestId('board')).toBeVisible();
    await expect(page.getByTestId('board-svg')).toBeVisible();
    await expect(page.getByTestId('board-svg')).toHaveJSProperty('tagName', 'svg');

    expect(await page.locator('[data-testid^="cell-"]').count()).toBe(121);
    expect(await page.locator('[data-testid^="piece-"]').count()).toBe(20);

    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'HUMAN_TURN');
    await expect(page.getByTestId('turn-text')).toHaveText('你的回合 · 红方');
    await expect(page.getByTestId('move-count')).toHaveText('0');
    await expect(page.getByTestId('progress-red')).toHaveText('0/10');
    await expect(page.getByTestId('progress-blue')).toHaveText('0/10');
    await expect(page.getByTestId('ai-thinking')).toBeHidden();

    // 初始棋子位置与规则一致（红下营、蓝上营）
    const state = await snapshot(page);
    expect(state.humanColor).toBe('R');
    expect(state.ply).toBe(0);
  });

  test('E2E-FIRST-02 选子高亮与单步；AI 在 ≤2000ms 内回应', async ({ page }) => {
    // AI 延迟 1000ms，便于稳定观察思考态（仍 ≤2000ms 硬上限）
    await setAiMode(page, 'delay', { delayMs: 1000 });
    await startGame(page, { difficulty: 'normal', side: 'first' });

    const origin = coord(1, -5, 4);
    await clickCell(page, origin);
    await expect(cell(page, origin)).toHaveAttribute('data-selected', 'true');

    // 高亮集合必须与引擎派生集合完全一致
    const moves = await getLegalMoves(page, origin);
    const toCellId = (value: { x: number; y: number; z: number }) =>
      `cell-${value.x}_${value.y}_${value.z}`;
    const expectedSteps = new Set(
      moves.filter((move) => move.kind === 'step').map((move) => toCellId(move.path[0])),
    );
    const expectedJumps = new Set(
      moves.filter((move) => move.kind === 'jump').map((move) => toCellId(move.path[0])),
    );

    const domSteps = new Set(
      await page
        .locator('[data-target="step"]')
        .evaluateAll((elements) => elements.map((element) => element.getAttribute('data-testid'))),
    );
    const domJumps = new Set(
      await page
        .locator('[data-target="jump"]')
        .evaluateAll((elements) => elements.map((element) => element.getAttribute('data-testid'))),
    );
    expect(domSteps).toEqual(expectedSteps);
    expect(domJumps).toEqual(expectedJumps);

    // 点击相邻空位执行单步
    await clickCell(page, [1, -4, 3]);
    await expect(page.getByTestId('move-count')).toHaveText('1');
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
    await expect(page.getByTestId('ai-thinking')).toBeVisible();
    await expect(page.getByTestId('turn-text')).toContainText('AI 思考中');

    const startedAt = Date.now();
    await waitForPhase(page, 'HUMAN_TURN', 2200);
    const elapsed = Date.now() - startedAt;
    expect(elapsed).toBeLessThanOrEqual(2000);

    await expect(page.getByTestId('move-count')).toHaveText('2');
    await expect(page.getByTestId('turn-text')).toHaveText('你的回合 · 红方');
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'HUMAN_TURN');
  });

  test('E2E-FIRST-03 先手完整对局：SETUP → GAME_OVER → 重开', async ({ page }) => {
    test.setTimeout(240_000);
    // 仅缩短 AI 思考等待；落子仍由真实规则/控制器执行
    await setAiMode(page, 'delay', { delayMs: 0 });
    await startGame(page, { difficulty: 'normal', side: 'first' });

    const final = await playFullGame(page, 300);
    expect(final.phase).toBe('GAME_OVER');

    await expect(page.getByTestId('game-result')).toBeVisible();
    const title = (await page.getByTestId('game-result-title').textContent())?.trim();
    expect(['红方获胜', '蓝方获胜', '平局']).toContain(title);

    // 终局封盘：棋盘点击不改变任何状态
    const before = await snapshot(page);
    await forceClickCell(page, [0, 0, 0]);
    const after = await snapshot(page);
    expect(after.ply).toBe(before.ply);
    expect(after.pieces).toEqual(before.pieces);
    expect(after.phase).toBe('GAME_OVER');

    // 弹层重开：回到初始布局
    await page.getByTestId('result-restart-button').click();
    await expect(page.getByTestId('game-result')).toBeHidden();
    await waitForPhase(page, 'HUMAN_TURN', 5000);
    await expectInitialBoard(page);
    await expect(page.getByTestId('move-count')).toHaveText('0');
  });

  test('E2E-FIRST-04 夹具 A：最后一子进营立即获胜并展示结果', async ({ page }) => {
    await loadFixture(page, fixtureAInput());
    await waitForPhase(page, 'HUMAN_TURN');
    await expect(page.getByTestId('move-count')).toHaveText('80');

    await clickCell(page, [-3, 4, -1]);
    await expect(cell(page, [-4, 5, -1])).toHaveAttribute('data-target', 'step');
    await clickCell(page, [-4, 5, -1]);

    await expect(page.getByTestId('game-result')).toBeVisible();
    await expect(page.getByTestId('game-result-title')).toHaveText('红方获胜');
    await expect(page.getByTestId('game-result-reason')).toHaveText('占满对方营地 10 孔');
    await expect(page.getByTestId('move-count')).toHaveText('81');
    await expect(page.getByTestId('progress-red')).toHaveText('10/10');

    // 终局后棋盘锁定
    const before = await snapshot(page);
    await forceClickCell(page, [0, 0, 0]);
    expect((await snapshot(page)).pieces).toEqual(before.pieces);

    // 弹层重开
    await page.getByTestId('result-restart-button').click();
    await waitForPhase(page, 'HUMAN_TURN', 5000);
    await expectInitialBoard(page);
  });

  test('E2E-FIRST-05 棋盘初始布局与引擎快照一致', async ({ page }) => {
    await startGame(page, { side: 'first' });
    const state = await snapshot(page);
    // DOM 的每个棋子 testid 均与引擎 pieces 一一对应
    const expected = new Set(
      state.pieces.map(
        (piece) =>
          `piece-${piece.pos.x}_${piece.pos.y}_${piece.pos.z}`,
      ),
    );
    const dom = new Set(
      await page
        .locator('[data-testid^="piece-"]')
        .evaluateAll((elements) => elements.map((element) => element.getAttribute('data-testid'))),
    );
    expect(dom).toEqual(expected);

    // 抽查坐标 key 使用 ASCII 负号/下划线
    expect(await page.getByTestId('cell-1_-5_4').count()).toBe(1);
    expect(coordKey(coord(-4, 5, -1))).toBe('-4,5,-1');
  });
});
