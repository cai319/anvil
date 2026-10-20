/**
 * 可注入的确定性随机源（mulberry32），供控制器与 AI 共用（契约 §1.3 / §6.7）。
 *
 * 禁止在 AI / 控制器兜底路径使用 Math.random()。
 */

import type { Player } from '../types/index.js';

export interface SeededRandom {
  /** [0, 1) */
  next(): number;
  /** uint32 */
  nextUint32(): number;
  pick<T>(items: readonly T[]): T;
}

/** mulberry32：同一种子产生同一序列。 */
export function createSeededRandom(seed: number): SeededRandom {
  let state = seed >>> 0;

  const nextUint32 = (): number => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return (t ^ (t >>> 14)) >>> 0;
  };

  const next = (): number => nextUint32() / 4294967296;

  const pick = <T>(items: readonly T[]): T => {
    if (items.length === 0) {
      throw new RangeError('pick 不能用于空数组');
    }
    return items[Math.floor(next() * items.length)];
  };

  return { next, nextUint32, pick };
}

/** 契约 §1.3 的种子派生。 */
export function deriveSeed(gameId: number, ply: number, player: Player): number {
  const p = player === 'R' ? 0x9e3779b1 : 0x85ebca77;
  return (Math.imul(gameId, 0x9e3779b1) ^ Math.imul(ply + 1, 0x85ebca77) ^ p) >>> 0;
}

/** 默认系统时钟（Node 与浏览器通用）。 */
export const DEFAULT_CLOCK = {
  now(): number {
    return typeof performance !== 'undefined' && typeof performance.now === 'function'
      ? performance.now()
      : Date.now();
  },
  setTimeout(handler: () => void, ms: number): number {
    return setTimeout(handler, ms) as unknown as number;
  },
  clearTimeout(handle: number): void {
    clearTimeout(handle as unknown as ReturnType<typeof setTimeout>);
  },
} as const;
