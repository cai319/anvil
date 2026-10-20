/**
 * E2E-HOOK-01 ~ 02
 * 覆盖 AC-46：测试钩子仅在 ?e2e=1 时启用，且不改变正常对局逻辑。
 */
import { expect, test } from '@playwright/test';
import { fixtureAInput, hookAvailable, openApp, snapshot } from './helpers';

test.describe('测试钩子隔离', () => {
  test('E2E-HOOK-01 正常入口（无 ?e2e=1）不存在 window.__CC_TEST__', async ({ page }) => {
    await openApp(page, { e2e: false });
    expect(await hookAvailable(page)).toBe(false);
    expect(
      await page.evaluate(() => Object.prototype.hasOwnProperty.call(window, '__CC_TEST__')),
    ).toBe(false);
  });

  test('E2E-HOOK-02 ?e2e=1 时钩子可用；非法夹具被拒绝且状态不变', async ({ page }) => {
    await openApp(page, { e2e: true });

    expect(
      await page.evaluate(() => window.__CC_TEST__?.version ?? null),
    ).toBe('1.0');

    const initial = await snapshot(page);
    expect(initial.phase).toBe('SETUP');

    const valid = fixtureAInput();
    const invalid = {
      ...valid,
      pieces: valid.pieces.slice(0, 19),
    };
    const result = await page.evaluate(
      (input) => {
        const hook = window.__CC_TEST__;
        if (!hook) throw new Error('test hook missing');
        return hook.loadState(input);
      },
      invalid,
    );
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe('INVALID_FIXTURE');
    }

    const after = await snapshot(page);
    expect(after.phase).toBe('SETUP');
    expect(after.pieces).toEqual([]);
  });
});
