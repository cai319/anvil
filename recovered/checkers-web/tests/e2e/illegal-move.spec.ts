/**
 * E2E-ILLEGAL-01 ~ 03
 * 覆盖 AC-12/19/23：非法点击不改变局面、显示非阻塞中文提示、提示自动消失。
 */
import { expect, test } from '@playwright/test';
import {
  cell,
  clickCell,
  openApp,
  snapshot,
  startGame,
} from './helpers';

test.describe('非法操作提示', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
    await startGame(page, { difficulty: 'normal', side: 'first' });
  });

  test('E2E-ILLEGAL-01 点击空格/对方棋子：提示且局面不变、无阻塞弹窗', async ({ page }) => {
    let dialogSeen = false;
    page.on('dialog', async (dialog) => {
      dialogSeen = true;
      await dialog.dismiss();
    });

    const before = await snapshot(page);

    // 空中心孔位
    await clickCell(page, [0, 0, 0]);
    await expect(page.getByTestId('hint')).toHaveText(/不可落子|请选择一枚棋子/);
    expect((await snapshot(page)).pieces).toEqual(before.pieces);

    // 提示约 2 秒内消失，不阻塞后续操作
    await expect(page.getByTestId('hint')).toHaveText('', { timeout: 2500 });

    // 对方（蓝方）棋子
    await clickCell(page, [-4, 8, -4]);
    await expect(page.getByTestId('hint')).toHaveText('请选择己方棋子');

    const after = await snapshot(page);
    expect(after.pieces).toEqual(before.pieces);
    expect(after.ply).toBe(before.ply);
    expect(after.turn).toBe(before.turn);
    expect(after.selection).toBeNull();
    expect(dialogSeen).toBe(false);
  });

  test('E2E-ILLEGAL-02 选中后点击非高亮孔位：提示「不可落子」，选中保持', async ({ page }) => {
    const origin = [1, -5, 4] as const;
    await clickCell(page, origin);
    await expect(cell(page, origin)).toHaveAttribute('data-selected', 'true');

    const before = await snapshot(page);
    await clickCell(page, [0, 0, 0]);

    await expect(page.getByTestId('hint')).toHaveText('不可落子');

    const after = await snapshot(page);
    expect(after.pieces).toEqual(before.pieces);
    expect(after.ply).toBe(before.ply);
    // 非高亮点击不取消选中，也不切换棋子
    expect(after.selection?.origin).toEqual({ x: 1, y: -5, z: 4 });
    await expect(cell(page, origin)).toHaveAttribute('data-selected', 'true');
  });

  test('E2E-ILLEGAL-03 非法点击不污染状态，随后合法走子仍成功', async ({ page }) => {
    const before = await snapshot(page);

    await clickCell(page, [0, 0, 0]);
    await clickCell(page, [-4, 8, -4]);
    await clickCell(page, [4, -8, 4]); // 己方棋子但不在可选集合（红方下营顶点）
    await page.waitForTimeout(100);

    // 非法点击后局面不变（手数/棋子/回合）
    const afterIllegal = await snapshot(page);
    expect(afterIllegal.ply).toBe(before.ply);
    expect(afterIllegal.pieces).toEqual(before.pieces);
    expect(afterIllegal.turn).toBe(before.turn);

    // 随后执行合法单步：状态正常推进
    await clickCell(page, [1, -5, 4]);
    await clickCell(page, [1, -4, 3]);
    await expect(page.getByTestId('move-count')).toHaveText('1');
    await expect(page.getByTestId('board')).toHaveAttribute('data-phase', 'AI_THINKING');
  });
});
