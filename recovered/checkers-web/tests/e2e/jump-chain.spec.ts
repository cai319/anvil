/**
 * E2E-JUMP-01 ~ 03
 * 覆盖 AC-08/10/11/20：夹具 C 的连跳 UI、结束回合、自动结束与非法切换。
 */
import { expect, test } from '@playwright/test';
import {
  cell,
  clickCell,
  coordKey,
  fixtureCInput,
  loadFixture,
  openApp,
  setAiMode,
  snapshot,
  waitForPhase,
} from './helpers';

const ORIGIN = [0, 0, 0] as const;
const HOP1 = [2, -2, 0] as const;
const HOP2 = [2, 0, -2] as const;

test.describe('连跳 UI（夹具 C）', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
    // 延迟 AI，保证跳后/结束回合的中间态可稳定断言
    await setAiMode(page, 'delay', { delayMs: 1200 });
    await loadFixture(page, fixtureCInput());
    await waitForPhase(page, 'HUMAN_TURN');
  });

  test('E2E-JUMP-01 首跳后显示连跳条；点击「结束回合」停在首跳落点', async ({ page }) => {
    await expect(page.getByTestId('move-count')).toHaveText('40');

    await clickCell(page, ORIGIN);
    await expect(cell(page, HOP1)).toHaveAttribute('data-target', 'jump');
    await clickCell(page, HOP1);

    // UI 连跳态
    await expect(page.getByTestId('jump-bar')).toBeVisible();
    await expect(page.getByTestId('end-turn')).toBeVisible();
    await expect(page.getByTestId('jump-status')).toContainText('已跳 1 跳');
    await expect(page.getByTestId('move-count')).toHaveText('40');
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'HUMAN_TURN');

    const draft = await snapshot(page);
    expect(draft.selection?.mode).toBe('jumping');
    expect(draft.selection?.origin).toEqual({ x: 0, y: 0, z: 0 });
    expect(draft.selection?.current).toEqual({ x: 2, y: -2, z: 0 });
    expect(draft.selection?.path.map(coordKey)).toEqual([coordKey({ x: 2, y: -2, z: 0 })]);
    // 引擎状态草稿期间 origin 仍有子；视觉上 origin 为空、current 为红
    expect(
      draft.pieces.some(
        (piece) =>
          piece.color === 'R' && piece.pos.x === 0 && piece.pos.y === 0 && piece.pos.z === 0,
      ),
    ).toBe(true);
    await expect(cell(page, ORIGIN)).toHaveAttribute('data-occupant', 'none');
    await expect(cell(page, HOP1)).toHaveAttribute('data-occupant', 'R');

    // 手动结束回合
    await page.getByTestId('end-turn').click();
    await expect(page.getByTestId('move-count')).toHaveText('41');
    await expect(page.getByTestId('jump-bar')).toBeHidden();
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');

    const after = await snapshot(page);
    const redAtHop1 = after.pieces.some(
      (piece) =>
        piece.color === 'R' && piece.pos.x === 2 && piece.pos.y === -2 && piece.pos.z === 0,
    );
    expect(redAtHop1).toBe(true);
  });

  test('E2E-JUMP-02 续跳后无后续跳，自动结束回合', async ({ page }) => {
    await clickCell(page, ORIGIN);
    await clickCell(page, HOP1);
    await expect(page.getByTestId('end-turn')).toBeVisible();

    await clickCell(page, HOP2);
    await expect(page.getByTestId('move-count')).toHaveText('41');
    await expect(page.getByTestId('jump-bar')).toBeHidden();
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');

    const after = await snapshot(page);
    const redAtHop2 = after.pieces.some(
      (piece) =>
        piece.color === 'R' && piece.pos.x === 2 && piece.pos.y === 0 && piece.pos.z === -2,
    );
    expect(redAtHop2).toBe(true);
  });

  test('E2E-JUMP-03 连跳中切换棋子/点击非高亮孔位被拒绝', async ({ page }) => {
    await clickCell(page, ORIGIN);
    await clickCell(page, HOP1);

    // 点击另一枚己方棋子
    await clickCell(page, [1, -5, 4]);
    await expect(page.getByTestId('hint')).toHaveText('连跳中只能继续跳跃或结束回合');

    let state = await snapshot(page);
    expect(state.selection?.mode).toBe('jumping');
    expect(state.selection?.current).toEqual({ x: 2, y: -2, z: 0 });
    expect(state.ply).toBe(40);

    // 点击非高亮空位
    await clickCell(page, [3, -3, 0]);
    await expect(page.getByTestId('hint')).toHaveText('连跳中只能继续跳跃或结束回合');

    state = await snapshot(page);
    expect(state.selection?.current).toEqual({ x: 2, y: -2, z: 0 });
    expect(state.ply).toBe(40);
    await expect(page.getByTestId('end-turn')).toBeVisible();
  });
});
