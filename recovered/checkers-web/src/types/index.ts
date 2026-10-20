/**
 * 中国跳棋（2 人 Web 版）核心类型与错误模型。
 *
 * 本文件是 `docs/interface-contract.md` §2/§3/§5/§6/§8 的 TypeScript 落地，
 * 只包含类型与错误构造，不依赖 DOM / 浏览器 API / Node API，可在任意环境导入。
 *
 * 说明：S3 工作流的物理目录为 `src/types/**` 与 `src/engine/**`（契约 §0.1 中的
 * `src/core/**` 为逻辑分层名）。公开名称与签名严格保持与契约一致。
 */

// ---------------------------------------------------------------------------
// §1.2 阵营 / §1.4 结果
// ---------------------------------------------------------------------------

export type Player = 'R' | 'B';

export type Result<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: EngineError };

export function ok<T>(value: T): Result<T> {
  return { ok: true, value };
}

// ---------------------------------------------------------------------------
// §2.1 坐标、棋子、棋盘
// ---------------------------------------------------------------------------

export interface CubeCoord {
  readonly x: number;
  readonly y: number;
  readonly z: number;
}

export type CellKey = string;
export type CoordTuple = readonly [number, number, number];

export type CampId = 'C+' | 'C-' | 'C+x' | 'C-x' | 'C+z' | 'C-z';

export interface Piece {
  readonly color: Player;
  readonly pos: CubeCoord;
}

export interface BoardDefinition {
  /** 121 个孔位，规范顺序：y 从 +8 到 −8，行内 x 递增。 */
  readonly cells: readonly CubeCoord[];
  /** 中心六边形 61 孔。 */
  readonly centerCells: readonly CubeCoord[];
  /** 6 个营地，每个恰好 10 孔。 */
  readonly camps: Readonly<Record<CampId, readonly CubeCoord[]>>;
  /** 目标营：R → 'C+'（上营），B → 'C-'（下营）。 */
  readonly targetCamp: Readonly<Record<Player, CampId>>;
  /** D0..D5，顺序与 PRD §4.1.4 一致。 */
  readonly directions: readonly CubeCoord[];
  /** 初始 20 子：10 R 占 C-，10 B 占 C+。 */
  readonly initialPieces: readonly Piece[];
}

// ---------------------------------------------------------------------------
// §2.2 规则状态
// ---------------------------------------------------------------------------

export type MoveKind = 'step' | 'jump';

export interface LegalMove {
  /** 走子前棋子的孔位（origin）。 */
  readonly piece: CubeCoord;
  /** 不含 origin；长度 ≥1。step 时长度=1；jump 时长度≥1。 */
  readonly path: readonly CubeCoord[];
  readonly kind: MoveKind;
}

export type GamePhase = 'PLAYING' | 'FINISHED';

export type GameResult =
  | { readonly winner: Player; readonly reason: 'goal' }
  | { readonly winner: 'draw'; readonly reason: 'both-pass' | 'ply-limit' };

export interface AppliedMove {
  readonly player: Player;
  readonly piece: CubeCoord;
  readonly path: readonly CubeCoord[];
  readonly kind: MoveKind;
  readonly to: CubeCoord;
  readonly plyBefore: number;
  readonly plyAfter: number;
}

export interface CampProgress {
  readonly R: number;
  readonly B: number;
}

export interface GameState {
  readonly gameId: number;
  readonly phase: GamePhase;
  readonly turn: Player;
  readonly ply: number;
  readonly pieces: readonly Piece[];
  readonly consecutivePasses: 0 | 1 | 2;
  readonly result: GameResult | null;
  readonly lastMove: AppliedMove | null;
}

// ---------------------------------------------------------------------------
// §2.3 夹具与坐标工具
// ---------------------------------------------------------------------------

export interface FixturePieceInput {
  readonly color: Player;
  readonly pos: CubeCoord | CoordTuple;
}

