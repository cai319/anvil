/**
 * 对局状态构造、夹具加载与不变量校验（契约 §4.2）。
 */

import type {
  AppliedMove,
  CampProgress,
  CreateStateFromFixtureOptions,
  CubeCoord,
  FixturePieceInput,
  FixtureState,
  GameResult,
  GameState,
  Piece,
  Player,
  Result,
} from '../types/index.js';
import { err, ok } from '../types/index.js';
import {
  BOARD,
  compareCoordAsc,
  coordKey,
  countInTargetCamp,
  isCell,
  toCoord,
} from './board.js';

export interface CreateInitialStateOptions {
  readonly gameId: number;
}

function canonicalPieces(pieces: readonly Piece[]): readonly Piece[] {
  return pieces
    .map((p) => ({ color: p.color, pos: { ...p.pos } }))
    .sort((a, b) => {
      if (a.color !== b.color) return a.color === 'R' ? -1 : 1;
      return compareCoordAsc(a.pos, b.pos);
    });
}

function isPlayer(value: unknown): value is Player {
  return value === 'R' || value === 'B';
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

/** 初始局面：R 10 子占 C−，B 10 子占 C+；turn=R、ply=0。 */
export function createInitialState(options: CreateInitialStateOptions): GameState {
  const gameId = options?.gameId;
  if (!isPositiveInteger(gameId)) {
    throw new RangeError('gameId 必须为正整数');
  }
  return {
    gameId,
    phase: 'PLAYING',
    turn: 'R',
    ply: 0,
    pieces: canonicalPieces(BOARD.initialPieces),
    consecutivePasses: 0,
    result: null,
    lastMove: null,
  };
}

function validateLastMoveShape(lastMove: unknown): lastMove is AppliedMove {
  if (typeof lastMove !== 'object' || lastMove === null) return false;
  const m = lastMove as Partial<AppliedMove>;
  if (!isPlayer(m.player)) return false;
  if (m.kind !== 'step' && m.kind !== 'jump') return false;
  if (typeof m.piece !== 'object' || m.piece === null || !isCell(m.piece as CubeCoord)) {
    return false;
  }
  if (!Array.isArray(m.path) || m.path.length < 1) return false;
  if (!m.path.every((p) => typeof p === 'object' && p !== null && isCell(p as CubeCoord))) {
    return false;
  }
  const last = m.path[m.path.length - 1] as CubeCoord;
  if (!m.to || last.x !== m.to.x || last.y !== m.to.y || last.z !== m.to.z) return false;
  if (!isNonNegativeInteger(m.plyBefore) || !isNonNegativeInteger(m.plyAfter)) return false;
  if (m.plyAfter !== m.plyBefore + 1) return false;
  return true;
}

/**
 * 从夹具构造进行中的对局（测试/测试钩子专用）。
 * 任何非法输入都返回 INVALID_FIXTURE，绝不抛异常。
 */
export function createStateFromFixture(
  fixture: FixtureState,
  options: CreateStateFromFixtureOptions,
): Result<GameState> {
  try {
    if (typeof fixture !== 'object' || fixture === null) {
      return err('INVALID_FIXTURE', '夹具必须为对象');
    }
    if (!isPlayer(fixture.turn)) return err('INVALID_FIXTURE', 'turn 必须为 R 或 B');
    if (!isNonNegativeInteger(fixture.ply) || fixture.ply > 299) {
      return err('INVALID_FIXTURE', 'ply 必须为 0..299 的整数');
    }
    if (!isPositiveInteger(options?.gameId)) {
      return err('INVALID_FIXTURE', 'gameId 必须为正整数');
    }
    const consecutivePasses = options.consecutivePasses ?? 0;
    if (consecutivePasses !== 0 && consecutivePasses !== 1) {
      return err('INVALID_FIXTURE', 'consecutivePasses 只能是 0 或 1');
    }

    if (!Array.isArray(fixture.pieces) || fixture.pieces.length !== 20) {
      return err('INVALID_FIXTURE', 'pieces 必须恰好 20 项');
    }

    let redCount = 0;
    let blueCount = 0;
    const seen = new Set<string>();
    const pieces: Piece[] = [];

    for (const raw of fixture.pieces as readonly FixturePieceInput[]) {
      if (typeof raw !== 'object' || raw === null) {
        return err('INVALID_FIXTURE', '棋子项必须为对象');
      }
      if (!isPlayer(raw.color)) return err('INVALID_FIXTURE', '棋子颜色非法');
      let pos: CubeCoord;
      try {
        pos = toCoord(raw.pos);
      } catch (cause) {
        return err('INVALID_FIXTURE', '棋子坐标非法', cause);
      }
      if (!isCell(pos)) return err('INVALID_FIXTURE', `棋子坐标越界: ${coordKey(pos)}`);
      const key = coordKey(pos);
      if (seen.has(key)) return err('INVALID_FIXTURE', `棋子坐标重复: ${key}`);
      seen.add(key);

      if (raw.color === 'R') redCount += 1;
      else blueCount += 1;
      pieces.push({ color: raw.color, pos });
    }

    if (redCount !== 10 || blueCount !== 10) {
      return err('INVALID_FIXTURE', `棋子必须为 10 R + 10 B（实际 ${redCount} R / ${blueCount} B）`);
    }

    const lastMove = options.lastMove ?? null;
    if (lastMove !== null && !validateLastMoveShape(lastMove)) {
      return err('INVALID_FIXTURE', 'lastMove 形状非法');
    }

    const state: GameState = {
      gameId: options.gameId,
      phase: 'PLAYING',
      turn: fixture.turn,
      ply: fixture.ply,
      pieces: canonicalPieces(pieces),
      consecutivePasses: consecutivePasses as 0 | 1,
      result: null,
      lastMove: lastMove ?? null,
    };

    // 进行中夹具不得已是终局（任一方 10/10）。
    if (countInTargetCamp(state, 'R') === 10) {
      return err('INVALID_FIXTURE', '红方已占满目标营，不能作为进行中夹具');
    }
    if (countInTargetCamp(state, 'B') === 10) {
      return err('INVALID_FIXTURE', '蓝方已占满目标营，不能作为进行中夹具');
    }

    return ok(state);
  } catch (cause) {
    return err('INVALID_FIXTURE', '夹具解析异常', cause);
  }
}

/**
 * 校验 §2.2 的全部不变量。
 * 成功返回 ok(state)，失败返回 INVALID_STATE；不修改 state、不抛异常。
 */
export function validateState(state: GameState): Result<GameState> {
  try {
    if (typeof state !== 'object' || state === null) {
      return err('INVALID_STATE', 'state 必须为对象');
    }
    if (!isPositiveInteger(state.gameId)) return err('INVALID_STATE', 'gameId 必须为正整数');
    if (state.phase !== 'PLAYING' && state.phase !== 'FINISHED') {
      return err('INVALID_STATE', 'phase 非法');
    }
    if ((state.phase === 'FINISHED') !== (state.result !== null)) {
      return err('INVALID_STATE', 'phase 与 result 不一致');
    }
    if (!isPlayer(state.turn)) return err('INVALID_STATE', 'turn 非法');
    if (!isNonNegativeInteger(state.ply) || state.ply > 300) {
      return err('INVALID_STATE', 'ply 必须为 0..300 的整数');
    }
    if (state.consecutivePasses !== 0 && state.consecutivePasses !== 1 && state.consecutivePasses !== 2) {
      return err('INVALID_STATE', 'consecutivePasses 非法');
    }

    if (!Array.isArray(state.pieces) || state.pieces.length !== 20) {
      return err('INVALID_STATE', 'pieces 必须恰好 20 项');
    }

    let red = 0;
    let blue = 0;
    const seen = new Set<string>();
    for (const piece of state.pieces) {
      if (!piece || !isPlayer(piece.color)) return err('INVALID_STATE', '棋子颜色非法');
      if (!piece.pos || !isCell(piece.pos)) return err('INVALID_STATE', '棋子坐标非法');
      const key = coordKey(piece.pos);
      if (seen.has(key)) return err('INVALID_STATE', `棋子坐标重复: ${key}`);
      seen.add(key);
      if (piece.color === 'R') red += 1;
      else blue += 1;
    }
    if (red !== 10 || blue !== 10) {
      return err('INVALID_STATE', `棋子必须为 10 R + 10 B（实际 ${red} R / ${blue} B）`);
    }

    // lastMove 不变量
    const lm = state.lastMove;
    if (lm !== null) {
      if (!validateLastMoveShape(lm)) return err('INVALID_STATE', 'lastMove 形状非法');
      if (lm.plyAfter > state.ply) return err('INVALID_STATE', 'lastMove.plyAfter 大于 ply');
      if (lm.plyAfter !== state.ply) {
        const passesAfter = state.ply - lm.plyAfter;
        if (state.consecutivePasses === 0 || passesAfter !== state.consecutivePasses) {
          return err('INVALID_STATE', 'lastMove 与轮空计数不一致');
        }
      }
    }

    // result 不变量
    if (state.result !== null) {
      const result = state.result as GameResult;
      if (result.winner === 'draw') {
        if (result.reason === 'both-pass') {
          if (state.consecutivePasses !== 2) {
            return err('INVALID_STATE', 'both-pass 平局要求 consecutivePasses=2');
          }
        } else if (result.reason === 'ply-limit') {
          if (state.ply !== 300) return err('INVALID_STATE', 'ply-limit 平局要求 ply=300');
        } else {
          return err('INVALID_STATE', '平局 reason 非法');
        }
      } else if (result.winner === 'R' || result.winner === 'B') {
        if (result.reason !== 'goal') return err('INVALID_STATE', '获胜 reason 非法');
        if (countInTargetCamp(state, result.winner) !== 10) {
          return err('INVALID_STATE', '获胜方未占满目标营');
        }
      } else {
        return err('INVALID_STATE', 'result.winner 非法');
      }
    }

    if (state.consecutivePasses === 2 && state.result?.reason !== 'both-pass') {
      return err('INVALID_STATE', 'consecutivePasses=2 只能与 both-pass 同时出现');
    }

    // 双方不可能同时 10/10。
    if (countInTargetCamp(state, 'R') === 10 && countInTargetCamp(state, 'B') === 10) {
      return err('INVALID_STATE', '双方同时占满目标营');
    }

    return ok(state);
  } catch (cause) {
    return err('INVALID_STATE', '状态校验异常', cause);
  }
}

export function getProgress(state: GameState): CampProgress {
  return {
    R: countInTargetCamp(state, 'R'),
    B: countInTargetCamp(state, 'B'),
  };
}
