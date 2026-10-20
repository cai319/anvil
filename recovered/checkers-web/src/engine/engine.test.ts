import { describe, expect, it } from 'vitest';

import type { CoordTuple, CubeCoord, FixtureState, GameState, LegalMove } from '../types/index.js';
import {
  BOARD,
  coordKey,
  countInTargetCamp,
  isCell,
  toTuple,
} from './board.js';
import {
  applyMove,
  getLegalMoves,
  isLegalMove,
  passTurn,
  queryMoveOptions,
} from './rules.js';
import {
  createInitialState,
  createStateFromFixture,
  getProgress,
  validateState,
} from './game.js';

// ---------------------------------------------------------------------------
// 附录 B 标准夹具
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

function fixture(f: FixtureState, gameId = 1, ply?: number): GameState {
  const result = createStateFromFixture(f, { gameId, ...(ply !== undefined ? { ply } : {}) });
  if (!result.ok) throw new Error(`fixture failed: ${result.error.code}`);
  return result.value;
}

function moveKey(move: LegalMove): string {
  return `${coordKey(move.piece)}:${move.kind}:${move.path.map(coordKey).join('>')}`;
}

// ---------------------------------------------------------------------------
// 棋盘
// ---------------------------------------------------------------------------

describe('BOARD', () => {
  it('定义 121 孔、61 中心孔与 6 个 10 孔营地', () => {
    expect(BOARD.cells).toHaveLength(121);
    expect(BOARD.centerCells).toHaveLength(61);

    const campIds = ['C+', 'C-', 'C+x', 'C-x', 'C+z', 'C-z'] as const;
    for (const id of campIds) {
      expect(BOARD.camps[id]).toHaveLength(10);
    }

    const all = new Set(BOARD.cells.map(coordKey));
    expect(all.size).toBe(121);

    for (const id of campIds) {
      for (const cell of BOARD.camps[id]) {
        expect(isCell(cell)).toBe(true);
      }
    }

    // 营地两两不相交，且与中心区不相交。
    const seen = new Set<string>();
    for (const id of campIds) {
      for (const cell of BOARD.camps[id]) {
        const key = coordKey(cell);
        expect(seen.has(key)).toBe(false);
        seen.add(key);
      }
    }
    for (const cell of BOARD.centerCells) {
      expect(seen.has(coordKey(cell))).toBe(false);
    }
  });

  it('规范顺序：y 从 +8 到 −8，行内 x 递增', () => {
    expect(toTuple(BOARD.cells[0])).toEqual([-4, 8, -4]);
    expect(toTuple(BOARD.cells[BOARD.cells.length - 1])).toEqual([4, -8, 4]);
  });
});

// ---------------------------------------------------------------------------
// 初始局面 / 合法着法
// ---------------------------------------------------------------------------

