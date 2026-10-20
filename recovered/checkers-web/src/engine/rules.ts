/**
 * 规则引擎：合法着法、路径校验、走子与轮空（契约 §4）。
 *
 * 所有函数同步、纯函数：不修改入参，用户输入错误一律返回 Result，不抛异常。
 */

import type {
  AppliedMove,
  CellKey,
  CubeCoord,
  GameResult,
  GameState,
  LegalMove,
  LegalMoveFilter,
  MoveOutcome,
  MoveQueryResult,
  PassOutcome,
  Piece,
  Player,
  Result,
} from '../types/index.js';
import { err, ok } from '../types/index.js';

export type {
  LegalMoveFilter,
  MoveOutcome,
  MoveQueryResult,
  PassOutcome,
} from '../types/index.js';

import {
  BOARD,
  OTHER_PLAYER,
  compareCoordAsc,
  countInTargetCamp,
  coordKey,
  getPieceAt,
  isCell,
  isInTargetCamp,
} from './board.js';

export const PLY_LIMIT = 300;

const EMPTY_MOVES: readonly LegalMove[] = Object.freeze([]);

type Occupancy = ReadonlyMap<CellKey, Player>;

function occupancyOf(state: GameState, excludeOrigin?: CubeCoord): Map<CellKey, Player> {
  const occ = new Map<CellKey, Player>();
  const excludeKey = excludeOrigin ? coordKey(excludeOrigin) : null;
  for (const piece of state.pieces) {
    const key = coordKey(piece.pos);
    if (excludeKey !== null && key === excludeKey) continue;
    occ.set(key, piece.color);
  }
  return occ;
}

function addCoord(a: CubeCoord, d: CubeCoord): CubeCoord {
  return { x: a.x + d.x, y: a.y + d.y, z: a.z + d.z };
}

function sameCoord(a: CubeCoord, b: CubeCoord): boolean {
  return a.x === b.x && a.y === b.y && a.z === b.z;
}

function samePath(a: readonly CubeCoord[], b: readonly CubeCoord[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (!sameCoord(a[i], b[i])) return false;
  }
  return true;
}

/** 排序：origin（y 降序、x 升序、z 升序）→ kind（step 先）→ path 长度升序 → path 逐项升序。 */
export function compareMoveList(a: LegalMove, b: LegalMove): number {
  if (a.piece.y !== b.piece.y) return b.piece.y - a.piece.y;
  if (a.piece.x !== b.piece.x) return a.piece.x - b.piece.x;
  if (a.piece.z !== b.piece.z) return a.piece.z - b.piece.z;
  if (a.kind !== b.kind) return a.kind === 'step' ? -1 : 1;
  if (a.path.length !== b.path.length) return a.path.length - b.path.length;
  for (let i = 0; i < a.path.length; i += 1) {
    const c = compareCoordAsc(a.path[i], b.path[i]);
    if (c !== 0) return c;
  }
  return 0;
}

/** UI 高亮用的落点排序：y 降序、x 升序、z 升序。 */
function compareBoardOrder(a: CubeCoord, b: CubeCoord): number {
  if (a.y !== b.y) return b.y - a.y;
  if (a.x !== b.x) return a.x - b.x;
  return a.z - b.z;
}

/**
 * 若 action 方将 origin 上的棋子移动到 landing，是否立即占满目标营（10/10）。
 * 只考虑当前这条路径的最终落点。
 */
function winsByMovingTo(
  state: GameState,
  player: Player,
  origin: CubeCoord,
  landing: CubeCoord,
): boolean {
  const base = countInTargetCamp(state, player) - (isInTargetCamp(player, origin) ? 1 : 0);
  return base + (isInTargetCamp(player, landing) ? 1 : 0) === 10;
}

interface GenerateContext {
  readonly state: GameState;
  readonly player: Player;
  readonly origin: CubeCoord;
  readonly occ: Occupancy;
  readonly baseCount: number;
  readonly out: LegalMove[];
}

function pushMove(ctx: GenerateContext, path: readonly CubeCoord[], kind: 'jump'): void {
  ctx.out.push({ piece: ctx.origin, path: path.map((p) => ({ ...p })), kind });
}

