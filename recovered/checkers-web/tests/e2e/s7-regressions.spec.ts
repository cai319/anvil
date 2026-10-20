import { expect, test, type Page } from '@playwright/test';

/**
 * S7 回归：S9 走查 P2-01 / P2-03 / P2-04 / P2-05。
 */

const FIXTURE_C = {
  turn: 'R',
  ply: 40,
  pieces: [
    { color: 'B', pos: [-4, 7, -3] },
    { color: 'B', pos: [-3, 7, -4] },
    { color: 'B', pos: [-4, 6, -2] },
    { color: 'B', pos: [-3, 6, -3] },
    { color: 'B', pos: [-2, 6, -4] },
    { color: 'B', pos: [-4, 5, -1] },
    { color: 'B', pos: [-3, 5, -2] },
    { color: 'B', pos: [-2, 5, -3] },
    { color: 'B', pos: [1, -1, 0] },
    { color: 'B', pos: [2, -1, -1] },
    { color: 'R', pos: [0, 0, 0] },
    { color: 'R', pos: [1, -5, 4] },
    { color: 'R', pos: [2, -5, 3] },
    { color: 'R', pos: [3, -5, 2] },
    { color: 'R', pos: [4, -5, 1] },
    { color: 'R', pos: [2, -6, 4] },
    { color: 'R', pos: [3, -6, 3] },
    { color: 'R', pos: [4, -6, 2] },
    { color: 'R', pos: [3, -7, 4] },
    { color: 'R', pos: [4, -7, 3] },
  ],
} as const;

async function loadFixtureC(page: Page): Promise<void> {
  await page.goto('/?e2e=1');
  const result = await page.evaluate((fixture) => {
    const hook = window.__CC_TEST__;
    if (!hook) throw new Error('missing __CC_TEST__');
    const loaded = hook.loadState({
      pieces: fixture.pieces as unknown as Parameters<typeof hook.loadState>[0]['pieces'],
      turn: 'R',
      ply: 40,
      config: { difficulty: 'normal', humanSide: 'first' },
      phase: 'HUMAN_TURN',
    });
    return loaded.ok ? 'ok' : loaded.error.code;
  }, FIXTURE_C);
  expect(result).toBe('ok');
  await page.getByTestId('board').waitFor({ state: 'visible' });
}

async function enterJumping(page: Page): Promise<void> {
  await page.getByTestId('cell-0_0_0').click();
  await page.getByTestId('cell-2_-2_0').click();
  await expect(page.getByTestId('jump-bar')).toBeVisible();
}