export interface FixtureState {
  readonly turn: Player;
  readonly ply: number;
  readonly pieces: readonly FixturePieceInput[];
}

export interface CreateStateFromFixtureOptions {
  readonly gameId: number;
  readonly consecutivePasses?: 0 | 1;
  readonly lastMove?: AppliedMove | null;
}

// ---------------------------------------------------------------------------
// §3 错误模型
// ---------------------------------------------------------------------------

export type EngineErrorCode =
  // 坐标/棋子/回合
  | 'OUT_OF_BOARD'
  | 'EMPTY_CELL'
  | 'NOT_YOUR_PIECE'
  | 'NOT_YOUR_TURN'
  | 'GAME_OVER'
  | 'WRONG_PHASE'
  // 走子规则
  | 'NOT_ADJACENT'
  | 'OCCUPIED'
  | 'JUMP_NO_PIECE'
  | 'JUMP_LANDING_OCCUPIED'
  | 'PATH_REPEAT'
  | 'PATH_BROKEN'
  | 'MIXED_MOVE'
  | 'INCOMPLETE_PATH'
  | 'ILLEGAL_MOVE'
  | 'PATH_BEYOND_WIN'
  | 'PASS_NOT_ALLOWED'
  // 并发/调用
  | 'STALE_GAME'
  | 'AI_BUSY'
  // 配置/数据
  | 'INVALID_CONFIG'
  | 'INVALID_STATE'
  | 'INVALID_FIXTURE'
  // AI 降级
  | 'AI_TIMEOUT'
  | 'AI_EXCEPTION'
  | 'AI_ILLEGAL_MOVE'
  | 'AI_NO_OUTPUT'
  | 'AI_ABORTED'
  | 'UNKNOWN';

export type ErrorDisplay = 'toast' | 'silent';

export interface EngineError {
  readonly code: EngineErrorCode;
  readonly message: string;
  readonly display: ErrorDisplay;
  readonly detail?: string;
  readonly cause?: unknown;
}

interface ErrorMeta {
  readonly message: string;
  readonly display: ErrorDisplay;
}

const ERROR_META: Readonly<Record<EngineErrorCode, ErrorMeta>> = {
  OUT_OF_BOARD: { message: '目标不在棋盘内', display: 'toast' },
  EMPTY_CELL: { message: '该孔位没有棋子', display: 'silent' },
  NOT_YOUR_PIECE: { message: '请选择己方棋子', display: 'toast' },
  NOT_YOUR_TURN: { message: '还没轮到你', display: 'silent' },
  GAME_OVER: { message: '对局已结束', display: 'silent' },
  WRONG_PHASE: { message: '当前阶段不可操作', display: 'silent' },
  NOT_ADJACENT: { message: '只能移动到相邻孔位', display: 'toast' },
  OCCUPIED: { message: '目标孔位已被占用', display: 'toast' },
  JUMP_NO_PIECE: { message: '跳跃中点没有棋子', display: 'toast' },
  JUMP_LANDING_OCCUPIED: { message: '跳跃落点已被占用', display: 'toast' },
  PATH_REPEAT: { message: '连跳不能回到走过的孔位', display: 'toast' },
  PATH_BROKEN: { message: '连跳路径不连续', display: 'toast' },
  MIXED_MOVE: { message: '跳跃后不能改为单步', display: 'toast' },
  INCOMPLETE_PATH: { message: '连跳路径未完成', display: 'toast' },
  ILLEGAL_MOVE: { message: '该走法不合法', display: 'toast' },
  PATH_BEYOND_WIN: { message: '该连跳在获胜后仍有后续', display: 'silent' },
  PASS_NOT_ALLOWED: { message: '有合法着法时不能轮空', display: 'silent' },
  STALE_GAME: { message: '对局已更新', display: 'silent' },
  AI_BUSY: { message: 'AI 正在思考', display: 'silent' },
  INVALID_CONFIG: { message: '设置无效，请重新选择', display: 'toast' },
  INVALID_STATE: { message: '对局状态异常，请重新开始', display: 'toast' },
  INVALID_FIXTURE: { message: '测试局面数据非法', display: 'toast' },
  AI_TIMEOUT: { message: 'AI 思考超时', display: 'silent' },
  AI_EXCEPTION: { message: 'AI 计算异常', display: 'silent' },
  AI_ILLEGAL_MOVE: { message: 'AI 返回非法着法', display: 'silent' },
  AI_NO_OUTPUT: { message: 'AI 未返回着法', display: 'silent' },
  AI_ABORTED: { message: 'AI 计算已取消', display: 'silent' },
  UNKNOWN: { message: '未知错误', display: 'toast' },
};

