/**
 * DOM 绑定、订阅渲染与交互（设计规格 §2 / §5 / §6 / §9 / §10）。
 */

import type {
  ActionResult,
  AiMoveResponse,
  AiStrategy,
  AppEvent,
  Difficulty,
  EngineError,
  GameSnapshot,
  GameResult,
  HumanSide,
  Player,
  RulesApi,
  SetupConfig,
} from '../types/index.js';
import { DEFAULT_SETUP_CONFIG } from '../types/index.js';
import { createAiClient, createDefaultStrategy } from '../ai/index.js';
import {
  applyMove,
  getLegalMoves,
  passTurn,
  queryMoveOptions,
} from '../engine/rules.js';
import { createBoardView, type BoardView, type MoveAnimation } from './boardView.js';
import { createController, type GameControllerWithTest } from './controller.js';
import { installTestHook, type TestControl } from './testHook.js';

type HtmlAttrs = Record<string, string | number | boolean | null | undefined>;

function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: HtmlAttrs = {},
  ...children: Array<Node | string>
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = String(value);
    else if (key === 'text') el.textContent = String(value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  for (const child of children) el.append(child);
  return el;
}

function colorName(player: Player | null): string {
  return player === 'R' ? '红方' : player === 'B' ? '蓝方' : '—';
}

function playerName(player: Player): string {
  return player === 'R' ? '红方' : '蓝方';
}

function difficultyName(difficulty: Difficulty): string {
  return difficulty === 'easy' ? '简单' : difficulty === 'hard' ? '困难' : '普通';
}

function sideName(side: HumanSide): string {
  return side === 'first' ? '我先手' : '我后手';
}

function reasonText(result: GameResult): string {
  if (result.reason === 'goal') return '占满对方营地 10 孔';
  if (result.reason === 'both-pass') return '双方连续无可行着法';
  return '达到 300 手上限';
}

function resultTitleFor(result: GameResult): string {
  if (result.winner === 'draw') return '平局';
  return result.winner === 'R' ? '红方获胜' : '蓝方获胜';
}

function userMessage(error: EngineError): string {
  switch (error.code) {
    case 'EMPTY_CELL':
      return '请选择一枚棋子';
    case 'NOT_YOUR_PIECE':
      return '请选择己方棋子';
    case 'OUT_OF_BOARD':
    case 'NOT_ADJACENT':
    case 'OCCUPIED':
    case 'JUMP_NO_PIECE':
    case 'JUMP_LANDING_OCCUPIED':
      return '不可落子';
    case 'ILLEGAL_MOVE':
      return error.message || '不可落子';
    default:
      return error.message;
  }
}

function isSilentActionError(error: EngineError): boolean {
  return (
    error.code === 'AI_BUSY' ||
    error.code === 'GAME_OVER' ||
    error.code === 'WRONG_PHASE' ||
    error.code === 'NOT_YOUR_TURN' ||
    error.code === 'STALE_GAME'
  );
}

export interface AppHandle {
  readonly controller: GameControllerWithTest;
  readonly boardView: BoardView;
  destroy(): void;
}

