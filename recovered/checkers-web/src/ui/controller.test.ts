import { describe, expect, it } from 'vitest';

import type {
  AiClient,
  AiMoveRequest,
  AiMoveResponse,
  AppEvent,
  Clock,
  FixtureState,
  GameState,
  LegalMove,
  RulesApi,
  TestStateInput,
} from '../types/index.js';
import { getLegalMoves, queryMoveOptions } from '../engine/rules.js';
import {
  applyMove as coreApplyMove,
  passTurn as corePassTurn,
} from '../engine/rules.js';
import { createController } from './controller.js';

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

class FakeClock implements Clock {
  private current = 0;
  private nextId = 1;
  private timers: Array<{ id: number; at: number; cb: () => void }> = [];

  now(): number {
    return this.current;
  }

  setTimeout(cb: () => void, ms: number): number {
    const id = this.nextId;
    this.nextId += 1;
    this.timers.push({ id, at: this.current + Math.max(0, ms), cb });
    return id;
  }

  clearTimeout(handle: number): void {
    this.timers = this.timers.filter((timer) => timer.id !== handle);
  }

  advance(ms: number): void {
    this.current += ms;
    let progressed = true;
    while (progressed) {
      progressed = false;
      const due = this.timers
        .filter((timer) => timer.at <= this.current)
        .sort((a, b) => a.at - b.at || a.id - b.id);
      if (due.length === 0) break;
      const next = due[0];
      this.timers = this.timers.filter((timer) => timer.id !== next.id);
      next.cb();
      progressed = true;
    }
  }
}

class FakeAi implements AiClient {
  readonly requests: AiMoveRequest[] = [];
  private pending: Array<{
    request: AiMoveRequest;
    resolve: (response: AiMoveResponse) => void;
  }> = [];
  auto = false;

  chooseMove(request: AiMoveRequest): Promise<AiMoveResponse> {
    this.requests.push(request);
    if (this.auto) {
      const legal = getLegalMoves(request.state, { player: request.player });
      return Promise.resolve({
        move: legal[0] ?? null,
        bestSoFar: legal[0] ?? null,
        degraded: false,
      });
    }
    return new Promise<AiMoveResponse>((resolve) => {
      this.pending.push({ request, resolve });
    });
  }

  resolveNext(response: AiMoveResponse): void {
    const entry = this.pending.shift();
    entry?.resolve(response);
  }

  resolveAt(index: number, response: AiMoveResponse): void {
    const entry = this.pending[index];
    if (!entry) throw new Error(`no pending AI request at ${index}`);
    this.pending.splice(index, 1);
    entry.resolve(response);
  }

  dispose(): void {
    this.pending = [];
  }
}

