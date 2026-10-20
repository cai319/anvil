/**
 * DOM-free UI 状态机与 AI 调度（契约 §5）。
 *
 * 本文件不得引用 window/document/localStorage，可在 Node 环境直接导入与单测。
 */

import type {
  ActionEffect,
  ActionResult,
  AiDegradeReason,
  AiInfo,
  AiMoveRequest,
  AiMoveResponse,
  AppEvent,
  AppPhase,
  BoardSnapshot,
  Clock,
  CommitSource,
  ControllerDeps,
  ControllerListener,
  CubeCoord,
  Difficulty,
  GameController,
  GameResult,
  GameSnapshot,
  GameState,
  LegalMove,
  LegalMoveFilter,
  Piece,
  Player,
  Result,
  RulesApi,
  SelectionState,
  SetupConfig,
  TestStateInput,
  UiAppliedMove,
  Unsubscribe,
} from '../types/index.js';
import { DEFAULT_SETUP_CONFIG, err, ok } from '../types/index.js';
import { createInitialState, createStateFromFixture, getProgress } from '../engine/game.js';
import {
  applyMove as coreApplyMove,
  getLegalMoves as coreGetLegalMoves,
  isLegalMove as coreIsLegalMove,
  passTurn as corePassTurn,
  queryMoveOptions as coreQueryMoveOptions,
} from '../engine/rules.js';
import { createSeededRandom, deriveSeed, DEFAULT_CLOCK } from '../engine/random.js';
import { coordKey, getPieceAt, isCell, toCoord } from '../engine/board.js';
import {
  AI_HARD_LIMIT_MS,
  AI_MAX_SEARCH_BUDGET_MS,
  AI_MIN_DELAY_MS,
  AI_RESPONSE_DEADLINE_MS,
  AI_WATCHDOG_MS,
  AI_DEGRADE_AFTER,
} from '../ai/constants.js';

const RULES: RulesApi = {
  getLegalMoves: coreGetLegalMoves,
  queryMoveOptions: coreQueryMoveOptions,
  applyMove: coreApplyMove,
  passTurn: corePassTurn,
};

export interface ControllerTestApi {
  loadState(input: TestStateInput): Result<void>;
  getEngineState(): GameState | null;
  getLegalMoves(filter?: LegalMoveFilter): readonly LegalMove[];
}

export interface GameControllerWithTest extends GameController {
  readonly __test: ControllerTestApi;
}

function otherPlayer(player: Player): Player {
  return player === 'R' ? 'B' : 'R';
}

function humanColorOf(config: SetupConfig): Player {
  return config.humanSide === 'first' ? 'R' : 'B';
}

function isDifficulty(value: unknown): value is Difficulty {
  return value === 'easy' || value === 'normal' || value === 'hard';
}

function isHumanSide(value: unknown): value is 'first' | 'second' {
  return value === 'first' || value === 'second';
}

function copyCoord(c: CubeCoord): CubeCoord {
  return { x: c.x, y: c.y, z: c.z };
}

function copySelection(selection: SelectionState | null): SelectionState | null {
  if (!selection) return null;
  return {
    mode: selection.mode,
    origin: copyCoord(selection.origin),
    current: copyCoord(selection.current),
    path: selection.path.map(copyCoord),
    steps: selection.steps.map(copyCoord),
    jumps: selection.jumps.map(copyCoord),
    canEndTurn: selection.canEndTurn,
  };
}

function copyMove(move: UiAppliedMove | null): UiAppliedMove | null {
  if (!move) return null;
  return {
    ...move,
    piece: copyCoord(move.piece),
    path: move.path.map(copyCoord),
    to: copyCoord(move.to),
  };
}

function contextualError(
  code: Parameters<typeof err>[0],
  message: string,
): Result<never> {
  const base = err(code);
  if (base.ok) return base;
  return { ok: false, error: { ...base.error, message } };
}

interface PendingAi {
  readonly generation: number;
  readonly startedAt: number;
  readonly request: AiMoveRequest;
}