export function err(
  code: EngineErrorCode,
  detail?: string,
  cause?: unknown,
): Result<never> {
  const meta = ERROR_META[code] ?? ERROR_META.UNKNOWN;
  const error: EngineError = {
    code,
    message: meta.message,
    display: meta.display,
    ...(detail !== undefined ? { detail } : {}),
    ...(cause !== undefined ? { cause } : {}),
  };
  return { ok: false, error };
}

// ---------------------------------------------------------------------------
// §5.1 设置与阶段
// ---------------------------------------------------------------------------

export type Difficulty = 'easy' | 'normal' | 'hard';
export type HumanSide = 'first' | 'second';
export type AppPhase = 'SETUP' | 'HUMAN_TURN' | 'AI_THINKING' | 'GAME_OVER';

export interface SetupConfig {
  readonly difficulty: Difficulty;
  readonly humanSide: HumanSide;
}

export const DEFAULT_SETUP_CONFIG: SetupConfig = {
  difficulty: 'normal',
  humanSide: 'first',
};

// ---------------------------------------------------------------------------
// §5.2 快照类型
// ---------------------------------------------------------------------------

export interface SelectionState {
  readonly mode: 'selected' | 'jumping';
  readonly origin: CubeCoord;
  readonly current: CubeCoord;
  readonly path: readonly CubeCoord[];
  readonly steps: readonly CubeCoord[];
  readonly jumps: readonly CubeCoord[];
  readonly canEndTurn: boolean;
}

export type CommitSource = 'step' | 'auto-end' | 'end-turn' | 'ai';

export interface UiAppliedMove extends AppliedMove {
  readonly committedBy: CommitSource;
}

export interface BoardSnapshot {
  readonly pieces: readonly Piece[];
  readonly selection: SelectionState | null;
  readonly lastMove: UiAppliedMove | null;
  readonly progress: CampProgress;
}

export interface AiThinkingInfo {
  readonly startedAt: number;
  readonly hardLimitMs: 2000;
}

export interface AiInfo {
  readonly configuredDifficulty: Difficulty;
  readonly effectiveDifficulty: Difficulty;
  readonly forcedEasy: boolean;
  readonly degradationStreak: number;
  readonly thinking: AiThinkingInfo | null;
}

export interface GameSnapshot {
  readonly gameId: number;
  readonly phase: AppPhase;
  readonly config: SetupConfig;
  readonly humanColor: Player | null;
  readonly aiColor: Player | null;
  readonly turn: Player | null;
  readonly ply: number;
  readonly board: BoardSnapshot | null;
  readonly ai: AiInfo | null;
  readonly result: GameResult | null;
  readonly canRestart: boolean;
  readonly canBackToSetup: boolean;
}

// ---------------------------------------------------------------------------
// §5.3 控制器接口
// ---------------------------------------------------------------------------

export type Unsubscribe = () => void;
export type ControllerListener = (snapshot: GameSnapshot, event: AppEvent) => void;

export type ActionEffect =
  | { readonly type: 'selected'; readonly piece: CubeCoord }
  | { readonly type: 'deselected' }
  | { readonly type: 'switched'; readonly piece: CubeCoord }
  | { readonly type: 'jump-step'; readonly selection: SelectionState }
  | {
      readonly type: 'move-committed';
      readonly move: UiAppliedMove;
      readonly source: 'human' | 'ai';
    }
  | { readonly type: 'noop' };

