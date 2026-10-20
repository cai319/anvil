/**
 * alpha-beta 迭代加深搜索（契约 §6.2：可配置深度、时间预算、不阻塞）。
 *
 * 搜索本身以 Generator 实现，驱动层每累积少量节点让出事件循环，
 * 避免长时间占用主线程；超时/中止通过内部哨兵抛出。
 */

import type { Clock, GameState, LegalMove, Player } from '../types/index.js';
import type { SeededRandom } from '../engine/random.js';
import { applyMove, getLegalMoves } from '../engine/rules.js';
import { evaluateState, moveOrderScore } from './evaluate.js';

export const WIN_SCORE = 1_000_000;

/** 搜索内部超时哨兵。 */
export const SEARCH_TIMEOUT = Symbol('cc.search.timeout');
export const SEARCH_ABORTED = Symbol('cc.search.aborted');

const YIELD_EVERY = 128;

export interface SearchRequest {
  readonly state: GameState;
  readonly player: Player;
  readonly candidates: readonly LegalMove[];
  readonly maxDepth: number;
  readonly rootBranch: number;
  readonly innerBranch: number;
  readonly deadline: number;
  readonly clock: Clock;
  readonly rng: SeededRandom;
  readonly signal?: AbortSignal;
}

export interface SearchResult {
  readonly move: LegalMove | null;
  readonly score: number;
  readonly completedDepth: number;
  readonly nodes: number;
}

interface SearchContext {
  readonly clock: Clock;
  readonly deadline: number;
  readonly rng: SeededRandom;
  readonly rootBranch: number;
  readonly innerBranch: number;
  readonly signal?: AbortSignal;
  nodes: number;
}

interface RootResult {
  readonly move: LegalMove | null;
  readonly score: number;
}

function terminalValue(state: GameState, perspective: Player): number {
  const result = state.result;
  if (!result) return evaluateState(state, perspective);
  if (result.winner === 'draw') return 0;
  return result.winner === perspective
    ? WIN_SCORE - state.ply
    : -WIN_SCORE + state.ply;
}

function orderMoves(
  state: GameState,
  moves: readonly LegalMove[],
  player: Player,
  limit: number,
  rng: SeededRandom,
): LegalMove[] {
  const scored = moves.map((move, index) => ({
    move,
    index,
    score: moveOrderScore(state, move, player) + rng.next() * 1e-6,
  }));
  scored.sort((a, b) => b.score - a.score || a.index - b.index);
  const sliced = limit > 0 ? scored.slice(0, limit) : scored;
  return sliced.map((entry) => entry.move);
}

function* tick(ctx: SearchContext): Generator<void, void, void> {
  ctx.nodes += 1;
  if (ctx.signal?.aborted) throw SEARCH_ABORTED;
  if (ctx.clock.now() >= ctx.deadline) throw SEARCH_TIMEOUT;
  if (ctx.nodes % YIELD_EVERY === 0) yield;
}

function* negamax(
  state: GameState,
  player: Player,
  depth: number,
  alpha: number,
  beta: number,
  ctx: SearchContext,
): Generator<void, number, void> {
  yield* tick(ctx);

  if (state.phase === 'FINISHED') return terminalValue(state, player);
  if (depth <= 0) return evaluateState(state, player);

  const moves = getLegalMoves(state);
  if (moves.length === 0) return 0;

  const ordered = orderMoves(state, moves, player, ctx.innerBranch, ctx.rng);
  let best = -WIN_SCORE * 2;
  let lower = alpha;

  for (const move of ordered) {
    const applied = applyMove(state, move);
    if (!applied.ok) continue;
    const child = applied.value.state;
    const score = -(yield* negamax(child, child.turn, depth - 1, -beta, -lower, ctx));
    if (score > best) best = score;
    if (score > lower) lower = score;
    if (lower >= beta) break;
  }

  return best;
}

function* rootSearch(
  state: GameState,
  player: Player,
  depth: number,
  candidates: readonly LegalMove[],
  ctx: SearchContext,
): Generator<void, RootResult, void> {
  const ordered = orderMoves(state, candidates, player, ctx.rootBranch, ctx.rng);
  let bestMove: LegalMove | null = null;
  let bestScore = -WIN_SCORE * 2;
  let alpha = -WIN_SCORE * 2;
  const beta = WIN_SCORE * 2;

  for (const move of ordered) {
    const applied = applyMove(state, move);
    if (!applied.ok) continue;
    const child = applied.value.state;
    const score = -(yield* negamax(child, child.turn, depth - 1, -beta, -alpha, ctx));
    if (score > bestScore) {
      bestScore = score;
      bestMove = move;
    }
    if (score > alpha) alpha = score;
  }

  return { move: bestMove, score: bestScore };
}

function* searchAll(
  request: SearchRequest,
  ctx: SearchContext,
): Generator<void, SearchResult, void> {
  let best: LegalMove | null = request.candidates[0] ?? null;
  let bestScore = -WIN_SCORE * 2;
  let completedDepth = 0;

  for (let depth = 1; depth <= request.maxDepth; depth += 1) {
    if (ctx.clock.now() >= ctx.deadline) break;
    try {
      const result = yield* rootSearch(request.state, request.player, depth, request.candidates, ctx);
      if (result.move) {
        best = result.move;
        bestScore = result.score;
        completedDepth = depth;
      }
      if (result.score >= WIN_SCORE - 1000) break;
    } catch (error) {
      if (error === SEARCH_TIMEOUT || error === SEARCH_ABORTED) break;
      throw error;
    }
    if (ctx.clock.now() >= ctx.deadline) break;
  }

  return { move: best, score: bestScore, completedDepth, nodes: ctx.nodes };
}

/** 异步驱动 Generator 搜索；每 YIELD_EVERY 个节点让出一次事件循环。 */
export async function runSearch(request: SearchRequest): Promise<SearchResult> {
  const ctx: SearchContext = {
    clock: request.clock,
    deadline: request.deadline,
    rng: request.rng,
    rootBranch: request.rootBranch,
    innerBranch: request.innerBranch,
    ...(request.signal ? { signal: request.signal } : {}),
    nodes: 0,
  };

  const generator = searchAll(request, ctx);
  let step = generator.next();
  while (!step.done) {
    await new Promise<void>((resolve) => {
      request.clock.setTimeout(resolve, 0);
    });
    step = generator.next();
  }
  return step.value;
}
