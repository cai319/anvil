import { chromium } from '@playwright/test';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:4173';
const errors = [];
const log = (...args) => console.log(...args);

const FIXTURE_A = {
  turn: 'R',
  ply: 80,
  pieces: [
    { color: 'B', pos: [1, -5, 4] },
    { color: 'B', pos: [2, -5, 3] },
    { color: 'B', pos: [3, -5, 2] },
    { color: 'B', pos: [4, -5, 1] },
    { color: 'B', pos: [2, -6, 4] },
    { color: 'B', pos: [3, -6, 3] },
    { color: 'B', pos: [4, -6, 2] },
    { color: 'B', pos: [3, -7, 4] },
    { color: 'B', pos: [4, -7, 3] },
    { color: 'B', pos: [0, -4, 4] },
    { color: 'R', pos: [-4, 8, -4] },
    { color: 'R', pos: [-4, 7, -3] },
    { color: 'R', pos: [-3, 7, -4] },
    { color: 'R', pos: [-4, 6, -2] },
    { color: 'R', pos: [-3, 6, -3] },
    { color: 'R', pos: [-2, 6, -4] },
    { color: 'R', pos: [-3, 5, -2] },
    { color: 'R', pos: [-2, 5, -3] },
    { color: 'R', pos: [-1, 5, -4] },
    { color: 'R', pos: [-3, 4, -1] },
  ],
};