export type ActionResult = Result<ActionEffect>;

export interface GameController {
  getSnapshot(): GameSnapshot;
  subscribe(listener: ControllerListener): Unsubscribe;
  setSetupConfig(patch: Partial<SetupConfig>): Result<SetupConfig>;
  startGame(config?: Partial<SetupConfig>): Result<GameSnapshot>;
  restart(): Result<GameSnapshot>;
  backToSetup(): void;
  handleCellClick(pos: CubeCoord): ActionResult;
  endTurn(): ActionResult;
  checkDeadlines(): void;
  waitForIdle(): Promise<void>;
  destroy(): void;
}

export interface LegalMoveFilter {
  readonly player?: Player;
  readonly piece?: CubeCoord;
}

export interface MoveQueryResult {
  readonly piece: CubeCoord;
  readonly pathSoFar: readonly CubeCoord[];
  readonly phase: 'select' | 'jump';
  readonly steps: readonly CubeCoord[];
  readonly jumps: readonly CubeCoord[];
  readonly canEndTurn: boolean;
  readonly winningIfEndTurn: boolean;
}

export interface MoveOutcome {
  readonly state: GameState;
  readonly move: AppliedMove;
  readonly result: GameResult | null;
}

export interface PassOutcome {
  readonly state: GameState;
  readonly passedPlayer: Player;
  readonly result: GameResult | null;
  readonly passed: true;
}

export interface RulesApi {
  getLegalMoves(state: GameState, filter?: LegalMoveFilter): readonly LegalMove[];
  queryMoveOptions(
    state: GameState,
    piece: CubeCoord,
    pathSoFar?: readonly CubeCoord[],
  ): Result<MoveQueryResult>;
  applyMove(state: GameState, move: LegalMove): Result<MoveOutcome>;
  passTurn(
    state: GameState,
    options?: { readonly legalMoves?: readonly LegalMove[] },
  ): Result<PassOutcome>;
}

export interface Clock {
  now(): number;
  setTimeout(handler: () => void, ms: number): number;
  clearTimeout(handle: number): void;
}

export interface RandomSource {
  next(): number;
}

export interface ControllerDeps {
  readonly ai: AiClient;
  readonly clock?: Clock;
  readonly random?: RandomSource;
  readonly rules?: RulesApi;
  readonly aiMinDelayMs?: number;
  readonly aiResponseDeadlineMs?: number;
  readonly aiWatchdogMs?: number;
  readonly aiHardLimitMs?: number;
}

// ---------------------------------------------------------------------------
// §5.7 事件
// ---------------------------------------------------------------------------

export type AiDegradeReason =
  | 'timeout'
  | 'exception'
  | 'invalid-output'
  | 'no-output'
  | 'no-legal-moves'
  | 'aborted';

export interface Hint {
  readonly code: string;
  readonly message: string;
  readonly tone: 'info' | 'warn' | 'error';
  readonly durationMs: number;
  readonly sticky: boolean;
}

export type AppEvent =
  | { readonly type: 'setup-config-changed'; readonly config: SetupConfig }
  | { readonly type: 'match-start' }
  | { readonly type: 'selection-changed'; readonly selection: SelectionState | null }
  | { readonly type: 'jump-step'; readonly selection: SelectionState }
  | {
      readonly type: 'move-applied';
      readonly move: UiAppliedMove;
      readonly source: 'human' | 'ai';
    }
  | { readonly type: 'turn-changed'; readonly phase: AppPhase; readonly turn: Player | null }
  | { readonly type: 'pass'; readonly player: Player; readonly reason: 'no-legal-moves' }
  | { readonly type: 'ai-thinking'; readonly startedAt: number }
  | {
      readonly type: 'ai-degraded';
      readonly reason: AiDegradeReason;
      readonly streak: number;
      readonly effectiveDifficulty: Difficulty;
    }
  | { readonly type: 'game-over'; readonly result: GameResult }
  | { readonly type: 'restart' }
  | { readonly type: 'back-to-setup' }
  | { readonly type: 'hint'; readonly hint: Hint }
  | { readonly type: 'error'; readonly error: EngineError };