/** 枚举从 origin 出发的全部跳跃/连跳（每个前缀都是一个完整合法着法）。 */
function generateJumps(ctx: GenerateContext): void {
  const visited = new Set<CellKey>([coordKey(ctx.origin)]);

  const dfs = (curr: CubeCoord, path: readonly CubeCoord[]): void => {
    for (const d of BOARD.directions) {
      const mid = addCoord(curr, d);
      if (!isCell(mid) || !ctx.occ.has(coordKey(mid))) continue;

      const landing = addCoord(curr, d);
      const land = addCoord(landing, d);
      if (!isCell(land)) continue;

      const landKey = coordKey(land);
      if (ctx.occ.has(landKey)) continue;
      if (visited.has(landKey)) continue;

      const nextPath = [...path, land];
      pushMove(ctx, nextPath, 'jump');

      // 获胜后不得继续延伸。
      if (ctx.baseCount + (isInTargetCamp(ctx.player, land) ? 1 : 0) === 10) continue;

      visited.add(landKey);
      dfs(land, nextPath);
      visited.delete(landKey);
    }
  };

  dfs(ctx.origin, []);
}

function generateMovesForOrigin(
  state: GameState,
  player: Player,
  origin: CubeCoord,
): LegalMove[] {
  const occ = occupancyOf(state, origin);
  const baseCount = countInTargetCamp(state, player) - (isInTargetCamp(player, origin) ? 1 : 0);
  const out: LegalMove[] = [];

  // 单步
  for (const d of BOARD.directions) {
    const target = addCoord(origin, d);
    if (!isCell(target)) continue;
    if (occ.has(coordKey(target))) continue;
    out.push({ piece: { ...origin }, path: [{ ...target }], kind: 'step' });
  }

  // 跳跃 / 连跳
  const ctx: GenerateContext = { state, player, origin, occ, baseCount, out };
  generateJumps(ctx);

  return out;
}