export function createApp(root: HTMLElement): AppHandle {
  root.classList.add('cc-app');
  root.replaceChildren();

  // ---------------------------------------------------------------------------
  // 测试控制面（?e2e=1 时由 testHook 使用）
  // ---------------------------------------------------------------------------
  let noLegalMovesArmed = false;
  let controllerRef: GameControllerWithTest | null = null;

  const testControl: TestControl = {
    mode: 'normal',
    options: {},
    armNoLegalMoves(): void {
      noLegalMovesArmed = true;
    },
    reset(): void {
      noLegalMovesArmed = false;
      testControl.mode = 'normal';
      testControl.options = {};
    },
  };

  const realRules: RulesApi = {
    getLegalMoves,
    queryMoveOptions,
    applyMove,
    passTurn,
  };

  const rulesWrapper: RulesApi = {
    getLegalMoves(state, filter) {
      if (noLegalMovesArmed && controllerRef) {
        const snap = controllerRef.getSnapshot();
        if (snap.aiColor !== null && state.turn === snap.aiColor) {
          noLegalMovesArmed = false;
          testControl.mode = 'normal';
          return [];
        }
      }
      return realRules.getLegalMoves(state, filter);
    },
    queryMoveOptions: realRules.queryMoveOptions,
    applyMove: realRules.applyMove,
    passTurn: realRules.passTurn,
  };

  const defaultStrategy = createDefaultStrategy();
  const testStrategy: AiStrategy = {
    choose(request) {
      switch (testControl.mode) {
        case 'normal':
        case 'no-legal-moves':
          return defaultStrategy.choose(request);
        case 'delay': {
          const delayMs = testControl.options.delayMs ?? 1000;
          return new Promise<AiMoveResponse>((resolve) => {
            window.setTimeout(() => {
              const legal = getLegalMoves(request.state, { player: request.player });
              const move = legal.length > 0 ? legal[0] : null;
              resolve({ move, bestSoFar: move, degraded: false });
            }, delayMs);
          });
        }
        case 'timeout':
          return new Promise<AiMoveResponse>(() => {
            /* 永不 resolve，由控制器看门狗兜底 */
          });
        case 'throw':
          throw new Error('测试注入：AI 异常');
        case 'illegal':
          return {
            move: {
              piece: { x: 999, y: 0, z: -999 },
              path: [{ x: 999, y: 0, z: -999 }],
              kind: 'step',
            },
            degraded: false,
          };
        case 'no-output':
          return { move: null, bestSoFar: null, degraded: false };
        default:
          return defaultStrategy.choose(request);
      }
    },
  };

  // ---------------------------------------------------------------------------
  // 页面骨架
  // ---------------------------------------------------------------------------
  const brandTitle = h('h1', { class: 'cc-brand-title', text: '中国跳棋' });
  const brandSub = h('span', { class: 'cc-brand-sub', text: '2 人 · 人机对战' });
  const header = h(
    'header',
    { class: 'cc-header' },
    h('div', { class: 'cc-brand' }, brandTitle, brandSub),
    h('span', { class: 'cc-local-badge', text: '本地 AI · 不联网' }),
  );

  const difficultyLegend = h('legend', { class: 'setup-legend', text: 'AI 难度' });
  const difficultyOptions = h('div', { class: 'option-grid option-grid--difficulty' });
  const difficultyValues: Array<{ value: Difficulty; title: string; desc: string }> = [
    { value: 'easy', title: '简单', desc: '节奏快，适合熟悉规则' },
    { value: 'normal', title: '普通', desc: '会防守，默认推荐' },
    { value: 'hard', title: '困难', desc: '搜索更深，思考 ≤2 秒' },
  ];
  for (const option of difficultyValues) {
    const input = h('input', {
      type: 'radio',
      name: 'difficulty',
      value: option.value,
    });
    const card = h(
      'label',
      {
        class: 'option-card',
        'data-testid': `difficulty-${option.value}`,
      },
      input,
      h('span', { class: 'option-check', 'aria-hidden': 'true' }),
      h('span', { class: 'option-title', text: option.title }),
      h('span', { class: 'option-desc', text: option.desc }),
    );
    difficultyOptions.append(card);
  }

  const sideLegend = h('legend', { class: 'setup-legend', text: '我的先后手' });
  const sideOptions = h('div', { class: 'option-grid option-grid--side' });
  const sideValues: Array<{ value: HumanSide; title: string; desc: string }> = [
    { value: 'first', title: '我先手', desc: '执红 · 下营 · 你先走' },
    { value: 'second', title: '我后手', desc: '执蓝 · 上营 · AI 先走' },
  ];
  for (const option of sideValues) {
    const input = h('input', { type: 'radio', name: 'side', value: option.value });
    const card = h(
      'label',
      { class: 'option-card', 'data-testid': `side-${option.value}` },
      input,
      h('span', { class: 'option-check', 'aria-hidden': 'true' }),
      h('span', { class: 'option-title', text: option.title }),
      h('span', { class: 'option-desc', text: option.desc }),
    );
    sideOptions.append(card);
  }

  const startBtn = h('button', {
    type: 'button',
    class: 'btn btn-primary btn-start',
    'data-testid': 'start-game',
    text: '开始对局',
  });

  const setupScreen = h(
    'section',
    {
      id: 'setup-screen',
      class: 'setup-screen',
      'data-testid': 'setup-panel',
      'aria-labelledby': 'setup-title',
    },
    h('h2', { id: 'setup-title', class: 'setup-title', text: '中国跳棋' }),
    h('p', { class: 'setup-subtitle', text: '与 AI 对战，先手后手由你决定' }),
    h('p', {
      class: 'setup-rules',
      text: '六方向移动 · 可连续跳跃 · 占满对方营地 10 孔获胜',
    }),
    h('fieldset', { class: 'setup-group' }, difficultyLegend, difficultyOptions),
    h('fieldset', { class: 'setup-group' }, sideLegend, sideOptions),
    startBtn,
    h('p', {
      class: 'setup-footnote',
      text: 'AI 每步思考不超过 2 秒；本地运行，不联网。',
    }),
  );

  const boardMount = h('div', {
    class: 'board-frame',
    'data-testid': 'board',
    'data-phase': 'SETUP',
    'data-game-id': '0',
  });
  const jumpStatus = h('span', {
    class: 'jump-status',
    'data-testid': 'jump-status',
    text: '连跳中',
  });
  const endTurnBtn = h('button', {
    type: 'button',
    class: 'btn btn-secondary btn-end-turn',
    'data-testid': 'end-turn',
    text: '结束回合',
  });
  const jumpBar = h(
    'div',
    { class: 'jump-bar', 'data-testid': 'jump-bar', hidden: true },
    jumpStatus,
    endTurnBtn,
  );
  const boardColumn = h('section', { class: 'board-column' }, boardMount, jumpBar);

  const turnText = h('span', {
    class: 'turn-text',
    'data-testid': 'turn-text',
    'aria-live': 'polite',
    text: '你的回合 · 红方',
  });
  const thinkingDots = h(
    'span',
    {
      class: 'thinking-dots',
      'data-testid': 'ai-thinking',
      'aria-hidden': 'true',
      hidden: true,
    },
    h('i'), h('i'), h('i'),
  );
  const turnDot = h('span', { class: 'turn-dot', 'aria-hidden': 'true' });
  const turnSideBadge = h('span', { class: 'pill pill-human', text: '你' });
  const moveCount = h('span', { class: 'move-count', 'data-testid': 'move-count', text: '0' });
  const turnDifficulty = h('span', { class: 'turn-difficulty', text: '普通难度' });
  const turnCard = h(
    'section',
    {
      class: 'turn-card',
      'data-testid': 'turn-indicator',
      'aria-labelledby': 'turn-heading',
    },
    h('h2', { id: 'turn-heading', class: 'sr-only', text: '对局状态' }),
    h(
      'div',
      { class: 'turn-main' },
      turnDot,
      turnText,
      thinkingDots,
      turnSideBadge,
    ),
    h(
      'div',
      { class: 'turn-sub' },
      h('span', { class: 'turn-ply' }, h('span', { text: '第 ' }), moveCount, h('span', { text: ' 手 · ' })),
      turnDifficulty,
    ),
  );

  const makeProgressRow = (
    color: 'red' | 'blue',
    label: string,
  ): { row: HTMLElement; value: HTMLElement; bar: HTMLElement } => {
    const value = h('span', {
      class: 'progress-value',
      'data-testid': `progress-${color}`,
      text: '0/10',
    });
    const bar = h('div', {
      class: 'progress-bar',
      role: 'progressbar',
      'aria-valuemin': '0',
      'aria-valuemax': '10',
      'aria-valuenow': '0',
      'aria-label': `${label}进度`,
    });
    for (let i = 0; i < 10; i += 1) bar.append(h('span', { class: 'progress-seg' }));
    const row = h(
      'div',
      { class: `progress-row progress-row--${color}` },
      h('span', { class: 'progress-label', text: label }),
      bar,
      value,
      h('span', { class: 'pill pill-side', text: '你' }),
    );
    return { row, value, bar };
  };
  const redProgress = makeProgressRow('red', '红方进营');
  const blueProgress = makeProgressRow('blue', '蓝方进营');
  const progressCard = h(
    'section',
    { class: 'progress-card', 'aria-labelledby': 'progress-heading' },
    h('h3', { id: 'progress-heading', class: 'card-title', text: '进营进度' }),
    redProgress.row,
    blueProgress.row,
  );

  const restartBtn = h('button', {
    type: 'button',
    class: 'btn btn-primary',
    'data-testid': 'restart-button',
    text: '重新开始',
  });
  const backBtn = h('button', {
    type: 'button',
    class: 'btn btn-secondary',
    'data-testid': 'back-to-setup',
    text: '返回设置',
  });
  const actionsCard = h(
    'section',
    { class: 'actions-card' },
    restartBtn,
    backBtn,
    h('p', {
      class: 'actions-note',
      text: '对局中可随时重新开始或返回设置',
    }),
  );

  const difficultyBadge = h('span', {
    class: 'info-value',
    'data-testid': 'difficulty-badge',
    text: '普通',
  });
  const sideBadge = h('span', {
    class: 'info-value',
    'data-testid': 'side-badge',
    text: '我先手 · 红方',
  });
  const infoCard = h(
    'section',
    { class: 'info-card', 'data-testid': 'info-card' },
    h('h3', { class: 'card-title', text: '对局信息' }),
    h('div', { class: 'info-row' }, h('span', { class: 'info-label', text: '难度' }), difficultyBadge),
    h('div', { class: 'info-row' }, h('span', { class: 'info-label', text: '执子' }), sideBadge),
    h('p', {
      class: 'info-note',
      text: '连跳可随时结束；进入目标营后可自由移动。',
    }),
  );

  const degradedNotice = h(
    'div',
    {
      class: 'degraded-notice',
      'data-testid': 'ai-degraded-notice',
      role: 'status',
      'aria-live': 'polite',
      hidden: true,
    },
    h('span', { class: 'degraded-title', text: 'AI 已降级为简单难度' }),
    h('span', { class: 'degraded-sub', text: '（本局剩余时间生效）' }),
  );

  // 降级提示放在状态卡内，避免右栏栅格新增行导致越出首屏（S9 P2-02）。
  turnCard.append(degradedNotice);
  const sideColumn = h(
    'div',
    { class: 'side-column' },
    turnCard,
    progressCard,
    actionsCard,
    infoCard,
  );
  const gameScreen = h(
    'main',
    { id: 'game-screen', class: 'game-layout', hidden: true },
    boardColumn,
    sideColumn,
  );

  const footer = h('footer', {
    class: 'cc-footer',
    text: '规则摘要：六方向单步 · 连续跳跃 · 跳跃不吃子 · 占满对方营地 10 孔获胜',
  });

  const hintRegion = h('div', {
    class: 'hint-region',
    'data-testid': 'hint',
    role: 'status',
    'aria-live': 'polite',
  });
  const srStatus = h('div', {
    class: 'sr-only',
    id: 'sr-status',
    'aria-live': 'polite',
    'aria-atomic': 'true',
  });

  const resultTitle = h('h2', {
    id: 'result-title',
    class: 'result-title',
    'data-testid': 'game-result-title',
    tabindex: '-1',
  });
  const resultSubtitle = h('p', {
    id: 'result-desc',
    class: 'result-subtitle',
    'data-testid': 'game-result-subtitle',
  });
  const resultReason = h('p', {
    class: 'result-reason',
    'data-testid': 'game-result-reason',
  });
  const resultSummary = h('p', { class: 'result-summary' });
  const resultIcon = h('div', { class: 'result-icon', 'aria-hidden': 'true' });
  const resultRestart = h('button', {
    type: 'button',
    class: 'btn btn-primary',
    'data-testid': 'result-restart-button',
    text: '再来一局',
  });
  const resultBack = h('button', {
    type: 'button',
    class: 'btn btn-secondary',
    'data-testid': 'result-back-to-setup',
    text: '返回设置',
  });
  const resultDialog = h(
    'div',
    {
      class: 'result-dialog',
      role: 'dialog',
      'aria-modal': 'true',
      'aria-labelledby': 'result-title',
      'aria-describedby': 'result-desc',
      'data-testid': 'game-result',
    },
    resultIcon,
    resultTitle,
    resultSubtitle,
    resultReason,
    resultSummary,
    h('div', { class: 'result-actions' }, resultRestart, resultBack),
  );
  const modalLayer = h('div', { class: 'modal-layer', hidden: true }, resultDialog);

  root.append(header, setupScreen, gameScreen, footer, hintRegion, srStatus, modalLayer);

  // ---------------------------------------------------------------------------
  // 控制器 / 棋盘
  // ---------------------------------------------------------------------------
  const ai = createAiClient({ strategy: testStrategy });
  const controller = createController({ ai, rules: rulesWrapper });
  controllerRef = controller;

  let pendingAnimation: MoveAnimation | null = null;

  const boardView = createBoardView({
    mount: boardMount,
    onCellClick(pos) {
      handleAction(controller.handleCellClick(pos));
    },
  });

  // ---------------------------------------------------------------------------
  // 提示 / 播报
  // ---------------------------------------------------------------------------
  let hintTimer: number | null = null;
  function showHint(message: string): void {
    hintRegion.textContent = message;
    hintRegion.classList.add('is-visible');
    if (hintTimer !== null) window.clearTimeout(hintTimer);
    hintTimer = window.setTimeout(() => {
      hintTimer = null;
      hintRegion.textContent = '';
      hintRegion.classList.remove('is-visible');
    }, 2000);
  }

  let announceTimer: number | null = null;
  function announce(message: string): void {
    if (announceTimer !== null) window.clearTimeout(announceTimer);
    srStatus.textContent = '';
    announceTimer = window.setTimeout(() => {
      announceTimer = null;
      srStatus.textContent = message;
    }, 10);
  }

  function handleAction(result: ActionResult): void {
    if (result.ok) return;
    if (isSilentActionError(result.error)) return;
    showHint(userMessage(result.error));
  }

  // ---------------------------------------------------------------------------
  // 结果弹层
  // ---------------------------------------------------------------------------
  let modalOpen = false;

  function setBackgroundInert(inert: boolean): void {
    for (const element of [header, setupScreen, gameScreen, footer]) {
      if (inert) element.setAttribute('inert', '');
      else element.removeAttribute('inert');
    }
  }

  function openModal(snap: GameSnapshot): void {
    if (!snap.result) return;
    resultTitle.textContent = resultTitleFor(snap.result);
    resultSubtitle.textContent =
      snap.result.winner === 'draw'
        ? '本局平局'
        : snap.result.winner === snap.humanColor
          ? '恭喜，你赢了'
          : '再接再厉';
    resultReason.textContent = reasonText(snap.result);
    resultIcon.className =
      snap.result.winner === 'draw'
        ? 'result-icon result-icon--draw'
        : snap.result.winner === snap.humanColor
          ? 'result-icon result-icon--win'
          : 'result-icon result-icon--lose';
    const effective = snap.ai?.effectiveDifficulty ?? snap.config.difficulty;
    resultSummary.textContent = `第 ${snap.ply} 手 · ${difficultyName(effective)}难度 · ${sideName(
      snap.config.humanSide,
    )}`;

    if (modalOpen) return;
    modalOpen = true;
    modalLayer.hidden = false;
    setBackgroundInert(true);
    resultTitle.focus();
  }

  function closeModal(snap: GameSnapshot): void {
    if (!modalOpen) return;
    modalOpen = false;
    modalLayer.hidden = true;
    setBackgroundInert(false);
    if (snap.phase === 'SETUP') {
      startBtn.focus();
    } else {
      boardView.focus();
    }
  }

  modalLayer.addEventListener('keydown', (event) => {
    if (!modalOpen || event.key !== 'Tab') return;
    const focusables = [resultRestart, resultBack];
    const index = focusables.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.shiftKey
      ? index <= 0
        ? focusables.length - 1
        : index - 1
      : index < 0 || index === focusables.length - 1
        ? 0
        : index + 1;
    event.preventDefault();
    focusables[next].focus();
  });

  // ---------------------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------------------
  function syncSetup(config: SetupConfig): void {
    const difficulty = setupScreen.querySelectorAll<HTMLInputElement>(
      'input[name="difficulty"]',
    );
    for (const input of difficulty) input.checked = input.value === config.difficulty;
    const side = setupScreen.querySelectorAll<HTMLInputElement>('input[name="side"]');
    for (const input of side) input.checked = input.value === config.humanSide;
  }

  function updateProgress(
    row: { value: HTMLElement; bar: HTMLElement },
    count: number,
    isHuman: boolean,
  ): void {
    row.value.textContent = `${count}/10`;
    row.bar.setAttribute('aria-valuenow', String(count));
    const segments = Array.from(row.bar.children) as HTMLElement[];
    segments.forEach((segment, index) => {
      segment.classList.toggle('is-filled', index < count);
    });
    const pill = row.bar.parentElement?.querySelector('.pill-side');
    if (pill) {
      pill.textContent = isHuman ? '你' : 'AI';
      pill.classList.toggle('pill-human', isHuman);
      pill.classList.toggle('pill-ai', !isHuman);
    }
  }

  function render(snap: GameSnapshot): void {
    const inSetup = snap.phase === 'SETUP';
    setupScreen.hidden = !inSetup;
    gameScreen.hidden = inSetup;

    if (inSetup) {
      syncSetup(snap.config);
      if (modalOpen) closeModal(snap);
      return;
    }

    boardView.render(snap, pendingAnimation);
    pendingAnimation = null;

    const selection = snap.board?.selection ?? null;
    const jumping = selection?.mode === 'jumping' && snap.phase === 'HUMAN_TURN';

    // 状态卡
    if (snap.phase === 'GAME_OVER') {
      turnText.textContent = '对局结束';
      thinkingDots.hidden = true;
      turnSideBadge.textContent = '—';
      turnSideBadge.className = 'pill pill-neutral';
    } else {
      turnText.textContent =
        snap.phase === 'AI_THINKING'
          ? `AI 思考中 · ${colorName(snap.turn)}`
          : `你的回合 · ${colorName(snap.turn)}`;
      thinkingDots.hidden = snap.phase !== 'AI_THINKING';
      const humanTurn = snap.phase === 'HUMAN_TURN';
      turnSideBadge.textContent = humanTurn ? '你' : 'AI';
      turnSideBadge.className = humanTurn ? 'pill pill-human' : 'pill pill-ai';
    }
    turnDot.className = `turn-dot turn-dot--${snap.turn === 'R' ? 'red' : 'blue'}`;
    moveCount.textContent = String(snap.ply);
    const effective = snap.ai?.effectiveDifficulty ?? snap.config.difficulty;
    turnDifficulty.textContent = `${difficultyName(effective)}难度${
      snap.ai?.forcedEasy ? ' · 已降级' : ''
    } · ${sideName(snap.config.humanSide)}`;

    const humanColor = snap.humanColor;
    updateProgress(redProgress, snap.board?.progress.R ?? 0, humanColor === 'R');
    updateProgress(blueProgress, snap.board?.progress.B ?? 0, humanColor === 'B');

    // 连跳条（普通文档流；不改变棋盘尺寸）
    jumpBar.hidden = !jumping;
    if (jumping && selection) {
      jumpStatus.textContent = `连跳中 · 已跳 ${selection.path.length} 跳`;
      endTurnBtn.disabled = !selection.canEndTurn;
    }

    // 信息卡
    difficultyBadge.textContent = `${difficultyName(effective)}${
      snap.ai?.forcedEasy ? ' · 已降级' : ''
    }`;
    sideBadge.textContent = `${sideName(snap.config.humanSide)} · ${colorName(
      humanColor,
    )}`;

    degradedNotice.hidden = !snap.ai?.forcedEasy;

    if (snap.phase === 'GAME_OVER' && snap.result) {
      openModal(snap);
    } else if (modalOpen) {
      closeModal(snap);
    }
  }

  // ---------------------------------------------------------------------------
  // 事件
  // ---------------------------------------------------------------------------
  function handleEvent(snap: GameSnapshot, event: AppEvent): void {
    switch (event.type) {
      case 'match-start':
        if (snap.phase === 'HUMAN_TURN') {
          announce(`对局开始。你的回合，${colorName(snap.turn)}。`);
        } else if (snap.phase === 'AI_THINKING') {
          announce(`对局开始。AI 执${colorName(snap.turn)}，思考中。`);
        }
        break;
      case 'restart':
        break;
      case 'back-to-setup':
        break;
      case 'move-applied': {
        pendingAnimation = { move: event.move };
        const player = playerName(event.move.player);
        if (event.source === 'ai') {
          announce('AI 走了一步。');
        } else if (event.move.kind === 'jump' && event.move.path.length > 1) {
          announce(`${player}连跳 ${event.move.path.length} 跳。`);
        } else {
          announce(`${player}走了一步。`);
        }
        break;
      }
      case 'pass':
        showHint(`${playerName(event.player)}无可行着法，自动轮空`);
        announce(`${playerName(event.player)}无可行着法，自动轮空。`);
        break;
      case 'selection-changed':
        if (event.selection && event.selection.mode === 'selected') {
          announce(
            `已选中${colorName(snap.humanColor)}棋子，可落子 ${event.selection.steps.length} 处，可跳跃 ${event.selection.jumps.length} 处。`,
          );
        }
        break;
      case 'jump-step':
        break;
      case 'ai-thinking':
        break;
      case 'ai-degraded':
        announce('AI 已降级为简单难度。');
        break;
      case 'game-over':
        announce(`对局结束，${resultTitleFor(event.result)}。`);
        break;
      case 'hint':
        showHint(event.hint.message);
        break;
      case 'error':
        console.error('[CC][app]', event.error);
        if (event.error.display === 'toast') showHint(event.error.message);
        break;
      case 'turn-changed':
      case 'setup-config-changed':
      default:
        break;
    }

    render(snap);

    if (event.type === 'match-start') boardView.focus();
    if (event.type === 'game-over') resultTitle.focus();
    if (event.type === 'back-to-setup') startBtn.focus();
  }

  controller.subscribe(handleEvent);

  // ---------------------------------------------------------------------------
  // DOM 事件
  // ---------------------------------------------------------------------------
  setupScreen.addEventListener('change', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.name === 'difficulty') {
      const result = controller.setSetupConfig({ difficulty: target.value as Difficulty });
      if (!result.ok) showHint(userMessage(result.error));
    } else if (target.name === 'side') {
      const result = controller.setSetupConfig({ humanSide: target.value as HumanSide });
      if (!result.ok) showHint(userMessage(result.error));
    }
  });

  startBtn.addEventListener('click', () => {
    const result = controller.startGame();
    if (!result.ok) showHint(userMessage(result.error));
  });

  restartBtn.addEventListener('click', () => {
    const result = controller.restart();
    if (!result.ok) showHint(userMessage(result.error));
  });

  backBtn.addEventListener('click', () => controller.backToSetup());

  resultRestart.addEventListener('click', () => {
    const result = controller.restart();
    if (!result.ok) showHint(userMessage(result.error));
  });

  resultBack.addEventListener('click', () => controller.backToSetup());

  endTurnBtn.addEventListener('click', () => {
    handleAction(controller.endTurn());
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const snap = controller.getSnapshot();
    if (snap.phase !== 'HUMAN_TURN') return;
    const selection = snap.board?.selection;
    if (selection?.mode === 'selected') {
      controller.handleCellClick(selection.origin);
    }
  });

  const onVisibility = (): void => {
    if (document.visibilityState === 'visible') controller.checkDeadlines();
  };
  document.addEventListener('visibilitychange', onVisibility);
  const onPageShow = (): void => controller.checkDeadlines();
  window.addEventListener('pageshow', onPageShow);

  // ---------------------------------------------------------------------------
  // 测试钩子（仅 ?e2e=1）
  // ---------------------------------------------------------------------------
  const e2eEnabled =
    new URLSearchParams(window.location.search).get('e2e') === '1';
  installTestHook(controller, e2eEnabled, testControl);

  // 首次渲染
  syncSetup(DEFAULT_SETUP_CONFIG);
  render(controller.getSnapshot());

  return {
    controller,
    boardView,
    destroy(): void {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pageshow', onPageShow);
      controller.destroy();
      boardView.destroy();
    },
  };
}
