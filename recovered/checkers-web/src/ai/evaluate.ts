/**
 * 局面评估与战术检测（启发式，契约 §6.2 / PRD §7）。
 *
 * 评估只读取状态，不修改任何入参。
 */

import type { CubeCoord, GameState, LegalMove, Player } from '../types/index.js';
import { getLegalMoves } from '../engine/rules.js';
import {
  BOARD,
  OTHER_PLAYER,
  coordKey,
  countInTargetCamp,
  isInTargetCamp,
} from '../engine/board.js';

function hexDistance(a: CubeCoord, b: CubeCoord): number {
  return (Math.abs(a.x - b.x) + Math.abs(a.y - b.y) + Math.abs(a.z - b.z)) / 2;
}

const TARGET_DISTANCE: Readonly<Record<Player, ReadonlyMap<string, number>>> = (() => {
  const build = (player: Player): ReadonlyMap<string, number> => {
    const targetCamp = BOARD.camps[BOARD.targetCamp[player]];
    const map = new Map<string, number>();
    for (const cell of BOARD.cells) {
      let best = Number.POSITIVE_INFINITY;
      for (const target of targetCamp) {
        const d = hexDistance(cell, target);
        if (d < best) best = d;
      }
      map.set(coordKey(cell), best);
    }
    return map;
  };
  return { R: build('R'), B: build('B') };
})();

export function distanceToTarget(player: Player, pos: CubeCoord): number {
  return TARGET_DISTANCE[player].get(coordKey(pos)) ?? 0;
}

export function otherPlayer(player: Player): Player {
  return OTHER_PLAYER[player];
}

/**
 * 局面分（从 player 视角，越大越好）。
 * 进营权重远高于距离推进；距离按到目标营的最近六角距离计。
 */
export function evaluateState(state: GameState, player: Player): number {
  let score = 0;
  for (const piece of state.pieces) {
    const inTarget = isInTargetCamp(piece.color, piece.pos);
    const value = (inTarget ? 100 : 0) - distanceToTarget(piece.color, piece.pos);
    if (piece.color === player) score += value;
    else score -= value;
  }
  return score;
}

/** 该着法是否使 player 立即占满目标营。 */
export function isWinningMove(state: GameState, move: LegalMove, player: Player): boolean {
  if (move.path.length === 0) return false;
  const to = move.path[move.path.length - 1];
  const base = countInTargetCamp(state, player) - (isInTargetCamp(player, move.piece) ? 1 : 0);
  return base + (isInTargetCamp(player, to) ? 1 : 0) === 10;
}

export function findWinningMoves(state: GameState, player: Player): LegalMove[] {
  const moves = getLegalMoves(state, { player });
  return moves.filter((move) => isWinningMove(state, move, player));
}

/** 对手是否拥有一走着法的获胜威胁（不要求轮到对手）。 */
export function hasImmediateWin(state: GameState, player: Player): boolean {
  if (state.phase !== 'PLAYING') return false;
  const moves = getLegalMoves(state, { player });
  for (const move of moves) {
    if (isWinningMove(state, move, player)) return true;
  }
  return false;
}

/** 着法排序分：推进距离 + 进营 + 立即获胜。 */
export function moveOrderScore(state: GameState, move: LegalMove, player: Player): number {
  if (move.path.length === 0) return -Number.POSITIVE_INFINITY;
  const to = move.path[move.path.length - 1];
  let score = (distanceToTarget(player, move.piece) - distanceToTarget(player, to)) * 10;
  if (isInTargetCamp(player, to)) score += 40;
  if (isWinningMove(state, move, player)) score += 1_000_000;
  return score;
}