function assert(condition, message) {
  if (!condition) throw new Error(`ASSERT: ${message}`);
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
const page = await context.newPage();

page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`);
});
page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));

try {
  await page.goto(`${BASE}/?e2e=1`, { waitUntil: 'networkidle' });

  // 1. 首屏 SETUP
  await page.getByTestId('setup-panel').waitFor({ state: 'visible' });
  assert((await page.getByTestId('difficulty-normal').locator('input').isChecked()), '默认难度应为普通');
  assert((await page.getByTestId('side-first').locator('input').isChecked()), '默认应为我先手');
  assert((await page.getByTestId('board').isHidden()), 'SETUP 时棋盘应隐藏');
  log('PASS 1: SETUP 默认值与可见性');

  // 2. 人类先手开始
  await page.getByTestId('start-game').click();
  await page.getByTestId('board').waitFor({ state: 'visible' });
  assert((await page.getByTestId('cell--4_8_-4').count()) === 0 ? false : true, 'cell testid 应使用 ASCII');
  assert((await page.locator('[data-testid^="cell-"]').count()) === 121, '应有 121 个孔位');
  assert((await page.locator('[data-testid^="piece-"]').count()) === 20, '应有 20 枚棋子');
  assert((await page.getByTestId('turn-text').textContent()).includes('你的回合 · 红方'), '人类先手回合文案');
  assert((await page.getByTestId('move-count').textContent()).trim() === '0', '初始手数 0');
  log('PASS 2: 人类先手开局、121 孔/20 子');

  // 3. 选子 + 合法落点
  await page.getByTestId('cell-1_-5_4').click();
  await page.getByTestId('legal-step-1_-4_3').waitFor({ state: 'attached' });
  await page.getByTestId('legal-step-0_-4_4').waitFor({ state: 'attached' });
  assert((await page.getByTestId('selected-piece').count()) === 1, '应有选中标记');
  log('PASS 3: 选子与合法单步高亮');

  // 4. 单步 → AI 思考 → AI 落子 → 人类回合
  await page.getByTestId('legal-step-1_-4_3').click();
  await page.waitForFunction(
    () => document.querySelector('[data-testid="turn-text"]')?.textContent?.includes('AI 思考中'),
    null,
    { timeout: 1500 },
  );
  await page.waitForFunction(
    () => document.querySelector('[data-testid="turn-text"]')?.textContent?.includes('你的回合'),
    null,
    { timeout: 6000 },
  );
  const plyAfterRound = Number((await page.getByTestId('move-count').textContent()).trim());
  assert(plyAfterRound >= 2, `双方各走一步后手数应 >=2，实际 ${plyAfterRound}`);
  log(`PASS 4: 单步、AI 思考态与 AI 落子（ply=${plyAfterRound}）`);

  // 5. 重新开始
  const beforeRestart = Number(await page.getByTestId('board').getAttribute('data-game-id'));
  await page.getByTestId('restart-button').click();
  await page.waitForFunction(
    (previous) => Number(document.querySelector('[data-testid="board"]')?.getAttribute('data-game-id')) > previous,
    beforeRestart,
    { timeout: 3000 },
  );
  assert((await page.getByTestId('move-count').textContent()).trim() === '0', '重开后手数归零');
  log('PASS 5: 重新开始 gameId 递增、手数归零');

  // 6. 返回设置
  await page.getByTestId('back-to-setup').click();
  await page.getByTestId('setup-panel').waitFor({ state: 'visible' });
  assert(await page.getByTestId('board').isHidden(), '返回设置后棋盘隐藏');
  log('PASS 6: 返回设置');

  // 7. 人类后手：AI 自动首手
  await page.getByTestId('side-second').click();
  await page.getByTestId('start-game').click();
  await page.waitForFunction(
    () => document.querySelector('[data-testid="turn-text"]')?.textContent?.includes('AI 思考中'),
    null,
    { timeout: 1500 },
  );
  await page.waitForFunction(
    () => document.querySelector('[data-testid="turn-text"]')?.textContent?.includes('你的回合 · 蓝方'),
    null,
    { timeout: 6000 },
  );
  const aiPly = Number((await page.getByTestId('move-count').textContent()).trim());
  assert(aiPly >= 1, `AI 首手后手数应 >=1，实际 ${aiPly}`);
  log(`PASS 7: 我后手 AI 自动首手（ply=${aiPly}）`);

  // 8. 通过测试钩子加载夹具 A 并完成一步获胜
  await page.getByTestId('back-to-setup').click();
  await page.evaluate((fixture) => {
    const hook = window.__CC_TEST__;
    if (!hook) throw new Error('missing __CC_TEST__');
    const result = hook.loadState({
      pieces: fixture.pieces,
      turn: 'R',
      ply: 80,
      config: { difficulty: 'normal', humanSide: 'first' },
      phase: 'HUMAN_TURN',
    });
    if (!result.ok) throw new Error(`loadState failed: ${result.error.code}`);
  }, FIXTURE_A);
  await page.getByTestId('board').waitFor({ state: 'visible' });
  await page.getByTestId('cell--3_4_-1').click();
  await page.getByTestId('legal-step--4_5_-1').click();
  await page.getByTestId('game-result').waitFor({ state: 'visible' });
  assert(
    (await page.getByTestId('game-result-title').textContent()) === '红方获胜',
    '结算标题应为红方获胜',
  );
  await page.screenshot({ path: '/tmp/cc-s4-result.png' });
  log('PASS 8: 夹具 A 一步获胜、结果弹层');

  // 9. 弹层再来一局
  await page.getByTestId('result-restart-button').click();
  await page.getByTestId('game-result').waitFor({ state: 'hidden' });
  assert((await page.getByTestId('move-count').textContent()).trim() === '0', '再来一局后手数 0');
  log('PASS 9: 结果弹层再来一局');

  // 10. 生产模式不暴露测试钩子
  const plain = await context.newPage();
  await plain.goto(`${BASE}/`, { waitUntil: 'networkidle' });
  const hasHook = await plain.evaluate(() => typeof window.__CC_TEST__ !== 'undefined');
  assert(!hasHook, '无 ?e2e=1 时不得暴露 __CC_TEST__');
  await plain.close();
  log('PASS 10: 测试钩子仅 ?e2e=1 启用');

  // 11. 桌面截图（对局中）
  await page.screenshot({ path: '/tmp/cc-s4-desktop.png' });
  await page.setViewportSize({ width: 360, height: 640 });
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByTestId('start-game').click();
  await page.getByTestId('board').waitFor({ state: 'visible' });
  await page.screenshot({ path: '/tmp/cc-s4-mobile.png' });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  assert(!overflow, '360px 视口不得横向滚动');
  log('PASS 11: 360×640 无横向滚动');

  assert(errors.length === 0, `页面存在未捕获错误：${errors.join(' | ')}`);
  log('ALL SMOKE TESTS PASSED');
} catch (cause) {
  console.error('SMOKE FAILED:', cause);
  await page.screenshot({ path: '/tmp/cc-s4-failure.png' });
  process.exitCode = 1;
} finally {
  await browser.close();
}