function dedupeAndSort(moves: readonly LegalMove[]): readonly LegalMove[] {
  const seen = new Set<string>();
  const result: LegalMove[] = [];
  for (const move of moves) {
    const key = `${coordKey(move.piece)}|${move.kind}|${move.path.map(coordKey).join('|')}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(move);
  }
  result.sort(compareMoveList);
  return result;
}

const turnCache = new WeakMap<GameState, readonly LegalMove[]>();
const playerCache = new WeakMap<GameState, Map<Player, readonly LegalMove[]>>();

function computeAllMoves(state: GameState, player: Player): readonly LegalMove[] {
  const moves: LegalMove[] = [];
  for (const piece of state.pieces) {
    if (piece.color !== player) continue;
    moves.push(...generateMovesForOrigin(state, player, piece.pos));
  }
  return dedupeAndSort(moves);
}

/**
 * 生成合法着法（契约 §4.3）。
 * - phase=FINISHED → []
 * - filter.player 默认 state.turn；filter.piece 限定 origin
 * - 排序规范、去重、确定性；获胜后不延伸
 */
export function getLegalMoves(
  state: GameState,
  filter?: LegalMoveFilter,
): readonly LegalMove[] {
  if (!state || state.phase !== 'PLAYING') return EMPTY_MOVES;

  const player = filter?.player ?? state.turn;

  if (filter?.piece) {
    const origin = filter.piece;
    if (!isCell(origin)) return EMPTY_MOVES;
    const piece = getPieceAt(state, origin);
    if (!piece || piece.color !== player) return EMPTY_MOVES;
    return dedupeAndSort(generateMovesForOrigin(state, player, origin));
  }

  if (player === state.turn) {
    const cached = turnCache.get(state);
    if (cached) return cached;
    const computed = computeAllMoves(state, player);
    turnCache.set(state, computed);
    return computed;
  }

  let perPlayer = playerCache.get(state);
  if (!perPlayer) {
    perPlayer = new Map();
    playerCache.set(state, perPlayer);
  }
  const cached = perPlayer.get(player);
  if (cached) return cached;
  const computed = computeAllMoves(state, player);
  perPlayer.set(player, computed);
  return computed;
}

function moveInList(moves: readonly LegalMove[], move: LegalMove): boolean {
  for (const candidate of moves) {
    if (
      sameCoord(candidate.piece, move.piece) &&
      candidate.kind === move.kind &&
      samePath(candidate.path, move.path)
    ) {
      return true;
    }
  }
  return false;
}

/** 任何畸形 move 返回 false，不抛异常。 */
export function isLegalMove(state: GameState, move: LegalMove): boolean {
  try {
    if (!move || !isCell(move.piece) || !Array.isArray(move.path) || move.path.length < 1) {
      return false;
    }
    if (move.kind !== 'step' && move.kind !== 'jump') return false;
    if (move.kind === 'step' && move.path.length !== 1) return false;
    if (!move.path.every(isCell)) return false;
    return moveInList(getLegalMoves(state, { piece: move.piece }), move);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// 路径结构校验
// ---------------------------------------------------------------------------

type PathErrorCode =
  | 'OUT_OF_BOARD'
  | 'NOT_ADJACENT'
  | 'OCCUPIED'
  | 'JUMP_NO_PIECE'
  | 'JUMP_LANDING_OCCUPIED'
  | 'PATH_REPEAT'
  | 'PATH_BROKEN'
  | 'MIXED_MOVE'
  | 'INCOMPLETE_PATH';

function validateStepPath(
  occ: Occupancy,
  origin: CubeCoord,
  path: readonly CubeCoord[],
): PathErrorCode | null {
  if (path.length !== 1) return 'MIXED_MOVE';
  const target = path[0];
  if (!isCell(target)) return 'OUT_OF_BOARD';

  let adjacent = false;
  for (const d of BOARD.directions) {
    if (sameCoord(addCoord(origin, d), target)) {
      adjacent = true;
      break;
    }
  }
  if (!adjacent) return 'NOT_ADJACENT';
  if (occ.has(coordKey(target))) return 'OCCUPIED';
  return null;
}

function validateJumpPath(
  occ: Occupancy,
  origin: CubeCoord,
  path: readonly CubeCoord[],
): PathErrorCode | null {
  if (path.length < 1) return 'INCOMPLETE_PATH';

  const visited = new Set<CellKey>([coordKey(origin)]);
  let curr = origin;

  for (const next of path) {
    if (!isCell(next)) return 'OUT_OF_BOARD';
    const nextKey = coordKey(next);
    if (visited.has(nextKey)) return 'PATH_REPEAT';

    // 相邻单步出现在跳跃序列中 → MIXED_MOVE。
    let isAdjacentStep = false;
    let jumpDirection: CubeCoord | null = null;
    for (const d of BOARD.directions) {
      if (sameCoord(addCoord(curr, d), next)) {
        isAdjacentStep = true;
        break;
      }
      const twice = addCoord(addCoord(curr, d), d);
      if (sameCoord(twice, next)) {
        jumpDirection = d;
        break;
      }
    }
    if (isAdjacentStep) return 'MIXED_MOVE';
    if (!jumpDirection) return 'PATH_BROKEN';

    const mid = addCoord(curr, jumpDirection);
    if (!isCell(mid) || !occ.has(coordKey(mid))) return 'JUMP_NO_PIECE';
    if (occ.has(nextKey)) return 'JUMP_LANDING_OCCUPIED';

    visited.add(nextKey);
    curr = next;
  }

  return null;
}

function validatePathStructure(
  state: GameState,
  move: LegalMove,
  player: Player,
): PathErrorCode | null {
  const occ = occupancyOf(state, move.piece);
  if (move.kind === 'step') return validateStepPath(occ, move.piece, move.path);
  return validateJumpPath(occ, move.piece, move.path);
}

function findWinningPrefix(
  state: GameState,
  move: LegalMove,
  player: Player,
): boolean {
  if (move.kind !== 'jump') return false;
  for (let i = 0; i < move.path.length - 1; i += 1) {
    if (winsByMovingTo(state, player, move.piece, move.path[i])) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// queryMoveOptions（契约 §4.4）
// ---------------------------------------------------------------------------

export function queryMoveOptions(
  state: GameState,
  piece: CubeCoord,
  pathSoFar: readonly CubeCoord[] = [],
): Result<MoveQueryResult> {
  if (!state || state.phase !== 'PLAYING') return err('GAME_OVER');
  if (!isCell(piece)) return err('OUT_OF_BOARD');

  const occupant = getPieceAt(state, piece);
  if (!occupant) return err('EMPTY_CELL');
  if (occupant.color !== state.turn) return err('NOT_YOUR_PIECE');

  const player = state.turn;
  const allMoves = getLegalMoves(state, { piece });
  const jumpsFromPiece = allMoves.filter((m) => m.kind === 'jump');

  if (pathSoFar.length === 0) {
    const steps = allMoves
      .filter((m) => m.kind === 'step')
      .map((m) => m.path[0])
      .sort(compareBoardOrder);
    const jumps = uniqCoords(jumpsFromPiece.map((m) => m.path[0]));
    return ok({
      piece: { ...piece },
      pathSoFar: [],
      phase: 'select',
      steps,
      jumps,
      canEndTurn: false,
      winningIfEndTurn: false,
    });
  }

  // --- jump 态：先做逐段结构校验 ---
  const occ = occupancyOf(state, piece);
  const visited = new Set<CellKey>([coordKey(piece)]);
  let curr = piece;

  for (const next of pathSoFar) {
    if (!isCell(next)) return err('OUT_OF_BOARD');
    const nextKey = coordKey(next);
    if (visited.has(nextKey)) return err('PATH_REPEAT');

    let isAdjacentStep = false;
    let jumpDirection: CubeCoord | null = null;
    for (const d of BOARD.directions) {
      if (sameCoord(addCoord(curr, d), next)) {
        isAdjacentStep = true;
        break;
      }
      const twice = addCoord(addCoord(curr, d), d);
      if (sameCoord(twice, next)) {
        jumpDirection = d;
        break;
      }
    }
    if (isAdjacentStep) return err('MIXED_MOVE');
    if (!jumpDirection) return err('PATH_BROKEN');

    const mid = addCoord(curr, jumpDirection);
    if (!isCell(mid) || !occ.has(coordKey(mid))) return err('JUMP_NO_PIECE');
    if (occ.has(nextKey)) return err('JUMP_LANDING_OCCUPIED');

    visited.add(nextKey);
    curr = next;
  }

  // 必须是至少一个合法着法的前缀。
  const matching = jumpsFromPiece.filter((m) => isPrefixOf(pathSoFar, m.path));
  if (matching.length === 0) return err('ILLEGAL_MOVE');

  const exact = matching.find((m) => m.path.length === pathSoFar.length) ?? null;
  const canEndTurn = exact !== null;
  const winningIfEndTurn =
    exact !== null && winsByMovingTo(state, player, piece, exact.path[exact.path.length - 1]);

  const jumps = winningIfEndTurn
    ? []
    : uniqCoords(
        matching
          .filter((m) => m.path.length > pathSoFar.length)
          .map((m) => m.path[pathSoFar.length]),
      );

  return ok({
    piece: { ...piece },
    pathSoFar: pathSoFar.map((p) => ({ ...p })),
    phase: 'jump',
    steps: [],
    jumps,
    canEndTurn,
    winningIfEndTurn,
  });
}

function isPrefixOf(prefix: readonly CubeCoord[], path: readonly CubeCoord[]): boolean {
  if (prefix.length > path.length) return false;
  for (let i = 0; i < prefix.length; i += 1) {
    if (!sameCoord(prefix[i], path[i])) return false;
  }
  return true;
}

function uniqCoords(coords: readonly CubeCoord[]): readonly CubeCoord[] {
  const seen = new Set<CellKey>();
  const out: CubeCoord[] = [];
  for (const c of coords) {
    const key = coordKey(c);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ...c });
  }
  out.sort(compareBoardOrder);
  return out;
}

// ---------------------------------------------------------------------------
// applyMove（契约 §4.5）
// ---------------------------------------------------------------------------

function canonicalPieces(pieces: readonly Piece[]): readonly Piece[] {
  return pieces
    .map((p) => ({ color: p.color, pos: { ...p.pos } }))
    .sort((a, b) => {
      if (a.color !== b.color) return a.color === 'R' ? -1 : 1;
      return compareCoordAsc(a.pos, b.pos);
    });
}

export function applyMove(state: GameState, move: LegalMove): Result<MoveOutcome> {
  // 1. 阶段
  if (!state || state.phase !== 'PLAYING') return err('GAME_OVER');

  // 2. move 形状
  if (!move || !move.piece || !isCell(move.piece)) return err('OUT_OF_BOARD');
  if (!Array.isArray(move.path) || move.path.length < 1) return err('INCOMPLETE_PATH');
  if (move.kind !== 'step' && move.kind !== 'jump') return err('ILLEGAL_MOVE');
  if (move.kind === 'step' && move.path.length !== 1) return err('MIXED_MOVE');
  if (move.kind === 'jump' && move.path.length < 1) return err('INCOMPLETE_PATH');

  // 3. origin 必须是当前行动方的棋子
  const occupant = getPieceAt(state, move.piece);
  if (!occupant) return err('EMPTY_CELL');
  if (occupant.color !== state.turn) return err('NOT_YOUR_PIECE');

  // 4. 路径结构
  const pathError = validatePathStructure(state, move, state.turn);
  if (pathError) return err(pathError);

  // 5. 精确校验：必须存在于合法着法集合
  const legal = getLegalMoves(state, { piece: move.piece });
  if (!moveInList(legal, move)) {
    if (findWinningPrefix(state, move, state.turn)) return err('PATH_BEYOND_WIN', '获胜后继续跳跃');
    return err('ILLEGAL_MOVE');
  }

  // 6. 逐跳模拟：获胜后不得继续
  if (findWinningPrefix(state, move, state.turn)) {
    return err('PATH_BEYOND_WIN', '获胜后继续跳跃');
  }

  // 7. 原子应用
  const player = state.turn;
  const to = move.path[move.path.length - 1];
  const plyAfter = state.ply + 1;
  const won = winsByMovingTo(state, player, move.piece, to);

  const pieces = canonicalPieces(
    state.pieces.map((p) =>
      sameCoord(p.pos, move.piece) ? { color: p.color, pos: { ...to } } : p,
    ),
  );

  let phase: GameState['phase'] = 'PLAYING';
  let result: GameResult | null = null;
  if (won) {
    phase = 'FINISHED';
    result = { winner: player, reason: 'goal' };
  } else if (plyAfter >= PLY_LIMIT) {
    phase = 'FINISHED';
    result = { winner: 'draw', reason: 'ply-limit' };
  }

  const applied: AppliedMove = {
    player,
    piece: { ...move.piece },
    path: move.path.map((p) => ({ ...p })),
    kind: move.kind,
    to: { ...to },
    plyBefore: state.ply,
    plyAfter,
  };

  const newState: GameState = {
    gameId: state.gameId,
    phase,
    turn: OTHER_PLAYER[player],
    ply: plyAfter,
    pieces,
    consecutivePasses: 0,
    result,
    lastMove: applied,
  };

  return ok({ state: newState, move: applied, result });
}

// ---------------------------------------------------------------------------
// passTurn（契约 §4.7）
// ---------------------------------------------------------------------------

export function passTurn(
  state: GameState,
  options?: { readonly legalMoves?: readonly LegalMove[] },
): Result<PassOutcome> {
  if (!state || state.phase !== 'PLAYING') return err('GAME_OVER');

  const legalMoves = options?.legalMoves ?? getLegalMoves(state);
  if (legalMoves.length > 0) return err('PASS_NOT_ALLOWED');

  const player = state.turn;
  const plyAfter = state.ply + 1;
  const passes = Math.min(2, state.consecutivePasses + 1) as 0 | 1 | 2;

  let phase: GameState['phase'] = 'PLAYING';
  let result: GameResult | null = null;
  if (passes >= 2) {
    phase = 'FINISHED';
    result = { winner: 'draw', reason: 'both-pass' };
  } else if (plyAfter >= PLY_LIMIT) {
    phase = 'FINISHED';
    result = { winner: 'draw', reason: 'ply-limit' };
  }

  const newState: GameState = {
    gameId: state.gameId,
    phase,
    turn: OTHER_PLAYER[player],
    ply: plyAfter,
    pieces: canonicalPieces(state.pieces),
    consecutivePasses: passes,
    result,
    lastMove: state.lastMove,
  };

  return ok({ state: newState, passedPlayer: player, result, passed: true });
}
