/**
 * AI 客户端（契约 §6.1）。
 *
 * - `chooseMove` 不同步执行搜索：策略在 setTimeout(0) 之后启动。
 * - 返回值为 Promise，永不 reject；策略异常/超时/中止都转为降级响应。
 * - 硬时限内若策略未返回，使用 seeded 随机合法着法兜底。
 * - 每次请求拥有内部 AbortController：外部 signal 或 dispose() 都会中止搜索。
 */

import type {
  AiClient,
  AiClientOptions,
  AiMoveRequest,
  AiMoveResponse,
  LegalMove,
} from '../types/index.js';
import { createSeededRandom, DEFAULT_CLOCK } from '../engine/random.js';
import { getLegalMoves } from '../engine/rules.js';
import { AI_MAX_SEARCH_BUDGET_MS } from './constants.js';
import { createDefaultStrategy } from './levels.js';

export * from './constants.js';
export * from './evaluate.js';
export * from './levels.js';
export * from './search.js';

export type {
  AiClient,
  AiClientOptions,
  AiDegradeReason,
  AiMoveRequest,
  AiMoveResponse,
  AiStats,
  AiStrategy,
  Clock,
  Difficulty,
  WorkerLike,
} from '../types/index.js';

export { PLY_LIMIT } from '../engine/rules.js';
export { createSeededRandom, deriveSeed } from '../engine/random.js';

export function createAiClient(options: AiClientOptions = {}): AiClient {
  const clock = options.clock ?? DEFAULT_CLOCK;
  // workerFactory 为可选的前向兼容接口；默认实现使用时间片让出的主线程搜索。
  const strategy = options.strategy ?? createDefaultStrategy(clock);

  let disposed = false;
  const active = new Set<AbortController>();

  const fallbackMove = (request: AiMoveRequest, seed: number): LegalMove | null => {
    const moves = getLegalMoves(request.state, { player: request.player });
    if (moves.length === 0) return null;
    return createSeededRandom(seed).pick(moves);
  };

  return {
    chooseMove(request: AiMoveRequest): Promise<AiMoveResponse> {
      return new Promise<AiMoveResponse>((resolve) => {
        const startedAt = clock.now();
        const elapsed = (): number => Math.max(0, Math.round(clock.now() - startedAt));

        let settled = false;
        let hardTimer: number | null = null;
        let startTimer: number | null = null;

        // 内部 signal：合并外部取消与 dispose()，供策略/搜索观察。
        const controller = new AbortController();
        const signal = controller.signal;

        const onAbort = (): void => {
          settle({
            move: null,
            bestSoFar: null,
            degraded: true,
            reason: 'aborted',
            stats: { elapsedMs: elapsed() },
          });
        };

        const forwardAbort = (): void => {
          controller.abort();
        };

        const cleanup = (): void => {
          if (hardTimer !== null) {
            clock.clearTimeout(hardTimer);
            hardTimer = null;
          }
          if (startTimer !== null) {
            clock.clearTimeout(startTimer);
            startTimer = null;
          }
          request.signal.removeEventListener('abort', forwardAbort);
          signal.removeEventListener('abort', onAbort);
          active.delete(controller);
        };

        const settle = (response: AiMoveResponse): void => {
          if (settled) return;
          settled = true;
          cleanup();
          resolve(response);
        };

        if (disposed) {
          resolve({
            move: null,
            bestSoFar: null,
            degraded: true,
            reason: 'aborted',
            stats: { elapsedMs: 0 },
          });
          return;
        }

        if (request.signal.aborted) {
          onAbort();
          return;
        }

        request.signal.addEventListener('abort', forwardAbort);
        signal.addEventListener('abort', onAbort);
        active.add(controller);

        const seed = options.seed ?? request.seed;
        const effectiveRequest: AiMoveRequest = {
          ...request,
          seed,
          signal,
        };

        const budget = Math.max(0, Math.min(AI_MAX_SEARCH_BUDGET_MS, request.timeBudgetMs));

        // 先注册策略启动定时器，保证 0 预算时策略仍有机会先于硬超时返回。
        startTimer = clock.setTimeout(() => {
          startTimer = null;
          if (settled) return;

          let outcome: AiMoveResponse | Promise<AiMoveResponse>;
          try {
            outcome = strategy.choose(effectiveRequest);
          } catch (cause) {
            const move = fallbackMove(request, seed);
            console.warn('[CC][AI] strategy threw', { gameId: request.gameId, cause });
            settle({
              move,
              bestSoFar: move,
              degraded: true,
              reason: 'exception',
              stats: { elapsedMs: elapsed() },
            });
            return;
          }

          Promise.resolve(outcome).then(
            (response) => {
              if (settled) return;
              if (
                !response ||
                typeof response !== 'object' ||
                typeof (response as AiMoveResponse).degraded !== 'boolean'
              ) {
                const move = fallbackMove(request, seed);
                settle({
                  move,
                  bestSoFar: move,
                  degraded: true,
                  reason: 'no-output',
                  stats: { elapsedMs: elapsed() },
                });
                return;
              }
              settle(response);
            },
            (cause) => {
              if (settled) return;
              const move = fallbackMove(request, seed);
              console.warn('[CC][AI] strategy rejected', { gameId: request.gameId, cause });
              settle({
                move,
                bestSoFar: move,
                degraded: true,
                reason: 'exception',
                stats: { elapsedMs: elapsed() },
              });
            },
          );
        }, 0);

        hardTimer = clock.setTimeout(() => {
          hardTimer = null;
          const move = fallbackMove(request, seed);
          settle({
            move,
            bestSoFar: move,
            degraded: true,
            reason: 'timeout',
            stats: { elapsedMs: elapsed() },
          });
          // 结算后中止仍在进行的搜索（cleanup 已移除 abort 监听，不会覆盖 timeout）。
          controller.abort();
        }, budget);
      });
    },

    dispose(): void {
      disposed = true;
      for (const controller of Array.from(active)) controller.abort();
      active.clear();
    },
  };
}
