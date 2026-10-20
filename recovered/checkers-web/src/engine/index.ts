/**
 * 规则引擎公开入口。
 *
 * 契约中的逻辑分层名为 `src/core/*`；本工作流 S3 的物理 scope 为
 * `src/engine/**` 与 `src/types/**`，公开名称与签名保持与契约一致。
 */

export * from '../types/index.js';
export * from './board.js';
export * from './rules.js';
export * from './game.js';
export * from './random.js';