async function flushMicrotasks(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function makeController(options: {
  clock?: Clock;
  ai?: FakeAi;
  rules?: RulesApi;
} = {}) {
  const clock = options.clock ?? new FakeClock();
  const ai = options.ai ?? new FakeAi();
  const controller = createController({
    ai,
    clock,
    aiMinDelayMs: 0,
    aiResponseDeadlineMs: 1900,
    aiWatchdogMs: 1950,
    ...(options.rules ? { rules: options.rules } : {}),
  });
  return { controller, clock: clock as FakeClock, ai };
}

function collectEvents(controller: ReturnType<typeof createController>): AppEvent[] {
  const events: AppEvent[] = [];
  controller.subscribe((_snap, event) => events.push(event));
  return events;
}

function firstMoveFor(state: GameState, player: 'R' | 'B'): LegalMove {
  const moves = getLegalMoves(state, { player });
  if (moves.length === 0) throw new Error('no legal moves');
  return moves[0];
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FIXTURE_A: FixtureState = {
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

const FIXTURE_C: FixtureState = {
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
};

function fixtureInput(
  fixture: FixtureState,
  overrides: Partial<TestStateInput> = {},
): TestStateInput {
  return {
    pieces: fixture.pieces,
    turn: fixture.turn,
    ply: fixture.ply,
    config: { difficulty: 'normal', humanSide: 'first' },
    phase: 'HUMAN_TURN',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('控制器基础', () => {
  it('SETUP 快照与默认配置', () => {
    const { controller } = makeController();
    const snap = controller.getSnapshot();
    expect(snap.phase).toBe('SETUP');
    expect(snap.gameId).toBe(0);
    expect(snap.board).toBeNull();
    expect(snap.ai).toBeNull();
    expect(snap.canRestart).toBe(false);
    expect(snap.config).toEqual({ difficulty: 'normal', humanSide: 'first' });
    controller.destroy();
  });

  it('人类先手：startGame 事件顺序与初始局面', () => {
    const { controller } = makeController();
    const events = collectEvents(controller);
    const started = controller.startGame();
    expect(started.ok).toBe(true);

    const snap = controller.getSnapshot();
    expect(snap.phase).toBe('HUMAN_TURN');
    expect(snap.humanColor).toBe('R');
    expect(snap.aiColor).toBe('B');
    expect(snap.ply).toBe(0);
    expect(snap.board?.pieces).toHaveLength(20);
    expect(controller.__test.getLegalMoves()).toHaveLength(14);
    expect(events.map((e) => e.type)).toEqual(['match-start', 'turn-changed']);
    controller.destroy();
  });

  it('人类后手：AI_THINKING 与事件顺序', () => {
    const { controller, clock } = makeController();
    const events = collectEvents(controller);
    const started = controller.startGame({ humanSide: 'second' });
    expect(started.ok).toBe(true);
    expect(controller.getSnapshot().phase).toBe('AI_THINKING');
    expect(controller.getSnapshot().humanColor).toBe('B');
    expect(events.map((e) => e.type)).toEqual([
      'match-start',
      'turn-changed',
      'ai-thinking',
    ]);
    clock.advance(0);
    controller.destroy();
  });

  it('setSetupConfig 仅 SETUP 可用且校验非法值', () => {
    const { controller } = makeController();
    expect(controller.setSetupConfig({ difficulty: 'hard' }).ok).toBe(true);
    expect(controller.getSnapshot().config.difficulty).toBe('hard');
    expect(
      controller.setSetupConfig({ difficulty: 'nope' as unknown as 'easy' }).ok,
    ).toBe(false);

    controller.startGame();
    const late = controller.setSetupConfig({ difficulty: 'easy' });
    expect(late.ok ? null : late.error.code).toBe('WRONG_PHASE');
    controller.destroy();
  });
});

describe('人类走子与 AI 调度', () => {
  it('单步 → AI_THINKING → AI 落子 → HUMAN_TURN', async () => {
    const { controller, clock, ai } = makeController();
    const events = collectEvents(controller);
    controller.startGame();

    const before = controller.getSnapshot();
    const origin = before.board!.pieces.find((p) => p.color === 'R')!.pos;
    const options = queryMoveOptions(
      controller.__test.getEngineState()!,
      origin,
    );
    expect(options.ok).toBe(true);
    if (!options.ok) return;
    const target = options.value.steps[0];
    if (!target) return;

    expect(controller.handleCellClick(origin).ok).toBe(true);
    const moved = controller.handleCellClick(target);
    expect(moved.ok).toBe(true);
    expect(controller.getSnapshot().phase).toBe('AI_THINKING');
    expect(controller.getSnapshot().ply).toBe(1);

    clock.advance(0); // 启动 AI
    expect(ai.requests).toHaveLength(1);
    const request = ai.requests[0];
    const reply = firstMoveFor(request.state, 'B');
    ai.resolveNext({ move: reply, bestSoFar: reply, degraded: false });
    await flushMicrotasks();

    const snap = controller.getSnapshot();
    expect(snap.phase).toBe('HUMAN_TURN');
    expect(snap.ply).toBe(2);
    expect(snap.turn).toBe('R');
    expect(events.some((e) => e.type === 'move-applied' && e.source === 'ai')).toBe(true);
    controller.destroy();
  });

  it('AI 首手（人类后手）在 AI 落子后回到人类回合', async () => {
    const { controller, clock, ai } = makeController();
    controller.startGame({ humanSide: 'second' });
    clock.advance(0);
    expect(ai.requests).toHaveLength(1);
    const request = ai.requests[0];
    const reply = firstMoveFor(request.state, 'R');
    ai.resolveNext({ move: reply, bestSoFar: reply, degraded: false });
    await flushMicrotasks();

    const snap = controller.getSnapshot();
    expect(snap.phase).toBe('HUMAN_TURN');
    expect(snap.humanColor).toBe('B');
    expect(snap.turn).toBe('B');
    expect(snap.ply).toBe(1);
    controller.destroy();
  });

  it('AI 超时：看门狗兜底为合法着法并计降级', () => {
    const { controller, clock } = makeController();
    controller.startGame();
    const state = controller.getSnapshot();
    const origin = state.board!.pieces.find((p) => p.color === 'R')!.pos;
    const options = queryMoveOptions(controller.__test.getEngineState()!, origin);
    if (!options.ok || !options.value.steps[0]) return;
    controller.handleCellClick(origin);
    controller.handleCellClick(options.value.steps[0]);

    clock.advance(0); // AI 启动，但 FakeAi 不返回
    expect(controller.getSnapshot().phase).toBe('AI_THINKING');
    clock.advance(1950); // 看门狗
    const snap = controller.getSnapshot();
    expect(snap.phase).toBe('HUMAN_TURN');
    expect(snap.ply).toBe(2);
    expect(snap.ai?.degradationStreak).toBe(1);
    controller.destroy();
  });

  it('AI 非法输出被拒绝并兜底', async () => {
    const { controller, clock, ai } = makeController();
    controller.startGame();
    const state = controller.getSnapshot();
    const origin = state.board!.pieces.find((p) => p.color === 'R')!.pos;
    const options = queryMoveOptions(controller.__test.getEngineState()!, origin);
    if (!options.ok || !options.value.steps[0]) return;
    controller.handleCellClick(origin);
    controller.handleCellClick(options.value.steps[0]);
    clock.advance(0);

    ai.resolveNext({
      move: {
        piece: { x: 999, y: 0, z: -999 },
        path: [{ x: 999, y: 0, z: -999 }],
        kind: 'step',
      },
      degraded: false,
    });
    await flushMicrotasks();

    const snap = controller.getSnapshot();
    expect(snap.phase).toBe('HUMAN_TURN');
    expect(snap.ply).toBe(2);
    expect(snap.ai?.degradationStreak).toBe(1);
    controller.destroy();
  });

  it('重新开始使旧 AI 结果失效（gameId 代际）', async () => {
    const { controller, clock, ai } = makeController();
    controller.startGame({ humanSide: 'second' });
    clock.advance(0);
    expect(ai.requests).toHaveLength(1);
    const firstRequest = ai.requests[0];

    const restarted = controller.restart();
    expect(restarted.ok).toBe(true);
    const newGameId = controller.getSnapshot().gameId;
    expect(newGameId).toBe(2);

    clock.advance(0);
    expect(ai.requests).toHaveLength(2);
    const secondRequest = ai.requests[1];

    // 旧请求的合法着法不得落入新局。
    ai.resolveAt(0, {
      move: firstMoveFor(firstRequest.state, 'R'),
      degraded: false,
    });
    await flushMicrotasks();
    expect(controller.getSnapshot().gameId).toBe(newGameId);
    expect(controller.getSnapshot().ply).toBe(0);

    ai.resolveAt(0, {
      move: firstMoveFor(secondRequest.state, 'R'),
      degraded: false,
    });
    await flushMicrotasks();
    expect(controller.getSnapshot().ply).toBe(1);
    controller.destroy();
  });

  it('waitForIdle 在 AI 落子后 resolve', async () => {
    const { controller, clock, ai } = makeController();
    controller.startGame({ humanSide: 'second' });
    clock.advance(0);
    const idle = controller.waitForIdle();
    let resolved = false;
    void idle.then(() => {
      resolved = true;
    });
    await flushMicrotasks();
    expect(resolved).toBe(false);

    const request = ai.requests[0];
    ai.resolveNext({
      move: firstMoveFor(request.state, 'R'),
      degraded: false,
    });
    await flushMicrotasks();
    await idle;
    expect(resolved).toBe(true);
    controller.destroy();
  });
});

describe('轮空、连跳与终局', () => {
  it('无合法着法自动轮空', () => {
    const realRules: RulesApi = {
      getLegalMoves,
      queryMoveOptions,
      applyMove: coreApplyMove,
      passTurn: corePassTurn,
    };
    const wrappingRules: RulesApi = {
      ...realRules,
      getLegalMoves(state, filter) {
        if (state.turn === 'B') return [];
        return realRules.getLegalMoves(state, filter);
      },
    };
    const { controller } = makeController({ rules: wrappingRules });
    const events = collectEvents(controller);
    controller.startGame();
    const snap = controller.getSnapshot();
    const origin = snap.board!.pieces.find((p) => p.color === 'R')!.pos;
    const options = queryMoveOptions(controller.__test.getEngineState()!, origin);
    if (!options.ok || !options.value.steps[0]) return;
    controller.handleCellClick(origin);
    controller.handleCellClick(options.value.steps[0]);

    const after = controller.getSnapshot();
    expect(after.phase).toBe('HUMAN_TURN');
    expect(after.ply).toBe(2);
    expect(events.some((e) => e.type === 'pass' && e.player === 'B')).toBe(true);
    controller.destroy();
  });

  it('连跳草稿：jump-step → selection-changed，endTurn 提交完整路径', () => {
    const { controller } = makeController();
    const loaded = controller.__test.loadState(fixtureInput(FIXTURE_C));
    expect(loaded.ok).toBe(true);
    const events = collectEvents(controller);

    expect(controller.handleCellClick({ x: 0, y: 0, z: 0 }).ok).toBe(true);
    expect(controller.handleCellClick({ x: 2, y: -2, z: 0 }).ok).toBe(true);

    const snap = controller.getSnapshot();
    expect(snap.board?.selection?.mode).toBe('jumping');
    expect(snap.board?.selection?.canEndTurn).toBe(true);
    expect(snap.board?.selection?.path).toHaveLength(1);
    expect(events.map((e) => e.type)).toContain('jump-step');
    expect(events.map((e) => e.type)).toContain('selection-changed');

    expect(controller.endTurn().ok).toBe(true);
    const after = controller.getSnapshot();
    expect(after.ply).toBe(41);
    expect(after.phase).toBe('AI_THINKING');
    expect(after.board?.selection).toBeNull();
    controller.destroy();
  });

  it('夹具 A：点击最后一步立即获胜', () => {
    const { controller } = makeController();
    const loaded = controller.__test.loadState(fixtureInput(FIXTURE_A));
    expect(loaded.ok).toBe(true);

    expect(controller.handleCellClick({ x: -3, y: 4, z: -1 }).ok).toBe(true);
    expect(controller.handleCellClick({ x: -4, y: 5, z: -1 }).ok).toBe(true);

    const snap = controller.getSnapshot();
    expect(snap.phase).toBe('GAME_OVER');
    expect(snap.result).toEqual({ winner: 'R', reason: 'goal' });
    expect(snap.canRestart).toBe(true);
    expect(controller.handleCellClick({ x: 0, y: 0, z: 0 }).ok).toBe(false);
    controller.destroy();
  });

  it('非法输入返回对应错误码且状态不变', () => {
    const { controller } = makeController();
    controller.startGame();
    const before = JSON.stringify(controller.getSnapshot());

    const empty = controller.handleCellClick({ x: 0, y: 0, z: 0 });
    expect(empty.ok ? null : empty.error.code).toBe('EMPTY_CELL');

    const own = controller.getSnapshot().board!.pieces.find((p) => p.color === 'R')!.pos;
    expect(controller.handleCellClick(own).ok).toBe(true);

    const target = controller.handleCellClick({ x: 0, y: 0, z: 0 });
    expect(target.ok ? null : target.error.code).toBe('ILLEGAL_MOVE');
    expect(controller.getSnapshot().board?.selection?.mode).toBe('selected');

    expect(controller.handleCellClick(own).ok).toBe(true); // 取消选中
    expect(controller.getSnapshot().board?.selection).toBeNull();

    const notYours = controller.getSnapshot().board!.pieces.find((p) => p.color === 'B')!.pos;
    const wrong = controller.handleCellClick(notYours);
    expect(wrong.ok ? null : wrong.error.code).toBe('NOT_YOUR_PIECE');
    expect(JSON.stringify(controller.getSnapshot())).toBe(before);
    controller.destroy();
  });
});
