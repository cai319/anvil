/**
 * E2E 共享辅助：定位、测试钩子、夹具、完整对局驱动。
 *
 * 契约来源：
 * - window.__CC_TEST__ 与测试快照类型：docs/interface-contract.md §8
 * - data-testid 与 data-* 属性：docs/design-spec.md §10
 *
 * 类型直接引用应用公开类型（src/types/index.ts），避免与实现漂移；
 * 运行时 E2E 仍按黑盒方式只通过 DOM 与 window.__CC_TEST__ 交互。
 */
import { expect, type Locator, type Page } from '@playwright/test';
import type {
  AiTestMode,
  CoordTuple,
  CubeCoord,
  Difficulty,
  HumanSide,
  LegalMove,
  Result,
  SetupConfig,
  TestSnapshot,
  TestStateInput,
} from '../../src/types/index';

export type Coord = CoordTuple;

export function coord(x: number, y: number, z: number): CubeCoord {
  return { x, y, z };
}

export function coordKey(value: CubeCoord): string {
  return `${value.x},${value.y},${value.z}`;
}

export function tupleKey(value: Coord): string {
  return `${value[0]},${value[1]},${value[2]}`;
}

export const UPPER_CAMP: readonly Coord[] = [
  [-4, 8, -4],
  [-4, 7, -3], [-3, 7, -4],
  [-4, 6, -2], [-3, 6, -3], [-2, 6, -4],
  [-4, 5, -1], [-3, 5, -2], [-2, 5, -3], [-1, 5, -4],
];

export const LOWER_CAMP: readonly Coord[] = [
  [1, -5, 4], [2, -5, 3], [3, -5, 2], [4, -5, 1],
  [2, -6, 4], [3, -6, 3], [4, -6, 2],
  [3, -7, 4], [4, -7, 3],
  [4, -8, 4],
];

export const DEFAULT_CONFIG: SetupConfig = {
  difficulty: 'normal',
  humanSide: 'first',
};

/** 按元组值（而非引用）过滤，避免数组字面量 includes 失效。 */
function without(items: readonly Coord[], remove: readonly Coord[]): Coord[] {
  const removed = new Set(remove.map(tupleKey));
  return items.filter((item) => !removed.has(tupleKey(item)));
}

/** 夹具 A：红方一步获胜（acceptance-criteria.md 附录 B.1）。 */
export function fixtureAInput(config: SetupConfig = DEFAULT_CONFIG): TestStateInput {
  return {
    turn: 'R',
    ply: 80,
    config,
    phase: 'HUMAN_TURN',
    pieces: [
      ...without(LOWER_CAMP, [[4, -8, 4] as Coord]).map((pos) => ({
        color: 'B' as const,
        pos,
      })),
      { color: 'B' as const, pos: [0, -4, 4] as Coord },
      ...without(UPPER_CAMP, [[-4, 5, -1] as Coord]).map((pos) => ({
        color: 'R' as const,
        pos,
      })),
      { color: 'R' as const, pos: [-3, 4, -1] as Coord },
    ],
  };
}

/** 夹具 C：连跳与「结束回合」（acceptance-criteria.md 附录 B.3）。 */
export function fixtureCInput(config: SetupConfig = DEFAULT_CONFIG): TestStateInput {
  return {
    turn: 'R',
    ply: 40,
    config,
    phase: 'HUMAN_TURN',
    pieces: [
      ...without(UPPER_CAMP, [
        [-4, 8, -4] as Coord,
        [-1, 5, -4] as Coord,
      ]).map((pos) => ({ color: 'B' as const, pos })),
      { color: 'B' as const, pos: [1, -1, 0] as Coord },
      { color: 'B' as const, pos: [2, -1, -1] as Coord },
      { color: 'R' as const, pos: [0, 0, 0] as Coord },
      ...without(LOWER_CAMP, [[4, -8, 4] as Coord]).map((pos) => ({
        color: 'R' as const,
        pos,
      })),
    ],
  };
}

// ---------------------------------------------------------------------------
// 页面与测试钩子
// ---------------------------------------------------------------------------

export async function openApp(page: Page, options: { e2e?: boolean } = {}): Promise<void> {
  const e2e = options.e2e ?? true;
  await page.goto(e2e ? '/?e2e=1' : '/');
  if (e2e) {
    await page.waitForFunction(() => typeof window.__CC_TEST__ !== 'undefined');
  }
}

export async function hookAvailable(page: Page): Promise<boolean> {
  return page.evaluate(() => typeof window.__CC_TEST__ !== 'undefined');
}

export async function snapshot(page: Page): Promise<TestSnapshot> {
  return page.evaluate(() => {
    const hook = window.__CC_TEST__;
    if (!hook) throw new Error('window.__CC_TEST__ is not available');
    return hook.getState();
  });
}

export async function getLegalMoves(page: Page, piece?: CubeCoord): Promise<LegalMove[]> {
  return page.evaluate((target) => {
    const hook = window.__CC_TEST__;
    if (!hook) throw new Error('window.__CC_TEST__ is not available');
    return [...hook.getLegalMoves(target ?? undefined)];
  }, piece ?? null);
}

export async function setAiMode(
  page: Page,
  mode: AiTestMode,
  options?: { delayMs?: number },
): Promise<void> {
  await page.evaluate(
    ({ mode: nextMode, options: nextOptions }) => {
      const hook = window.__CC_TEST__;
      if (!hook) throw new Error('window.__CC_TEST__ is not available');
      hook.setAiMode(nextMode, nextOptions);
    },
    { mode, options },
  );
}