describe('初始局面', () => {
  it('R 10 子占下营、B 10 子占上营，红先', () => {
    const state = createInitialState({ gameId: 1 });
    expect(state.turn).toBe('R');
    expect(state.ply).toBe(0);
    expect(state.pieces).toHaveLength(20);
    expect(state.pieces.filter((p) => p.color === 'R')).toHaveLength(10);
    expect(state.pieces.filter((p) => p.color === 'B')).toHaveLength(10);
    expect(state.pieces.slice(0, 10).every((p) => p.color === 'R')).toBe(true);

    expect(countInTargetCamp(state, 'R')).toBe(0);
    expect(countInTargetCamp(state, 'B')).toBe(0);
    expect(getProgress(state)).toEqual({ R: 0, B: 0 });
  });

  it('开局红方恰好 14 个合法着法（8 单步 + 6 跳跃）', () => {
    const state = createInitialState({ gameId: 7 });
    const moves = getLegalMoves(state);
    expect(moves.filter((m) => m.kind === 'step')).toHaveLength(8);
    expect(moves.filter((m) => m.kind === 'jump')).toHaveLength(6);
    expect(moves).toHaveLength(14);

    // 确定性：同一状态重复调用结果一致。
    expect(getLegalMoves(state).map(moveKey)).toEqual(moves.map(moveKey));
  });

  it('validateState 接受初始局面', () => {
    expect(validateState(createInitialState({ gameId: 3 })).ok).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 夹具 A：一步获胜
// ---------------------------------------------------------------------------

describe('夹具 A：红方一步获胜', () => {
  it('唯一获胜着法为 (-3,4,-1) → (-4,5,-1)', () => {
    const state = fixture(FIXTURE_A);
    const wins = getLegalMoves(state).filter((m) => {
      const applied = applyMove(state, m);
      return applied.ok && applied.value.result?.reason === 'goal';
    });
    expect(wins).toHaveLength(1);
    expect(toTuple(wins[0].path[0])).toEqual([-4, 5, -1]);
    expect(wins[0].kind).toBe('step');
  });

  it('执行后立即终局，红方 10/10', () => {
    const state = fixture(FIXTURE_A);
    const move: LegalMove = {
      piece: { x: -3, y: 4, z: -1 },
      path: [{ x: -4, y: 5, z: -1 }],
      kind: 'step',
    };
    const applied = applyMove(state, move);
    expect(applied.ok).toBe(true);
    if (!applied.ok) return;

    expect(applied.value.result).toEqual({ winner: 'R', reason: 'goal' });
    expect(applied.value.state.phase).toBe('FINISHED');
    expect(applied.value.state.ply).toBe(81);
    expect(countInTargetCamp(applied.value.state, 'R')).toBe(10);
    expect(validateState(applied.value.state).ok).toBe(true);

    // 终局后拒绝走子。
    const again = applyMove(applied.value.state, move);
    expect(again.ok ? null : again.error.code).toBe('GAME_OVER');
  });
});

// ---------------------------------------------------------------------------
// 夹具 C：连跳与结束回合
// ---------------------------------------------------------------------------

describe('夹具 C：连跳', () => {
  it('首跳 → (2,-2,0)，可继续 → (2,0,-2)', () => {
    const state = fixture(FIXTURE_C);
    const options = queryMoveOptions(state, { x: 0, y: 0, z: 0 });
    expect(options.ok).toBe(true);
    if (!options.ok) return;

    expect(options.value.phase).toBe('select');
    expect(options.value.jumps.map(toTuple)).toContainEqual([2, -2, 0]);

    const first = queryMoveOptions(state, { x: 0, y: 0, z: 0 }, [{ x: 2, y: -2, z: 0 }]);
    expect(first.ok).toBe(true);
    if (!first.ok) return;
    expect(first.value.phase).toBe('jump');
    expect(first.value.canEndTurn).toBe(true);
    expect(first.value.jumps.map(toTuple)).toContainEqual([2, 0, -2]);

    const second = queryMoveOptions(state, { x: 0, y: 0, z: 0 }, [
      { x: 2, y: -2, z: 0 },
      { x: 2, y: 0, z: -2 },
    ]);
    expect(second.ok).toBe(true);
    if (!second.ok) return;
    expect(second.value.canEndTurn).toBe(true);
    expect(second.value.jumps).toHaveLength(0);
  });

  it('单跳与双跳均为合法完整着法，各计 1 手', () => {
    const state = fixture(FIXTURE_C);
    const single: LegalMove = {
      piece: { x: 0, y: 0, z: 0 },
      path: [{ x: 2, y: -2, z: 0 }],
      kind: 'jump',
    };
    const double: LegalMove = {
      piece: { x: 0, y: 0, z: 0 },
      path: [
        { x: 2, y: -2, z: 0 },
        { x: 2, y: 0, z: -2 },
      ],
      kind: 'jump',
    };

    expect(isLegalMove(state, single)).toBe(true);
    expect(isLegalMove(state, double)).toBe(true);

    const a = applyMove(state, single);
    const b = applyMove(state, double);
    expect(a.ok && b.ok).toBe(true);
    if (!a.ok || !b.ok) return;
    expect(a.value.state.ply).toBe(41);
    expect(b.value.state.ply).toBe(41);
    expect(a.value.state.turn).toBe('B');
    expect(b.value.state.turn).toBe('B');
  });

  it('跳跃后改单步 → MIXED_MOVE', () => {
    const state = fixture(FIXTURE_C);
    const mixed: LegalMove = {
      piece: { x: 0, y: 0, z: 0 },
      path: [
        { x: 2, y: -2, z: 0 },
        { x: 1, y: -2, z: 1 },
      ],
      kind: 'jump',
    };
    const result = applyMove(state, mixed);
    expect(result.ok ? null : result.error.code).toBe('MIXED_MOVE');
  });
});

// ---------------------------------------------------------------------------
// 非法走子
// ---------------------------------------------------------------------------

describe('非法走子拒绝且不改变状态', () => {
  const state = createInitialState({ gameId: 11 });

  const cases: Array<[string, LegalMove, string]> = [
    [
      '空孔位 → EMPTY_CELL',
      { piece: { x: 0, y: 0, z: 0 }, path: [{ x: 1, y: 0, z: -1 }], kind: 'step' },
      'EMPTY_CELL',
    ],
    [
      '对方棋子 → NOT_YOUR_PIECE',
      { piece: { x: -4, y: 8, z: -4 }, path: [{ x: -3, y: 8, z: -5 }], kind: 'step' },
      'NOT_YOUR_PIECE',
    ],
    [
      '不相邻单步 → NOT_ADJACENT',
      { piece: { x: 4, y: -8, z: 4 }, path: [{ x: 4, y: -6, z: 2 }], kind: 'step' },
      'NOT_ADJACENT',
    ],
    [
      '目标被占用 → OCCUPIED',
      { piece: { x: 4, y: -5, z: 1 }, path: [{ x: 4, y: -6, z: 2 }], kind: 'step' },
      'OCCUPIED',
    ],
    [
      '跨越空位 → JUMP_NO_PIECE',
      { piece: { x: 1, y: -5, z: 4 }, path: [{ x: 1, y: -3, z: 2 }], kind: 'jump' },
      'JUMP_NO_PIECE',
    ],
  ];

  for (const [name, move, code] of cases) {
    it(name, () => {
      const before = JSON.stringify(state);
      const result = applyMove(state, move);
      expect(result.ok ? null : result.error.code).toBe(code);
      expect(JSON.stringify(state)).toBe(before);
    });
  }
});

// ---------------------------------------------------------------------------
// 轮空与手数上限
// ---------------------------------------------------------------------------

describe('轮空与手数上限', () => {
  it('无合法着法时轮空；连续两次轮空判平局', () => {
    const state = createInitialState({ gameId: 21 });
    const first = passTurn(state, { legalMoves: [] });
    expect(first.ok).toBe(true);
    if (!first.ok) return;
    expect(first.value.passedPlayer).toBe('R');
    expect(first.value.state.turn).toBe('B');
    expect(first.value.state.ply).toBe(1);
    expect(first.value.state.consecutivePasses).toBe(1);
    expect(first.value.result).toBeNull();

    const second = passTurn(first.value.state, { legalMoves: [] });
    expect(second.ok).toBe(true);
    if (!second.ok) return;
    expect(second.value.result).toEqual({ winner: 'draw', reason: 'both-pass' });
    expect(second.value.state.phase).toBe('FINISHED');
    expect(second.value.state.consecutivePasses).toBe(2);
  });

  it('有合法着法时禁止轮空', () => {
    const state = createInitialState({ gameId: 22 });
    const result = passTurn(state);
    expect(result.ok ? null : result.error.code).toBe('PASS_NOT_ALLOWED');
  });

  it('第 300 手后判平局（胜负优先）', () => {
    const state = fixture({ ...FIXTURE_C, ply: 299 }, 23);
    const move: LegalMove = {
      piece: { x: 0, y: 0, z: 0 },
      path: [{ x: 1, y: 0, z: -1 }],
      kind: 'step',
    };
    const applied = applyMove(state, move);
    expect(applied.ok).toBe(true);
    if (!applied.ok) return;
    expect(applied.value.state.ply).toBe(300);
    expect(applied.value.result).toEqual({ winner: 'draw', reason: 'ply-limit' });
    expect(validateState(applied.value.state).ok).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 夹具加载
// ---------------------------------------------------------------------------

describe('createStateFromFixture', () => {
  it('拒绝非法夹具', () => {
    const short = { ...FIXTURE_A, pieces: FIXTURE_A.pieces.slice(0, 19) };
    expect(createStateFromFixture(short, { gameId: 1 }).ok).toBe(false);

    const duplicated = {
      ...FIXTURE_A,
      pieces: [...FIXTURE_A.pieces.slice(0, 19), FIXTURE_A.pieces[0]],
    };
    expect(createStateFromFixture(duplicated, { gameId: 1 }).ok).toBe(false);

    // 红方已 10/10 → 不能作为进行中夹具。
    const tupleOf = (pos: CubeCoord | CoordTuple): CoordTuple =>
      'x' in pos ? [pos.x, pos.y, pos.z] : pos;
    const wonPieces = FIXTURE_A.pieces.map((p) => {
      const t = tupleOf(p.pos);
      return p.color === 'R' && t[0] === -3 && t[1] === 4
        ? { color: 'R' as const, pos: [-4, 5, -1] as const }
        : p;
    });
    expect(createStateFromFixture({ ...FIXTURE_A, pieces: wonPieces }, { gameId: 1 }).ok).toBe(false);
  });

  it('返回规范排序的棋子', () => {
    const state = fixture(FIXTURE_A);
    const firstTen = state.pieces.slice(0, 10);
    expect(firstTen.every((p) => p.color === 'R')).toBe(true);
    expect(state.pieces.slice(10).every((p) => p.color === 'B')).toBe(true);
  });
});