export function createController(deps: ControllerDeps): GameControllerWithTest {
  const clock: Clock = deps.clock ?? DEFAULT_CLOCK;
  const rules: RulesApi = deps.rules ?? RULES;
  const aiMinDelayMs = deps.aiMinDelayMs ?? AI_MIN_DELAY_MS;
  const aiResponseDeadlineMs = deps.aiResponseDeadlineMs ?? AI_RESPONSE_DEADLINE_MS;
  const aiWatchdogMs = deps.aiWatchdogMs ?? AI_WATCHDOG_MS;
  const aiHardLimitMs = deps.aiHardLimitMs ?? AI_HARD_LIMIT_MS;

  let config: SetupConfig = { ...DEFAULT_SETUP_CONFIG };
  let phase: AppPhase = 'SETUP';
  let gameId = 0;
  let state: GameState | null = null;
  let selection: SelectionState | null = null;
  let lastMove: UiAppliedMove | null = null;
  let result: GameResult | null = null;
  let forcedEasy = false;
  let degradationStreak = 0;
  let thinking: { startedAt: number; hardLimitMs: 2000 } | null = null;

  const listeners = new Set<ControllerListener>();
  const idleWaiters: Array<() => void> = [];
  let destroyed = false;

  let aiGeneration = 0;
  let aiAbort: AbortController | null = null;
  let aiMinTimer: number | null = null;
  let aiWatchdogTimer: number | null = null;
  let pending: PendingAi | null = null;

  // -------------------------------------------------------------------------
  // Snapshot
  // -------------------------------------------------------------------------

  const humanColor = (): Player => humanColorOf(config);
  const aiColor = (): Player => otherPlayer(humanColorOf(config));

  function snapshot(): GameSnapshot {
    const inSetup = phase === 'SETUP';
    const hColor = inSetup ? null : humanColor();
    const aColor = inSetup ? null : aiColor();

    let board: BoardSnapshot | null = null;
    if (state) {
      board = {
        pieces: state.pieces.map((p) => ({ color: p.color, pos: copyCoord(p.pos) })),
        selection: copySelection(selection),
        lastMove: copyMove(lastMove),
        progress: getProgress(state),
      };
    }

    let ai: AiInfo | null = null;
    if (!inSetup) {
      ai = {
        configuredDifficulty: config.difficulty,
        effectiveDifficulty: forcedEasy ? 'easy' : config.difficulty,
        forcedEasy,
        degradationStreak,
        thinking: phase === 'AI_THINKING' ? thinking : null,
      };
    }

    return {
      gameId,
      phase,
      config: { ...config },
      humanColor: hColor,
      aiColor: aColor,
      turn: inSetup ? null : state?.turn ?? null,
      ply: inSetup ? 0 : state?.ply ?? 0,
      board,
      ai,
      result: phase === 'GAME_OVER' ? result : null,
      canRestart: !inSetup,
      canBackToSetup: !inSetup,
    };
  }

  function emit(event: AppEvent): void {
    const snap = snapshot();
    for (const listener of Array.from(listeners)) {
      try {
        listener(snap, event);
      } catch (cause) {
        console.error('[CC][controller] listener error', cause);
      }
    }
  }

  function flushIdle(): void {
    if (phase === 'AI_THINKING' || pending !== null || aiMinTimer !== null || aiWatchdogTimer !== null) {
      return;
    }
    const waiters = idleWaiters.splice(0, idleWaiters.length);
    for (const resolve of waiters) resolve();
  }

  // -------------------------------------------------------------------------
  // Config helpers
  // -------------------------------------------------------------------------

  function mergeConfig(patch: Partial<SetupConfig>): SetupConfig | null {
    const next = { ...config, ...patch };
    if (!isDifficulty(next.difficulty) || !isHumanSide(next.humanSide)) return null;
    return next;
  }

  // -------------------------------------------------------------------------
  // AI lifecycle
  // -------------------------------------------------------------------------

  function clearAiTimers(): void {
    if (aiMinTimer !== null) {
      clock.clearTimeout(aiMinTimer);
      aiMinTimer = null;
    }
    if (aiWatchdogTimer !== null) {
      clock.clearTimeout(aiWatchdogTimer);
      aiWatchdogTimer = null;
    }
  }

  function invalidateAi(): void {
    aiGeneration += 1;
    if (aiAbort) {
      aiAbort.abort();
      aiAbort = null;
    }
    clearAiTimers();
    pending = null;
    thinking = null;
  }

  function recordDegradation(reason: AiDegradeReason): void {
    degradationStreak += 1;
    console.warn('[CC][AI] degraded', {
      gameId,
      ply: state?.ply ?? 0,
      configuredDifficulty: config.difficulty,
      effectiveDifficulty: forcedEasy ? 'easy' : config.difficulty,
      reason,
      streak: degradationStreak,
    });
    if (!forcedEasy && degradationStreak >= AI_DEGRADE_AFTER) {
      forcedEasy = true;
      emit({
        type: 'ai-degraded',
        reason,
        streak: degradationStreak,
        effectiveDifficulty: 'easy',
      });
    }
  }

  function seededRandom(): { next(): number; pick<T>(items: readonly T[]): T } {
    if (deps.random) {
      return {
        next: () => deps.random!.next(),
        pick: <T>(items: readonly T[]): T => items[Math.floor(deps.random!.next() * items.length)],
      };
    }
    const seed = deriveSeed(gameId, state?.ply ?? 0, state?.turn ?? 'R');
    return createSeededRandom(seed);
  }

  function finishGame(gameResult: GameResult): void {
    result = gameResult;
    phase = 'GAME_OVER';
    thinking = null;
    selection = null;
    pending = null;
    clearAiTimers();
    emit({ type: 'game-over', result: gameResult });
    flushIdle();
  }

  function settleTurn(): void {
    if (!state) return;

    // 自动轮空循环（人类与 AI 共用）。
    while (state.phase === 'PLAYING') {
      const legal = rules.getLegalMoves(state);
      if (legal.length > 0) break;
      const passed = rules.passTurn(state, { legalMoves: legal });
      if (!passed.ok) {
        emit({ type: 'error', error: passed.error });
        break;
      }
      state = passed.value.state;
      selection = null;
      emit({
        type: 'pass',
        player: passed.value.passedPlayer,
        reason: 'no-legal-moves',
      });
      if (passed.value.result) {
        finishGame(passed.value.result);
        return;
      }
    }

    if (state.phase === 'FINISHED') {
      if (state.result) finishGame(state.result);
      return;
    }

    if (state.turn === aiColor()) {
      phase = 'AI_THINKING';
      thinking = null;
      emit({ type: 'turn-changed', phase, turn: state.turn });
      startAi();
      return;
    }

    phase = 'HUMAN_TURN';
    thinking = null;
    emit({ type: 'turn-changed', phase, turn: state.turn });
    flushIdle();
  }

  function startAi(): void {
    if (!state || state.phase !== 'PLAYING' || phase !== 'AI_THINKING') return;
    // 新的 AI 请求：使旧代际失效。
    aiGeneration += 1;
    if (aiAbort) aiAbort.abort();
    clearAiTimers();
    pending = null;

    const generation = aiGeneration;
    const startedAt = clock.now();
    thinking = { startedAt, hardLimitMs: 2000 };

    const abort = new AbortController();
    aiAbort = abort;

    emit({ type: 'ai-thinking', startedAt });

    aiWatchdogTimer = clock.setTimeout(() => {
      aiWatchdogTimer = null;
      onWatchdog(generation);
    }, aiWatchdogMs);

    aiMinTimer = clock.setTimeout(() => {
      aiMinTimer = null;
      runAi(generation, startedAt, abort);
    }, aiMinDelayMs);
  }

  function runAi(generation: number, startedAt: number, abort: AbortController): void {
    if (!state || generation !== aiGeneration || phase !== 'AI_THINKING') return;

    const now = clock.now();
    const deadlineAt = startedAt + aiResponseDeadlineMs;
    const timeBudgetMs = Math.max(
      0,
      Math.min(AI_MAX_SEARCH_BUDGET_MS, deadlineAt - now),
    );

    const request: AiMoveRequest = {
      gameId,
      state,
      player: state.turn,
      configuredDifficulty: config.difficulty,
      effectiveDifficulty: forcedEasy ? 'easy' : config.difficulty,
      deadlineAt,
      timeBudgetMs,
      seed: deriveSeed(gameId, state.ply, state.turn),
      signal: abort.signal,
    };

    pending = { generation, startedAt, request };

    let settled = false;
    const onResponse = (response: AiMoveResponse): void => {
      if (settled) return;
      settled = true;
      handleAiResponse(generation, request, response);
    };
    const onFailure = (cause: unknown): void => {
      if (settled) return;
      settled = true;
      handleAiResponse(generation, request, {
        move: null,
        bestSoFar: null,
        degraded: true,
        reason: 'exception',
      });
      console.warn('[CC][AI] chooseMove rejected', { gameId, cause });
    };

    try {
      deps.ai.chooseMove(request).then(onResponse, onFailure);
    } catch (cause) {
      onFailure(cause);
    }
  }

  function clearRequest(): void {
    clearAiTimers();
    pending = null;
    thinking = null;
    if (aiAbort) {
      aiAbort = null;
    }
  }

  function applyAiMove(move: LegalMove): boolean {
    if (!state) return false;
    const applied = rules.applyMove(state, move);
    if (!applied.ok) return false;

    state = applied.value.state;
    const uiMove: UiAppliedMove = { ...applied.value.move, committedBy: 'ai' };
    lastMove = uiMove;
    selection = null;
    thinking = null;
    pending = null;
    clearAiTimers();

    const terminal = applied.value.result;
    if (terminal) {
      result = terminal;
      phase = 'GAME_OVER';
    }
    emit({ type: 'move-applied', move: uiMove, source: 'ai' });
    if (terminal) {
      emit({ type: 'game-over', result: terminal });
      flushIdle();
    } else {
      settleTurn();
    }
    return true;
  }

  function applyFallback(reason: AiDegradeReason): void {
    if (!state) return;
    // 使旧请求结果失效。
    aiGeneration += 1;
    if (aiAbort) {
      aiAbort.abort();
      aiAbort = null;
    }
    clearAiTimers();
    pending = null;
    thinking = null;

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const legal = rules.getLegalMoves(state);
      if (legal.length === 0) break;
      const rng = seededRandom();
      const move = legal[Math.min(legal.length - 1, Math.floor(rng.next() * legal.length))];
      if (applyAiMove(move)) return;
    }

    // 无合法着法或应用失败：轮空兜底，绝不卡死。
    const passed = rules.passTurn(state);
    if (passed.ok) {
      state = passed.value.state;
      selection = null;
      emit({ type: 'pass', player: passed.value.passedPlayer, reason: 'no-legal-moves' });
      if (passed.value.result) {
        finishGame(passed.value.result);
      } else {
        settleTurn();
      }
      return;
    }

    recordDegradation(reason);
    emit({ type: 'error', error: passed.error });
  }

  function handleAiResponse(
    generation: number,
    request: AiMoveRequest,
    response: AiMoveResponse,
  ): void {
    if (generation !== aiGeneration) return; // 过期结果
    if (!state || phase !== 'AI_THINKING') return;
    if (request.gameId !== gameId || request.player !== state.turn) return;

    // 已超过看门狗：按超时兜底。
    if (clock.now() > request.deadlineAt + aiWatchdogMs - aiResponseDeadlineMs) {
      recordDegradation('timeout');
      applyFallback('timeout');
      return;
    }

    const move = response.move;
    const legal = move !== null && coreIsLegalMove(state, move);

    if (legal && move) {
      if (response.degraded) {
        recordDegradation(response.reason ?? 'invalid-output');
      } else {
        degradationStreak = 0;
      }
      if (applyAiMove(move)) return;
      recordDegradation('exception');
      applyFallback('exception');
      return;
    }

    const reason: AiDegradeReason =
      move === null ? 'no-output' : 'invalid-output';
    recordDegradation(reason);

    // 兜底顺序：bestSoFar（合法）→ seeded 随机合法着法 → 轮空。
    const best = response.bestSoFar ?? null;
    if (best && coreIsLegalMove(state, best)) {
      if (applyAiMove(best)) return;
    }

    applyFallback(reason);
  }

  function onWatchdog(generation: number): void {
    if (generation !== aiGeneration || phase !== 'AI_THINKING' || !pending) return;
    recordDegradation('timeout');
    applyFallback('timeout');
  }

  // -------------------------------------------------------------------------
  // Human move commit
  // -------------------------------------------------------------------------

  function commitHumanMove(
    move: LegalMove,
    committedBy: CommitSource,
  ): Result<ActionEffect> {
    if (!state) return err('WRONG_PHASE');
    const applied = rules.applyMove(state, move);
    if (!applied.ok) return { ok: false, error: applied.error };

    state = applied.value.state;
    const uiMove: UiAppliedMove = { ...applied.value.move, committedBy };
    lastMove = uiMove;
    selection = null;

    const terminal = applied.value.result;
    if (terminal) {
      result = terminal;
      phase = 'GAME_OVER';
      thinking = null;
    }

    emit({ type: 'move-applied', move: uiMove, source: 'human' });

    if (terminal) {
      emit({ type: 'game-over', result: terminal });
      flushIdle();
    } else {
      settleTurn();
    }

    return ok({ type: 'move-committed', move: uiMove, source: 'human' });
  }

  function makeSelection(piece: CubeCoord): Result<SelectionState | null> {
    if (!state) return err('WRONG_PHASE');
    const options = rules.queryMoveOptions(state, piece);
    if (!options.ok) return { ok: false, error: options.error };
    if (options.value.steps.length === 0 && options.value.jumps.length === 0) {
      return contextualError('ILLEGAL_MOVE', '该棋子没有可走的路线');
    }
    return ok({
      mode: 'selected',
      origin: copyCoord(piece),
      current: copyCoord(piece),
      path: [],
      steps: options.value.steps.map(copyCoord),
      jumps: options.value.jumps.map(copyCoord),
      canEndTurn: false,
    });
  }

  function advanceJump(pos: CubeCoord): Result<ActionEffect> {
    if (!state || !selection) return err('ILLEGAL_MOVE');
    const newPath = [...selection.path.map(copyCoord), copyCoord(pos)];
    const options = rules.queryMoveOptions(state, selection.origin, newPath);
    if (!options.ok) return { ok: false, error: options.error };

    if (options.value.winningIfEndTurn) {
      return commitHumanMove(
        { piece: copyCoord(selection.origin), path: newPath, kind: 'jump' },
        'auto-end',
      );
    }

    if (options.value.jumps.length === 0) {
      return commitHumanMove(
        { piece: copyCoord(selection.origin), path: newPath, kind: 'jump' },
        'auto-end',
      );
    }

    selection = {
      mode: 'jumping',
      origin: copyCoord(selection.origin),
      current: copyCoord(pos),
      path: newPath,
      steps: [],
      jumps: options.value.jumps.map(copyCoord),
      canEndTurn: options.value.canEndTurn,
    };

    emit({ type: 'jump-step', selection: copySelection(selection)! });
    emit({ type: 'selection-changed', selection: copySelection(selection) });
    return ok({ type: 'jump-step', selection: copySelection(selection)! });
  }

  // -------------------------------------------------------------------------
  // Click dispatch（契约 §5.5）
  // -------------------------------------------------------------------------

  function handleCellClick(pos: CubeCoord): ActionResult {
    if (destroyed) return err('WRONG_PHASE');
    if (phase === 'SETUP') return err('WRONG_PHASE');
    if (phase === 'AI_THINKING') return err('AI_BUSY');
    if (phase === 'GAME_OVER') return err('GAME_OVER');
    if (!state || state.phase !== 'PLAYING') return err('GAME_OVER');
    if (!isCell(pos)) return contextualError('OUT_OF_BOARD', '不可落子');

    const hColor = humanColor();
    const sel = selection;

    // A. 无选中
    if (!sel) {
      const piece = getPieceAt(state, pos);
      if (!piece) return contextualError('EMPTY_CELL', '请选择一枚棋子');
      if (piece.color !== hColor) return contextualError('NOT_YOUR_PIECE', '请选择己方棋子');

      const made = makeSelection(pos);
      if (!made.ok) return { ok: false, error: made.error };
      if (!made.value) return contextualError('ILLEGAL_MOVE', '该棋子没有可走的路线');

      selection = made.value;
      emit({ type: 'selection-changed', selection: copySelection(selection) });
      return ok({ type: 'selected', piece: copyCoord(pos) });
    }

    // B. 选中态
    if (sel.mode === 'selected') {
      if (coordKey(pos) === coordKey(sel.origin)) {
        selection = null;
        emit({ type: 'selection-changed', selection: null });
        return ok({ type: 'deselected' });
      }

      if (sel.steps.some((c) => coordKey(c) === coordKey(pos))) {
        return commitHumanMove(
          { piece: copyCoord(sel.origin), path: [copyCoord(pos)], kind: 'step' },
          'step',
        );
      }

      if (sel.jumps.some((c) => coordKey(c) === coordKey(pos))) {
        return advanceJump(pos);
      }

      const piece = getPieceAt(state, pos);
      if (piece && piece.color === hColor) {
        const made = makeSelection(pos);
        if (!made.ok) {
          // 保持原选中。
          return { ok: false, error: made.error };
        }
        selection = made.value;
        emit({ type: 'selection-changed', selection: copySelection(selection) });
        return ok({ type: 'switched', piece: copyCoord(pos) });
      }

      return contextualError('ILLEGAL_MOVE', '不可落子');
    }

    // C. 连跳态
    if (coordKey(pos) === coordKey(sel.current)) {
      return ok({ type: 'noop' });
    }
    if (sel.jumps.some((c) => coordKey(c) === coordKey(pos))) {
      return advanceJump(pos);
    }

    return contextualError('ILLEGAL_MOVE', '连跳中只能继续跳跃或结束回合');
  }

  function endTurn(): ActionResult {
    if (destroyed) return err('WRONG_PHASE');
    if (phase !== 'HUMAN_TURN') {
      if (phase === 'AI_THINKING') return err('AI_BUSY');
      if (phase === 'GAME_OVER') return err('GAME_OVER');
      return err('WRONG_PHASE');
    }
    if (!state || !selection || selection.mode !== 'jumping') {
      return contextualError('ILLEGAL_MOVE', '当前没有可结束的连跳');
    }

    const options = rules.queryMoveOptions(state, selection.origin, selection.path);
    if (!options.ok || !options.value.canEndTurn) {
      return contextualError('ILLEGAL_MOVE', '不可落子');
    }

    return commitHumanMove(
      {
        piece: copyCoord(selection.origin),
        path: selection.path.map(copyCoord),
        kind: 'jump',
      },
      'end-turn',
    );
  }

  // -------------------------------------------------------------------------
  // Match lifecycle
  // -------------------------------------------------------------------------

  function resetMatch(): void {
    gameId += 1;
    state = createInitialState({ gameId });
    selection = null;
    lastMove = null;
    result = null;
    forcedEasy = false;
    degradationStreak = 0;
    thinking = null;
  }

  function beginTurn(): void {
    if (!state) return;
    if (state.phase === 'FINISHED') {
      if (state.result) finishGame(state.result);
      return;
    }
    settleTurn();
  }

  function setSetupConfig(patch: Partial<SetupConfig>): Result<SetupConfig> {
    if (phase !== 'SETUP') return err('WRONG_PHASE');
    const next = mergeConfig(patch);
    if (!next) return err('INVALID_CONFIG');
    config = next;
    emit({ type: 'setup-config-changed', config: { ...config } });
    return ok({ ...config });
  }

  function startGame(patch?: Partial<SetupConfig>): Result<GameSnapshot> {
    if (phase !== 'SETUP') return err('WRONG_PHASE');
    if (patch) {
      const next = mergeConfig(patch);
      if (!next) return err('INVALID_CONFIG');
      config = next;
    }

    resetMatch();
    // 先确定 phase，保证 match-start 的快照已是新对局状态（避免 UI 闪回 SETUP）。
    phase = state!.turn === aiColor() ? 'AI_THINKING' : 'HUMAN_TURN';
    emit({ type: 'match-start' });
    beginTurn();
    return ok(snapshot());
  }

  function restart(): Result<GameSnapshot> {
    if (phase === 'SETUP') return err('WRONG_PHASE');
    invalidateAi();
    resetMatch();
    phase = state!.turn === aiColor() ? 'AI_THINKING' : 'HUMAN_TURN';
    emit({ type: 'restart' });
    beginTurn();
    return ok(snapshot());
  }

  function backToSetup(): void {
    if (phase === 'SETUP') return;
    invalidateAi();
    gameId += 1;
    state = null;
    selection = null;
    lastMove = null;
    result = null;
    forcedEasy = false;
    degradationStreak = 0;
    thinking = null;
    phase = 'SETUP';
    emit({ type: 'back-to-setup' });
    flushIdle();
  }

  function checkDeadlines(): void {
    if (phase !== 'AI_THINKING' || !pending) return;
    const hardAt = pending.startedAt + aiWatchdogMs;
    if (clock.now() >= hardAt) {
      const generation = pending.generation;
      onWatchdog(generation);
    }
  }

  function waitForIdle(): Promise<void> {
    if (destroyed) return Promise.resolve();
    if (phase !== 'AI_THINKING' && pending === null && aiMinTimer === null && aiWatchdogTimer === null) {
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      idleWaiters.push(resolve);
    });
  }

  function destroy(): void {
    if (destroyed) return;
    destroyed = true;
    invalidateAi();
    listeners.clear();
    const waiters = idleWaiters.splice(0, idleWaiters.length);
    for (const resolve of waiters) resolve();
    try {
      deps.ai.dispose();
    } catch (cause) {
      console.warn('[CC][controller] ai.dispose failed', cause);
    }
  }

  // -------------------------------------------------------------------------
  // Test hook API（仅 ?e2e=1 时由 app.ts 暴露）
  // -------------------------------------------------------------------------

  function loadState(input: TestStateInput): Result<void> {
    try {
      if (typeof input !== 'object' || input === null) return err('INVALID_FIXTURE');
      if (!isDifficulty(input.config?.difficulty) || !isHumanSide(input.config?.humanSide)) {
        return err('INVALID_FIXTURE', 'config 非法');
      }
      if (input.turn !== 'R' && input.turn !== 'B') return err('INVALID_FIXTURE', 'turn 非法');
      if (!Number.isInteger(input.ply) || input.ply < 0 || input.ply > 299) {
        return err('INVALID_FIXTURE', 'ply 非法');
      }
      if (
        input.phase !== 'HUMAN_TURN' &&
        input.phase !== 'AI_THINKING' &&
        input.phase !== 'GAME_OVER'
      ) {
        return err('INVALID_FIXTURE', 'phase 非法');
      }

      if (input.phase === 'GAME_OVER') {
        if (!input.result) return err('INVALID_FIXTURE', 'GAME_OVER 必须提供 result');
      } else if (input.result) {
        return err('INVALID_FIXTURE', '进行中局面不得提供 result');
      }

      const nextHuman = input.config.humanSide === 'first' ? 'R' : 'B';
      if (input.phase === 'HUMAN_TURN' && input.turn !== nextHuman) {
        return err('INVALID_FIXTURE', 'HUMAN_TURN 的 turn 必须为人类颜色');
      }
      if (input.phase === 'AI_THINKING' && input.turn === nextHuman) {
        return err('INVALID_FIXTURE', 'AI_THINKING 的 turn 必须为 AI 颜色');
      }
      if (input.selection && input.phase !== 'HUMAN_TURN') {
        return err('INVALID_FIXTURE', 'selection 仅允许 HUMAN_TURN');
      }
      const passes = input.consecutivePasses ?? 0;
      if (passes !== 0 && passes !== 1 && passes !== 2) {
        return err('INVALID_FIXTURE', 'consecutivePasses 非法');
      }
      if (passes === 2 && input.phase !== 'GAME_OVER') {
        return err('INVALID_FIXTURE', 'consecutivePasses=2 仅允许终局');
      }

      // 棋子校验（所有 phase 共用）：20 子、10 R + 10 B、合法且唯一。
      if (!Array.isArray(input.pieces) || input.pieces.length !== 20) {
        return err('INVALID_FIXTURE', 'pieces 必须恰好 20 项');
      }
      let redCount = 0;
      let blueCount = 0;
      const seen = new Set<string>();
      const normalizedPieces: Piece[] = [];
      for (const piece of input.pieces) {
        if (piece.color !== 'R' && piece.color !== 'B') {
          return err('INVALID_FIXTURE', '棋子颜色非法');
        }
        let pos: CubeCoord;
        try {
          pos = toCoord(piece.pos);
        } catch (cause) {
          return err('INVALID_FIXTURE', '棋子坐标非法', cause);
        }
        if (!isCell(pos)) return err('INVALID_FIXTURE', `棋子坐标越界: ${coordKey(pos)}`);
        const key = coordKey(pos);
        if (seen.has(key)) return err('INVALID_FIXTURE', `棋子坐标重复: ${key}`);
        seen.add(key);
        if (piece.color === 'R') redCount += 1;
        else blueCount += 1;
        normalizedPieces.push({ color: piece.color, pos });
      }
      if (redCount !== 10 || blueCount !== 10) {
        return err(
          'INVALID_FIXTURE',
          `棋子必须为 10 R + 10 B（实际 ${redCount} R / ${blueCount} B）`,
        );
      }

      const nextGameId =
        Number.isInteger(input.gameId) && (input.gameId ?? 0) > 0
          ? (input.gameId as number)
          : gameId + 1;

      const rawLast = input.lastMove ?? null;
      const engineLast = rawLast
        ? {
            player: rawLast.player,
            piece: rawLast.piece,
            path: rawLast.path,
            kind: rawLast.kind,
            to: rawLast.to,
            plyBefore: rawLast.plyBefore,
            plyAfter: rawLast.plyAfter,
          }
        : null;

      let nextState: GameState;
      if (input.phase === 'GAME_OVER') {
        // 终局展示：跳过胜负一致性校验，直接构造。
        nextState = {
          gameId: nextGameId,
          phase: 'FINISHED',
          turn: input.turn,
          ply: input.ply,
          pieces: normalizedPieces,
          consecutivePasses: passes as 0 | 1 | 2,
          result: input.result!,
          lastMove: engineLast,
        };
      } else {
        const created = createStateFromFixture(
          { turn: input.turn, ply: input.ply, pieces: normalizedPieces },
          {
            gameId: nextGameId,
            consecutivePasses: passes === 1 ? 1 : 0,
            lastMove: engineLast,
          },
        );
        if (!created.ok) return { ok: false, error: created.error };
        nextState = created.value;
      }

      invalidateAi();
      config = { ...input.config };
      gameId = nextGameId;
      state = nextState;
      lastMove = input.lastMove ? { ...input.lastMove } : null;
      result = input.phase === 'GAME_OVER' ? input.result ?? null : null;
      forcedEasy = false;
      degradationStreak = 0;
      thinking = null;
      selection = null;

      if (input.phase === 'GAME_OVER') {
        phase = 'GAME_OVER';
        emit({ type: 'match-start' });
        emit({ type: 'turn-changed', phase: 'GAME_OVER', turn: input.turn });
        if (result) emit({ type: 'game-over', result });
        flushIdle();
        return ok(undefined);
      }

      // selection 可恢复为选中/连跳草稿（必须与引擎局面一致）。
      if (input.selection) {
        if (input.selection.mode !== 'selected' && input.selection.mode !== 'jumping') {
          return err('INVALID_FIXTURE', 'selection.mode 非法');
        }
        if (input.selection.mode === 'jumping') {
          if (input.selection.path.length === 0) {
            return err('INVALID_FIXTURE', 'jumping selection 必须提供 path');
          }
          const last = input.selection.path[input.selection.path.length - 1];
          if (coordKey(last) !== coordKey(input.selection.current)) {
            return err('INVALID_FIXTURE', 'selection.current 必须等于 path 最后落点');
          }
        }
        const origin = input.selection.origin;
        const occupant = getPieceAt(nextState, origin);
        if (!occupant || occupant.color !== nextHuman) {
          return err('INVALID_FIXTURE', 'selection.origin 必须是人类棋子');
        }
        const options = rules.queryMoveOptions(
          nextState,
          origin,
          input.selection.mode === 'jumping' ? input.selection.path : [],
        );
        if (!options.ok) return { ok: false, error: options.error };
        selection =
          input.selection.mode === 'jumping'
            ? {
                mode: 'jumping',
                origin: copyCoord(origin),
                current: copyCoord(input.selection.current),
                path: input.selection.path.map(copyCoord),
                steps: [],
                jumps: options.value.jumps.map(copyCoord),
                canEndTurn: options.value.canEndTurn,
              }
            : {
                mode: 'selected',
                origin: copyCoord(origin),
                current: copyCoord(origin),
                path: [],
                steps: options.value.steps.map(copyCoord),
                jumps: options.value.jumps.map(copyCoord),
                canEndTurn: false,
              };
      }

      phase = input.phase;
      emit({ type: 'match-start' });
      // 进行中局面按正常流程结算（可能自动轮空/AI 首手）。
      beginTurn();
      return ok(undefined);
    } catch (cause) {
      return err('INVALID_FIXTURE', 'loadState 异常', cause);
    }
  }

  return {
    getSnapshot: snapshot,
    subscribe(listener: ControllerListener): Unsubscribe {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    setSetupConfig,
    startGame,
    restart,
    backToSetup,
    handleCellClick,
    endTurn,
    checkDeadlines,
    waitForIdle,
    destroy,
    __test: {
      loadState,
      getEngineState: () => state,
      getLegalMoves: (filter?: LegalMoveFilter) =>
        state ? rules.getLegalMoves(state, filter) : [],
    },
  };
}