export async function loadFixture(page: Page, input: TestStateInput): Promise<void> {
  const result: Result<void> = await page.evaluate((fixture) => {
    const hook = window.__CC_TEST__;
    if (!hook) throw new Error('window.__CC_TEST__ is not available');
    return hook.loadState(fixture);
  }, input);
  if (!result.ok) {
    throw new Error(`loadState failed: ${result.error.code}`);
  }
  await expect(page.getByTestId('board')).toBeVisible();
}

export async function waitForPhase(
  page: Page,
  phase: TestSnapshot['phase'],
  timeout = 5000,
): Promise<void> {
  await expect
    .poll(async () => (await snapshot(page)).phase, { timeout })
    .toBe(phase);
}

export async function waitForPly(page: Page, ply: number, timeout = 5000): Promise<void> {
  await expect.poll(async () => (await snapshot(page)).ply, { timeout }).toBe(ply);
}

// ---------------------------------------------------------------------------
// 定位与交互
// ---------------------------------------------------------------------------

export function cellTestId(target: Coord | CubeCoord): string {
  if (Array.isArray(target)) {
    return `cell-${target[0]}_${target[1]}_${target[2]}`;
  }
  const value = target as CubeCoord;
  return `cell-${value.x}_${value.y}_${value.z}`;
}

export function cell(page: Page, target: Coord | CubeCoord): Locator {
  return page.getByTestId(cellTestId(target));
}

export async function clickCell(page: Page, target: Coord | CubeCoord): Promise<void> {
  await cell(page, target).click();
}

export async function forceClickCell(page: Page, target: Coord | CubeCoord): Promise<void> {
  await cell(page, target).click({ force: true });
}

/** 读取单选框/单选卡的选中态（兼容 aria-checked / data-selected / 内部 input）。 */
export async function isSelected(locator: Locator): Promise<boolean> {
  if ((await locator.getAttribute('aria-checked')) === 'true') return true;
  if ((await locator.getAttribute('data-selected')) === 'true') return true;
  const input = locator.locator('input');
  if ((await input.count()) > 0) return input.first().isChecked();
  return false;
}

export async function startGame(
  page: Page,
  options: { difficulty?: Difficulty; side?: HumanSide } = {},
): Promise<void> {
  if (options.difficulty) {
    await page.getByTestId(`difficulty-${options.difficulty}`).click();
  }
  if (options.side) {
    await page.getByTestId(`side-${options.side}`).click();
  }
  await page.getByTestId('start-game').click();
  await expect(page.getByTestId('board')).toBeVisible();
}

export async function expectInitialBoard(page: Page): Promise<void> {
  const state = await snapshot(page);
  expect(state.ply).toBe(0);
  expect(state.pieces).toHaveLength(20);
  const reds = state.pieces
    .filter((piece) => piece.color === 'R')
    .map((piece) => coordKey(piece.pos));
  const blues = state.pieces
    .filter((piece) => piece.color === 'B')
    .map((piece) => coordKey(piece.pos));
  expect(new Set(reds)).toEqual(new Set(LOWER_CAMP.map(tupleKey)));
  expect(new Set(blues)).toEqual(new Set(UPPER_CAMP.map(tupleKey)));
}

// ---------------------------------------------------------------------------
// 完整对局驱动
// ---------------------------------------------------------------------------

/**
 * 通过真实 UI 点击完成一局：读取引擎合法着法 → 点击 origin → 逐跳点击 path
 * → 若仍处于连跳草稿则点击「结束回合」→ 等待人类落子生效。
 *
 * 说明：AI 侧建议先 setAiMode('delay', { delayMs: 0 }) 缩短等待；
 * 所有落子仍由真实规则引擎与控制器校验，不使用测试钩子直接改局面。
 */
export async function playFullGame(page: Page, maxPlies = 300): Promise<TestSnapshot> {
  for (let guard = 0; guard < maxPlies * 3; guard += 1) {
    const state = await snapshot(page);
    if (state.phase === 'GAME_OVER') return state;

    if (state.phase === 'AI_THINKING') {
      await expect
        .poll(async () => (await snapshot(page)).phase, { timeout: 6000 })
        .not.toBe('AI_THINKING');
      continue;
    }

    if (state.phase !== 'HUMAN_TURN') {
      throw new Error(`unexpected phase: ${state.phase}`);
    }

    const moves = await getLegalMoves(page);
    if (moves.length === 0) {
      await page.waitForTimeout(50);
      continue;
    }

    const move = moves[0];
    await clickCell(page, move.piece);
    for (const step of move.path) {
      await clickCell(page, step);
    }

    const endTurn = page.getByTestId('end-turn');
    if (await endTurn.isVisible().catch(() => false)) {
      await endTurn.click();
    }

    // 等待这一手生效（phase 或 ply 发生变化）
    const beforeKey = `${state.phase}:${state.ply}`;
    await expect
      .poll(
        async () => {
          const current = await snapshot(page);
          return `${current.phase}:${current.ply}`;
        },
        { timeout: 5000 },
      )
      .not.toBe(beforeKey);
  }

  throw new Error(`game did not finish within ${maxPlies * 3} iterations`);
}