test.describe('S7 布局回归', () => {
  test('E2E-S7-01 连跳条出现不改变棋盘尺寸（P2-01）', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await loadFixtureC(page);

    const before = await page.getByTestId('board').boundingBox();
    expect(before).not.toBeNull();

    await enterJumping(page);

    const after = await page.getByTestId('board').boundingBox();
    expect(after).not.toBeNull();
    expect(after!.width).toBeCloseTo(before!.width, 0);
    expect(after!.height).toBeCloseTo(before!.height, 0);
  });

  test('E2E-S7-02 1440×900 右栏卡片间距为 16px（P2-03）', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/?e2e=1');
    await page.getByTestId('start-game').click();

    const turn = await page.getByTestId('turn-indicator').boundingBox();
    const progress = await page.locator('.progress-card').boundingBox();
    expect(turn).not.toBeNull();
    expect(progress).not.toBeNull();

    const gap = progress!.y - (turn!.y + turn!.height);
    expect(gap).toBeGreaterThanOrEqual(15);
    expect(gap).toBeLessThanOrEqual(17);
  });

  test('E2E-S7-03 360×640 开始按钮位于首屏且 ≥44px（P2-04）', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 640 });
    await page.goto('/?e2e=1');

    const start = await page.getByTestId('start-game').boundingBox();
    expect(start).not.toBeNull();
    expect(start!.height).toBeGreaterThanOrEqual(44);
    expect(start!.y + start!.height).toBeLessThanOrEqual(640);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflow).toBe(false);
  });

  test('E2E-S7-04 360×640 结束回合按钮 ≥44px（P2-05）', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 640 });
    await loadFixtureC(page);
    await enterJumping(page);

    const endTurn = await page.getByTestId('end-turn').boundingBox();
    expect(endTurn).not.toBeNull();
    expect(endTurn!.height).toBeGreaterThanOrEqual(44);
  });

  test('E2E-S7-05 连续 3 次 AI 降级后提示位于状态卡内且不压页脚（P2-02）', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto('/?e2e=1');
    await page.getByTestId('start-game').click();
    await page.evaluate(() => window.__CC_TEST__!.setAiMode('throw'));

    for (let round = 0; round < 6; round += 1) {
      const state = await page.evaluate(() => window.__CC_TEST__!.getState());
      if (state.ai?.forcedEasy || state.phase === 'GAME_OVER') break;
      if (state.phase !== 'HUMAN_TURN') {
        await page.evaluate(() => window.__CC_TEST__!.waitForIdle());
        continue;
      }
      const moves = await page.evaluate(() => window.__CC_TEST__!.getLegalMoves());
      const simple = moves.find((move) => move.path.length === 1);
      expect(simple).toBeTruthy();
      if (!simple) break;
      const cellId = (c: { x: number; y: number; z: number }): string =>
        `cell-${c.x}_${c.y}_${c.z}`;
      await page.getByTestId(cellId(simple.piece)).click();
      await page.getByTestId(cellId(simple.path[0])).click();
      if (await page.getByTestId('jump-bar').isVisible().catch(() => false)) {
        await page.getByTestId('end-turn').click();
      }
      await page.evaluate(() => window.__CC_TEST__!.waitForIdle());
    }

    const result = await page.evaluate(() => {
      const state = window.__CC_TEST__!.getState();
      const notice = document.querySelector('[data-testid="ai-degraded-notice"]');
      const turn = document.querySelector('[data-testid="turn-indicator"]');
      const footer = document.querySelector('.cc-footer');
      const rect = (el: Element | null): { top: number; bottom: number } | null => {
        const r = el?.getBoundingClientRect();
        return r ? { top: r.top, bottom: r.bottom } : null;
      };
      return {
        forcedEasy: state.ai?.forcedEasy ?? false,
        streak: state.ai?.degradationStreak ?? 0,
        noticeVisible: notice ? !notice.hasAttribute('hidden') : false,
        noticeText: notice?.textContent ?? '',
        notice: rect(notice),
        turn: rect(turn),
        footer: rect(footer),
      };
    });

    expect(result.forcedEasy).toBe(true);
    expect(result.streak).toBeGreaterThanOrEqual(3);
    expect(result.noticeVisible).toBe(true);
    expect(result.noticeText).toContain('AI 已降级为简单难度');
    expect(result.notice).not.toBeNull();
    expect(result.turn).not.toBeNull();
    expect(result.footer).not.toBeNull();
    expect(result.notice!.top).toBeGreaterThanOrEqual(result.turn!.top);
    expect(result.notice!.bottom).toBeLessThanOrEqual(result.turn!.bottom);
    expect(result.notice!.bottom).toBeLessThan(result.footer!.top);
  });

  test('E2E-S7-06 后手开局点击开始后同步进入 AI_THINKING（AC-41 迁移）', async ({
    page,
  }) => {
    await page.goto('/?e2e=1');
    await page.evaluate(() => window.__CC_TEST__!.setAiMode('timeout'));
    await page.getByTestId('side-second').click();
    await page.getByTestId('start-game').click();

    const snap = await page.evaluate(() => {
      const state = window.__CC_TEST__!.getState();
      return { phase: state.phase, ply: state.ply, turn: state.turn };
    });
    expect(snap.phase).toBe('AI_THINKING');
    expect(snap.ply).toBe(0);
    expect(snap.turn).toBe('R');
  });
});
