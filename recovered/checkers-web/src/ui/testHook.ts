/**
 * E2E 测试钩子（契约 §8）：仅当 URL 显式包含 `?e2e=1` 时安装。
 * 生产环境不创建 window.__CC_TEST__。
 */

import type {
  AiTestMode,
  AiTestOptions,
  CcTestHook,
  GameController,
  TestSnapshot,
  TestStateInput,
} from '../types/index.js';
import { DEFAULT_SETUP_CONFIG } from '../types/index.js';
import type { GameControllerWithTest } from './controller.js';

/** 由 app.ts 创建、供测试钩子调整 AI 模式的控制面。 */
export interface TestControl {
  mode: AiTestMode;
  options: AiTestOptions;
  armNoLegalMoves(): void;
  reset(): void;
}

function asTestable(controller: GameController): GameControllerWithTest | null {
  const candidate = controller as Partial<GameControllerWithTest>;
  return candidate.__test ? (controller as GameControllerWithTest) : null;
}

export function installTestHook(
  controller: GameController,
  enabled: boolean,
  control?: TestControl,
): void {
  if (!enabled || typeof window === 'undefined') return;
  const testable = asTestable(controller);
  if (!testable) {
    console.warn('[CC][testHook] controller 未提供测试接口');
    return;
  }

  const snapshotToTest = (): TestSnapshot => {
    const snap = controller.getSnapshot();
    return {
      gameId: snap.gameId,
      phase: snap.phase,
      turn: snap.turn,
      ply: snap.ply,
      humanColor: snap.humanColor,
      aiColor: snap.aiColor,
      config: { ...snap.config },
      result: snap.result,
      pieces: snap.board ? snap.board.pieces.map((p) => ({ color: p.color, pos: { ...p.pos } })) : [],
      selection: snap.board?.selection ?? null,
      lastMove: snap.board?.lastMove ?? null,
      progress: snap.board?.progress ?? { R: 0, B: 0 },
      ai: snap.ai,
    };
  };

  const hook: CcTestHook = {
    version: '1.0',
    loadState(input: TestStateInput) {
      return testable.__test.loadState(input);
    },
    getState: snapshotToTest,
    getLegalMoves(piece) {
      return testable.__test.getLegalMoves(piece ? { piece } : undefined);
    },
    getSelection() {
      return controller.getSnapshot().board?.selection ?? null;
    },
    setAiMode(mode: AiTestMode, options?: AiTestOptions) {
      if (!control) return;
      if (mode === 'no-legal-moves') {
        control.mode = 'no-legal-moves';
        control.options = options ?? {};
        control.armNoLegalMoves();
        return;
      }
      control.mode = mode;
      control.options = options ?? {};
    },
    setSetupConfig(config) {
      return controller.setSetupConfig(config);
    },
    waitForIdle() {
      return controller.waitForIdle();
    },
    reset() {
      if (controller.getSnapshot().phase !== 'SETUP') controller.backToSetup();
      const applied = controller.setSetupConfig(DEFAULT_SETUP_CONFIG);
      control?.reset();
      if (!applied.ok) {
        // 理论上不可达；保证 reset 不抛出。
        console.warn('[CC][testHook] reset config failed', applied.error.code);
      }
    },
  };

  window.__CC_TEST__ = hook;
}
