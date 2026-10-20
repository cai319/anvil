import { expect, test } from '@playwright/test';

/**
 * S7 回归：落子动画不得阻塞状态更新（S6 风险问题 / S9 建议）。
 *
 * 用页面内 pointerdown + MutationObserver 计时，避免把 Playwright 操作开销
 * 混入 PRD P1 的「本地反馈 ≤100ms」断言。
 */
test.describe('落子动画与状态更新时序', () => {
  test('E2E-ANIM-01 人类落子后页面内 ≤100ms 完成快照 / move-count / 孔位更新', async ({
    page,
  }) => {
    await page.goto('/?e2e=1');
    await page.getByTestId('start-game').click();
    await page.getByTestId('cell-1_-5_4').click();

    await page.evaluate(() => {
      const target = document.querySelector('[data-testid="cell-1_-4_3"]');
      const moveCount = document.querySelector('[data-testid="move-count"]');
      const probe = {
        t0: 0,
        marks: [] as Array<{ what: string; t: number; value: string | null }>,
      };
      (window as unknown as { __animProbe: typeof probe }).__animProbe = probe;

      const markStart = (): void => {
        if (probe.t0 === 0) probe.t0 = performance.now();
      };
      target?.addEventListener('pointerdown', markStart, true);
      target?.addEventListener('mousedown', markStart, true);

      if (moveCount) {
        new MutationObserver(() => {
          probe.marks.push({
            what: 'move-count',
            t: performance.now(),
            value: moveCount.textContent,
          });
        }).observe(moveCount, { childList: true, characterData: true, subtree: true });
      }
      if (target) {
        new MutationObserver(() => {
          probe.marks.push({
            what: 'occupant',
            t: performance.now(),
            value: target.getAttribute('data-occupant'),
          });
        }).observe(target, { attributes: true, attributeFilter: ['data-occupant'] });
      }
    });

    await page.getByTestId('cell-1_-4_3').click();

    const result = await page.evaluate(() => {
      const probe = (
        window as unknown as {
          __animProbe: {
            t0: number;
            marks: Array<{ what: string; t: number; value: string | null }>;
          };
        }
      ).__animProbe;
      const snap = window.__CC_TEST__?.getState();
      return {
        t0: probe.t0,
        marks: probe.marks,
        ply: snap?.ply ?? -1,
        phase: snap?.phase ?? null,
      };
    });

    expect(result.t0).toBeGreaterThan(0);
    expect(result.ply).toBe(1);
    expect(result.phase).toBe('AI_THINKING');

    const occupant = result.marks.find((m) => m.what === 'occupant' && m.value === 'R');
    const count = result.marks.find((m) => m.what === 'move-count' && m.value === '1');
    expect(occupant).toBeTruthy();
    expect(count).toBeTruthy();
    // 页面内计时：从按下到 DOM 状态更新，远小于 100ms（动画 170ms 不参与状态更新）。
    expect(occupant!.t - result.t0).toBeGreaterThanOrEqual(0);
    expect(occupant!.t - result.t0).toBeLessThan(100);
    expect(count!.t - result.t0).toBeLessThan(100);
  });
});
