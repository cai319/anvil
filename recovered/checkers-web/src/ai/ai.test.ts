import { describe, expect, it } from 'vitest';

import type {
  AiClient,
  AiMoveRequest,
  AiStrategy,
  Clock,
  Difficulty,
  FixtureState,
  GameState,
  LegalMove,
  Player,
} from '../types/index.js';
import { createInitialState, createStateFromFixture, validateState } from '../engine/game.js';
import { applyMove, getLegalMoves, isLegalMove, passTurn } from '../engine/rules.js';
import { deriveSeed } from '../engine/random.js';
import { createAiClient } from './index.js';

// ---------------------------------------------------------------------------
// 与验收文档附录 B 一致的夹具（本文件独立复制，避免测试间耦合）
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

const FIXTURE_B: FixtureState = {
  turn: 'R',
  ply: 60,
  pieces: [
    { color: 'B', pos: [3, -4, 1] },
    { color: 'B', pos: [1, -5, 4] },
    { color: 'B', pos: [2, -5, 3] },
    { color: 'B', pos: [3, -5, 2] },
    { color: 'B', pos: [2, -6, 4] },
    { color: 'B', pos: [3, -6, 3] },
    { color: 'B', pos: [4, -6, 2] },
    { color: 'B', pos: [3, -7, 4] },
    { color: 'B', pos: [4, -7, 3] },
    { color: 'B', pos: [4, -8, 4] },
    { color: 'R', pos: [-4, 0, 4] },
    { color: 'R', pos: [-3, 0, 3] },
    { color: 'R', pos: [-2, 0, 2] },
    { color: 'R', pos: [-1, 0, 1] },
    { color: 'R', pos: [0, 0, 0] },
    { color: 'R', pos: [1, 0, -1] },
    { color: 'R', pos: [2, 0, -2] },
    { color: 'R', pos: [3, 0, -3] },
    { color: 'R', pos: [4, 0, -4] },
    { color: 'R', pos: [4, -4, 0] },
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

function stateOf(fixture: FixtureState): GameState {
  const result = createStateFromFixture(fixture, { gameId: 42 });
  if (!result.ok) throw new Error(`fixture: ${result.error.code}`);
  return result.value;
}

function makeRequest(
  state: GameState,
  difficulty: Difficulty,
  player: Player = state.turn,
  seed = deriveSeed(state.gameId, state.ply, player),
  timeBudgetMs = 1500,
): AiMoveRequest {
  return {
    gameId: state.gameId,
    state,
    player,
    configuredDifficulty: difficulty,
    effectiveDifficulty: difficulty,
    deadlineAt: Date.now() + 2000,
    timeBudgetMs,
    seed,
    signal: new AbortController().signal,
  };
}

function keyOf(move: LegalMove | null): string {
  if (!move) return 'null';
  return `${move.piece.x},${move.piece.y},${move.piece.z}|${move.kind}|${move.path
    .map((p) => `${p.x},${p.y},${p.z}`)
    .join('>')}`;
}

const difficulties: Difficulty[] = ['easy', 'normal', 'hard'];

// ---------------------------------------------------------------------------

describe('AI 硬约束', () => {
  it('所有难度在存在一步获胜时必选获胜着法（夹具 A）', async () => {
    const state = stateOf(FIXTURE_A);
    for (const difficulty of difficulties) {
      const client = createAiClient({ seed: 12345 });
      const response = await client.chooseMove(makeRequest(state, difficulty));
      client.dispose();

      expect(response.move).not.toBeNull();
      expect(isLegalMove(state, response.move as LegalMove)).toBe(true);
      expect([
        response.move!.path[response.move!.path.length - 1].x,
        response.move!.path[response.move!.path.length - 1].y,
        response.move!.path[response.move!.path.length - 1].z,
      ]).toEqual([-4, 5, -1]);
      expect(response.degraded).toBe(false);
    }
  });

  it('normal / hard 必须挡住对手一步获胜（夹具 B）', async () => {
    const state = stateOf(FIXTURE_B);
    for (const difficulty of ['normal', 'hard'] as Difficulty[]) {
      const client = createAiClient({ seed: 777 });
      const response = await client.chooseMove(makeRequest(state, difficulty));
      client.dispose();

      expect(response.move).not.toBeNull();
      expect(keyOf(response.move)).toBe('4,-4,0|step|4,-5,1');
    }
  });

  it('返回的着法始终属于合法着法集合', async () => {
    const state = stateOf(FIXTURE_C);
    for (const difficulty of difficulties) {
      const client = createAiClient({ seed: 2026 });
      const response = await client.chooseMove(makeRequest(state, difficulty));
      client.dispose();
      expect(response.move).not.toBeNull();
      expect(isLegalMove(state, response.move as LegalMove)).toBe(true);
    }
  });

  it('同局面 + 同种子 + 同难度可复现（AC-40）', async () => {
    const state = stateOf(FIXTURE_C);
    const request = makeRequest(state, 'normal', 'R', 0xabcdef);

    const a = await createAiClient({ seed: 0xabcdef }).chooseMove(request);
    const b = await createAiClient({ seed: 0xabcdef }).chooseMove(request);
    expect(keyOf(a.move)).toBe(keyOf(b.move));
    expect(isLegalMove(state, a.move as LegalMove)).toBe(true);
    expect(getLegalMoves(state).length).toBeGreaterThan(0);
  });

  it('chooseMove 同步返回 Promise，不阻塞调用方', () => {
    const state = stateOf(FIXTURE_C);
    const client = createAiClient({ seed: 1 });
    const started = performance.now();
    const promise = client.chooseMove(makeRequest(state, 'hard'));
    const elapsed = performance.now() - started;
    expect(promise).toBeInstanceOf(Promise);
    expect(elapsed).toBeLessThan(50);
    // 清理：立即释放，避免遗留搜索。
    client.dispose();
    void promise;
  });
});

describe('AI 对局可运行性', () => {
  it('AI vs AI 连续 40 手全部合法、状态可校验', async () => {
    let state = createInitialState({ gameId: 88 });
    const client = createAiClient();
    let plies = 0;

    while (state.phase === 'PLAYING' && plies < 40) {
      const legal = getLegalMoves(state);
      if (legal.length === 0) {
        const passed = passTurn(state, { legalMoves: [] });
        expect(passed.ok).toBe(true);
        if (!passed.ok) break;
        state = passed.value.state;
        plies += 1;
        continue;
      }

      const request = makeRequest(
        state,
        'easy',
        state.turn,
        deriveSeed(state.gameId, state.ply, state.turn),
        500,
      );
      const response = await client.chooseMove(request);
      expect(response.move).not.toBeNull();
      if (!response.move) break;
      expect(isLegalMove(state, response.move)).toBe(true);

      const applied = applyMove(state, response.move);
      expect(applied.ok).toBe(true);
      if (!applied.ok) break;
      state = applied.value.state;
      plies += 1;
      expect(state.ply).toBe(plies + 0);
      expect(validateState(state).ok).toBe(true);
      if (state.phase === 'FINISHED') break;
    }

    client.dispose();
    expect(plies).toBeGreaterThan(0);
  });

  it('hard 难度单步在 2 秒内返回合法着法', async () => {
    const state = createInitialState({ gameId: 99 });
    const client = createAiClient();
    const started = performance.now();
    const response = await client.chooseMove(
      makeRequest(state, 'hard', 'R', deriveSeed(state.gameId, state.ply, 'R'), 1500),
    );
    const elapsed = performance.now() - started;
    client.dispose();

    expect(response.move).not.toBeNull();
    expect(isLegalMove(state, response.move as LegalMove)).toBe(true);
    expect(elapsed).toBeLessThan(2000);
  });
});

describe('AI 降级与异步边界', () => {
  it('策略抛异常时转为 degraded 响应，不 reject', async () => {
    const state = stateOf(FIXTURE_C);
    const strategy: AiStrategy = {
      choose() {
        throw new Error('boom');
      },
    };
    const client = createAiClient({ strategy, seed: 5 });
    const response = await client.chooseMove(makeRequest(state, 'normal'));
    expect(response.degraded).toBe(true);
    expect(response.reason).toBe('exception');
    expect(response.move).not.toBeNull();
    expect(isLegalMove(state, response.move as LegalMove)).toBe(true);
    client.dispose();
  });

  it('策略超时后按硬时限兜底', async () => {
    const state = stateOf(FIXTURE_C);
    const never: AiStrategy = {
      choose() {
        return new Promise(() => {
          /* never resolves */
        });
      },
    };
    const client = createAiClient({ strategy: never, seed: 9 });
    const started = performance.now();
    const response = await client.chooseMove(makeRequest(state, 'normal', 'R', 1, 60));
    const elapsed = performance.now() - started;
    expect(response.degraded).toBe(true);
    expect(response.reason).toBe('timeout');
    expect(isLegalMove(state, response.move as LegalMove)).toBe(true);
    expect(elapsed).toBeLessThan(1000);
    client.dispose();
  });

  it('dispose 中止进行中的请求', async () => {
    const state = stateOf(FIXTURE_C);
    const never: AiStrategy = {
      choose() {
        return new Promise(() => {
          /* never resolves */
        });
      },
    };
    const client = createAiClient({ strategy: never, seed: 11 });
    const promise = client.chooseMove(makeRequest(state, 'normal'));
    client.dispose();
    const response = await promise;
    expect(response.degraded).toBe(true);
    expect(response.reason).toBe('aborted');
  });

  it('注入假时钟可确定性验证超时（不真实等待）', async () => {
    const state = stateOf(FIXTURE_C);
    const timers: Array<() => void> = [];
    let now = 0;
    const clock: Clock = {
      now: () => now,
      setTimeout(handler) {
        timers.push(() => {
          now += 50;
          handler();
        });
        return timers.length;
      },
      clearTimeout() {
        /* 队列由测试清空 */
      },
    };
    const never: AiStrategy = {
      choose() {
        return new Promise(() => {
          /* never resolves */
        });
      },
    };
    const client = createAiClient({ strategy: never, clock, seed: 3 });
    const promise = client.chooseMove(makeRequest(state, 'normal', 'R', 1, 40));

    // 驱动假时钟：0ms 启动策略，40ms 触发硬超时。
    while (timers.length > 0) {
      const next = timers.shift();
      next?.();
      if (now > 100) break;
    }
    const response = await promise;
    expect(response.reason).toBe('timeout');
    client.dispose();
  });
});