// ---------------------------------------------------------------------------
// §6 AI 边界
// ---------------------------------------------------------------------------

export interface AiStats {
  readonly nodes?: number;
  readonly depth?: number;
  readonly elapsedMs: number;
}

export interface AiMoveRequest {
  readonly gameId: number;
  readonly state: GameState;
  readonly player: Player;
  readonly configuredDifficulty: Difficulty;
  readonly effectiveDifficulty: Difficulty;
  readonly deadlineAt: number;
  readonly timeBudgetMs: number;
  readonly seed: number;
  readonly signal: AbortSignal;
}

export interface AiMoveResponse {
  readonly move: LegalMove | null;
  readonly bestSoFar?: LegalMove | null;
  readonly degraded: boolean;
  readonly reason?: AiDegradeReason;
  readonly stats?: AiStats;
}

export interface AiClient {
  chooseMove(request: AiMoveRequest): Promise<AiMoveResponse>;
  dispose(): void;
}

export interface AiStrategy {
  choose(request: AiMoveRequest): AiMoveResponse | Promise<AiMoveResponse>;
}

export interface WorkerLike {
  postMessage(message: unknown): void;
  terminate(): void;
  addEventListener(type: string, listener: (event: unknown) => void): void;
  removeEventListener(type: string, listener: (event: unknown) => void): void;
}

export interface AiClientOptions {
  readonly strategy?: AiStrategy;
  readonly clock?: Clock;
  readonly seed?: number;
  readonly workerFactory?: () => WorkerLike;
}

// ---------------------------------------------------------------------------
// §8 测试钩子
// ---------------------------------------------------------------------------

export type AiTestMode =
  | 'normal'
  | 'delay'
  | 'timeout'
  | 'throw'
  | 'illegal'
  | 'no-output'
  | 'no-legal-moves';

export interface AiTestOptions {
  readonly delayMs?: number;
}

export interface TestStateInput {
  readonly pieces: readonly FixturePieceInput[];
  readonly turn: Player;
  readonly ply: number;
  readonly config: SetupConfig;
  readonly phase: 'HUMAN_TURN' | 'AI_THINKING' | 'GAME_OVER';
  readonly result?: GameResult;
  readonly lastMove?: UiAppliedMove | null;
  readonly consecutivePasses?: 0 | 1 | 2;
  readonly gameId?: number;
  readonly selection?: SelectionState | null;
}

export interface TestSnapshot {
  readonly gameId: number;
  readonly phase: AppPhase;
  readonly turn: Player | null;
  readonly ply: number;
  readonly humanColor: Player | null;
  readonly aiColor: Player | null;
  readonly config: SetupConfig;
  readonly result: GameResult | null;
  readonly pieces: readonly Piece[];
  readonly selection: SelectionState | null;
  readonly lastMove: UiAppliedMove | null;
  readonly progress: CampProgress;
  readonly ai: AiInfo | null;
}

export interface CcTestHook {
  readonly version: '1.0';
  loadState(input: TestStateInput): Result<void>;
  getState(): TestSnapshot;
  getLegalMoves(piece?: CubeCoord): readonly LegalMove[];
  getSelection(): SelectionState | null;
  setAiMode(mode: AiTestMode, options?: AiTestOptions): void;
  setSetupConfig(config: Partial<SetupConfig>): Result<SetupConfig>;
  waitForIdle(): Promise<void>;
  reset(): void;
}

declare global {
  interface Window {
    __CC_TEST__?: CcTestHook;
  }
}
