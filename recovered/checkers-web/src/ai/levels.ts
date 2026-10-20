/**
 * 难度档位与默认 AI 策略（PRD §7.1，契约 §6.2 / §6.5）。
 *
 * 硬约束优先级：
 *   1. 存在一步获胜 → 必选获胜着法；
 *   2. normal / hard：对手存在一步获胜威胁且存在化解着法 → 必挡；
 *   3. 其余按难度做启发式搜索。
 */

import type {
  AiMoveRequest,
  AiMoveResponse,
  AiStrategy,
  Clock,
  Difficulty,
  LegalMove,
  Player,
} from '../types/index.js';
import type { SeededRandom } from '../engine/random.js';
import { createSeededRandom, DEFAULT_CLOCK } from '../engine/random.js';
import { applyMove, getLegalMoves, isLegalMove } from '../engine/rules.js';
import {
  findWinningMoves,
  hasImmediateWin,
  otherPlayer,
} from './evaluate.js';
import { AI_MAX_SEARCH_BUDGET_MS } from './constants.js';
import { runSearch } from './search.js';

export interface DifficultyConfig {
  /** 最大迭代加深深度。 */
  readonly maxDepth: number;
  /** 根节点保留的候选数。 */
  readonly rootBranch: number;
  /** 深层节点保留的候选数。 */
  readonly innerBranch: number;
  /** 该档位单步搜索时间上限（ms）。 */
  readonly maxSearchMs: number;
}

export const DIFFICULTY_CONFIG: Readonly<Record<Difficulty, DifficultyConfig>> = {
  easy: { maxDepth: 1, rootBranch: 12, innerBranch: 8, maxSearchMs: 400 },
  normal: { maxDepth: 2, rootBranch: 16, innerBranch: 10, maxSearchMs: 1200 },
  hard: { maxDepth: 4, rootBranch: 18, innerBranch: 10, maxSearchMs: 1800 },
};

function pickFrom<T>(rng: SeededRandom, items: readonly T[]): T | null {
  if (items.length === 0) return null;
  return rng.pick(items);
}

/**
 * 若走完该着法后对手不再拥有一步获胜威胁，则视为“安全着法”。
 * 己方获胜或平局同样安全。
 */
function isSafeAgainstImmediateWin(
  request: AiMoveRequest,
  move: LegalMove,
): boolean {
  const opponent = otherPlayer(request.player);
  const applied = applyMove(request.state, move);
  if (!applied.ok) return false;
  const child = applied.value.state;
  if (child.phase === 'FINISHED') {
    return child.result?.winner !== opponent;
  }
  return !hasImmediateWin(child, opponent);
}

export interface DefaultStrategyOptions {
  /** 按难度覆盖搜索深度/分支/时间预算（深度可配置）。 */
  readonly configOverrides?: Partial<Record<Difficulty, Partial<DifficultyConfig>>>;
}

export function createDefaultStrategy(
  clock: Clock = DEFAULT_CLOCK,
  options: DefaultStrategyOptions = {},
): AiStrategy {
  const resolveConfig = (difficulty: Difficulty): DifficultyConfig => {
    const base = DIFFICULTY_CONFIG[difficulty] ?? DIFFICULTY_CONFIG.normal;
    const override = options.configOverrides?.[difficulty];
    return override ? { ...base, ...override } : base;
  };

  return {
    async choose(request: AiMoveRequest): Promise<AiMoveResponse> {
      const startedAt = clock.now();
      const elapsed = (): number => Math.max(0, Math.round(clock.now() - startedAt));
      const rng = createSeededRandom(request.seed);

      const moves = getLegalMoves(request.state, { player: request.player });
      if (moves.length === 0) {
        return {
          move: null,
          bestSoFar: null,
          degraded: false,
          reason: 'no-legal-moves',
          stats: { elapsedMs: elapsed() },
        };
      }

      // 1) 必胜底线（所有难度）
      const winningMoves = findWinningMoves(request.state, request.player);
      if (winningMoves.length > 0) {
        const move = pickFrom(rng, winningMoves);
        return {
          move,
          bestSoFar: move,
          degraded: false,
          stats: { depth: 0, elapsedMs: elapsed() },
        };
      }

      // 2) 必挡（normal / hard）
      let candidates: readonly LegalMove[] = moves;
      if (request.effectiveDifficulty !== 'easy') {
        const opponent = otherPlayer(request.player);
        if (hasImmediateWin(request.state, opponent)) {
          const safe = moves.filter((move) => isSafeAgainstImmediateWin(request, move));
          if (safe.length > 0) candidates = safe;
        }
      }

      if (candidates.length === 0) candidates = moves;

      if (candidates.length === 1) {
        const move = candidates[0];
        return {
          move,
          bestSoFar: move,
          degraded: false,
          stats: { depth: 1, elapsedMs: elapsed() },
        };
      }

      const config = resolveConfig(request.effectiveDifficulty);
      const budget = Math.max(0, Math.min(AI_MAX_SEARCH_BUDGET_MS, request.timeBudgetMs));
      const searchMs = Math.max(0, Math.min(config.maxSearchMs, budget) - 80);
      const deadline = clock.now() + searchMs;

      try {
        const result = await runSearch({
          state: request.state,
          player: request.player,
          candidates,
          maxDepth: config.maxDepth,
          rootBranch: config.rootBranch,
          innerBranch: config.innerBranch,
          deadline,
          clock,
          rng,
          signal: request.signal,
        });

        let move = result.move ?? pickFrom(rng, candidates);
        if (move && !isLegalMove(request.state, move)) move = pickFrom(rng, candidates);

        const degraded = result.completedDepth === 0;
        const response: AiMoveResponse = {
          move,
          bestSoFar: move,
          degraded,
          ...(degraded ? { reason: 'timeout' as const } : {}),
          stats: {
            nodes: result.nodes,
            depth: result.completedDepth,
            elapsedMs: elapsed(),
          },
        };
        return response;
      } catch (cause) {
        const aborted = request.signal.aborted;
        const fallback = pickFrom(rng, candidates);
        console.warn('[CC][AI] search failed', {
          gameId: request.gameId,
          difficulty: request.effectiveDifficulty,
          aborted,
          cause,
        });
        return {
          move: fallback,
          bestSoFar: fallback,
          degraded: true,
          reason: aborted ? 'aborted' : 'exception',
          stats: { elapsedMs: elapsed() },
        };
      }
    },
  };
}
