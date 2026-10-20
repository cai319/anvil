/**
 * SVG 棋盘渲染与点击命中（设计规格 §4 / §10）。
 *
 * 只读取 GameSnapshot，不修改引擎状态；棋盘几何与 PRD §4.1.6 / 设计规格 §4.1 一致。
 */

import type {
  BoardSnapshot,
  CubeCoord,
  GameSnapshot,
  GameState,
  Piece,
  Player,
  SelectionState,
  UiAppliedMove,
} from '../types/index.js';
import { BOARD, coordKey, isCell } from '../engine/board.js';
import { getLegalMoves } from '../engine/rules.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const S = 44;
const SQRT3_2 = Math.sqrt(3) / 2;
const HIT_RADIUS = 22;

type AttrValue = string | number | boolean | null | undefined;

function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs?: Record<string, AttrValue>,
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) {
    for (const [name, value] of Object.entries(attrs)) {
      if (value === undefined || value === null) continue;
      el.setAttribute(name, String(value));
    }
  }
  return el;
}

function project(c: CubeCoord): { x: number; y: number } {
  return { x: S * (c.x + c.y / 2), y: -S * SQRT3_2 * c.y };
}

function cellTestId(c: CubeCoord, prefix = 'cell'): string {
  return `${prefix}-${c.x}_${c.y}_${c.z}`;
}

function starPath(): string {
  const outer = 4 * Math.sqrt(3) * S * 1.05;
  const inner = 4 * S * 1.05;
  const points: string[] = [];
  for (let k = 0; k < 12; k += 1) {
    const angle = ((-90 + k * 30) * Math.PI) / 180;
    const radius = k % 2 === 0 ? outer : inner;
    const x = Math.cos(angle) * radius;
    const y = Math.sin(angle) * radius;
    points.push(`${k === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return `${points.join(' ')} Z`;
}

function trianglePoints(vertices: readonly CubeCoord[]): string {
  return vertices
    .map((c) => {
      const p = project(c);
      return `${p.x.toFixed(2)},${p.y.toFixed(2)}`;
    })
    .join(' ');
}

const CAMP_TRIANGLES: ReadonlyArray<{
  id: string;
  points: readonly CubeCoord[];
  kind: 'blue' | 'red';
}> = [
  {
    id: 'cc-camp-top',
    kind: 'blue',
    points: [
      { x: -4, y: 5, z: -1 },
      { x: -1, y: 5, z: -4 },
      { x: -4, y: 8, z: -4 },
    ],
  },
  {
    id: 'cc-camp-bottom',
    kind: 'red',
    points: [
      { x: 4, y: -8, z: 4 },
      { x: 1, y: -5, z: 4 },
      { x: 4, y: -5, z: 1 },
    ],
  },
];

function nearestCell(vx: number, vy: number): CubeCoord | null {
  let best: CubeCoord | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const cell of BOARD.cells) {
    const p = project(cell);
    const d = Math.hypot(p.x - vx, p.y - vy);
    if (d < bestDistance) {
      bestDistance = d;
      best = cell;
    }
  }
  return bestDistance <= HIT_RADIUS ? best : null;
}

export interface MoveAnimation {
  readonly move: UiAppliedMove;
}

export interface BoardViewOptions {
  readonly mount: HTMLElement;
  readonly onCellClick: (pos: CubeCoord) => void;
}

export interface BoardView {
  render(snapshot: GameSnapshot, animation?: MoveAnimation | null): void;
  focus(): void;
  destroy(): void;
}

export function createBoardView(options: BoardViewOptions): BoardView {
  const { mount, onCellClick } = options;

  let destroyed = false;
  let cells = new Map<string, SVGCircleElement>();
  let pieceElements = new Map<string, SVGGElement>();
  let campTop: SVGPolygonElement | null = null;
  let campBottom: SVGPolygonElement | null = null;
  let labelTop: SVGTextElement | null = null;
  let labelBottom: SVGTextElement | null = null;

  const svg = svgEl('svg', {
    viewBox: '-300 -340 600 680',
    role: 'grid',
    tabindex: '0',
    'aria-label': '中国跳棋棋盘，121 个孔位',
    'data-testid': 'board-svg',
    class: 'cc-board-svg',
  });

  // ---------------------------------------------------------------------------
  // defs
  // ---------------------------------------------------------------------------
  const defs = svgEl('defs');

  const addLinearGradient = (
    id: string,
    stops: ReadonlyArray<readonly [string, number]>,
    x1: string,
    y1: string,
    x2: string,
    y2: string,
  ): void => {
    const gradient = svgEl('linearGradient', { id, x1, y1, x2, y2 });
    for (const [color, offset] of stops) {
      gradient.append(svgEl('stop', { offset, 'stop-color': color }));
    }
    defs.append(gradient);
  };

  const addRadialGradient = (
    id: string,
    stops: ReadonlyArray<readonly [string, number]>,
    cx: string,
    cy: string,
    r: string,
  ): void => {
    const gradient = svgEl('radialGradient', { id, cx, cy, r });
    for (const [color, offset] of stops) {
      gradient.append(svgEl('stop', { offset, 'stop-color': color }));
    }
    defs.append(gradient);
  };

  addLinearGradient(
    'cc-board-plate',
    [
      ['#F4EAD5', 0],
      ['#E9DABC', 0.55],
      ['#D8C5A1', 1],
    ],
    '0%',
    '0%',
    '0%',
    '100%',
  );
  addRadialGradient(
    'cc-hole',
    [
      ['#D2C097', 0],
      ['#B7A276', 0.55],
      ['#8E7A50', 1],
    ],
    '50%',
    '38%',
    '70%',
  );
  addRadialGradient(
    'cc-piece-red',
    [
      ['#F4A093', 0],
      ['#D25447', 0.45],
      ['#B43A2E', 0.78],
      ['#892219', 1],
    ],
    '34%',
    '27%',
    '82%',
  );
  addRadialGradient(
    'cc-piece-blue',
    [
      ['#8FB6DA', 0],
      ['#4E7FAD', 0.45],
      ['#35618C', 0.78],
      ['#1F3F60', 1],
    ],
    '34%',
    '27%',
    '82%',
  );

  const filter = svgEl('filter', {
    id: 'cc-piece-shadow',
    x: '-40%',
    y: '-40%',
    width: '180%',
    height: '180%',
  });
  filter.append(
    svgEl('feDropShadow', {
      dx: 0,
      dy: 3.2,
      stdDeviation: 2.4,
      'flood-color': '#3A2A18',
      'flood-opacity': 0.34,
    }),
  );
  defs.append(filter);
  svg.append(defs);

  // ---------------------------------------------------------------------------
  // 静态层
  // ---------------------------------------------------------------------------
  const plate = svgEl('g', { id: 'board-plate' });
  plate.append(
    svgEl('path', {
      d: starPath(),
      fill: 'url(#cc-board-plate)',
      stroke: '#B49E79',
      'stroke-width': 1.6,
      'stroke-linejoin': 'round',
    }),
  );
  const innerStar = svgEl('path', {
    d: starPath(),
    fill: 'none',
    stroke: '#FFFFFF',
    'stroke-opacity': 0.35,
    'stroke-width': 1,
  });
  innerStar.setAttribute('transform', 'scale(0.985)');
  plate.append(innerStar);
  svg.append(plate);

  const camps = svgEl('g', { id: 'camps' });
  for (const camp of CAMP_TRIANGLES) {
    const polygon = svgEl('polygon', {
      id: camp.id,
      points: trianglePoints(camp.points),
      fill: camp.kind === 'blue' ? 'rgba(60,110,159,.10)' : 'rgba(196,71,59,.10)',
      stroke: camp.kind === 'blue' ? 'rgba(60,110,159,.26)' : 'rgba(196,71,59,.26)',
      'stroke-width': 1,
    });
    if (camp.kind === 'blue') campTop = polygon;
    else campBottom = polygon;
    camps.append(polygon);
  }
  svg.append(camps);

  const grid = svgEl('g', { id: 'grid' });
  {
    // 单条 path 绘制全部网格线，减少 DOM 节点数。
    const drawn = new Set<string>();
    const dirs = BOARD.directions.slice(0, 3); // D0/D1/D2 覆盖全部无向边
    const segments: string[] = [];
    for (const cell of BOARD.cells) {
      for (const d of dirs) {
        const next: CubeCoord = { x: cell.x + d.x, y: cell.y + d.y, z: cell.z + d.z };
        if (!isCell(next)) continue;
        const a = coordKey(cell);
        const b = coordKey(next);
        const key = a < b ? `${a}|${b}` : `${b}|${a}`;
        if (drawn.has(key)) continue;
        drawn.add(key);
        const p1 = project(cell);
        const p2 = project(next);
        segments.push(
          `M${p1.x.toFixed(2)},${p1.y.toFixed(2)}L${p2.x.toFixed(2)},${p2.y.toFixed(2)}`,
        );
      }
    }
    grid.append(
      svgEl('path', {
        d: segments.join(''),
        fill: 'none',
        stroke: '#B9A985',
        'stroke-opacity': 0.42,
        'stroke-width': 1.1,
        'stroke-linecap': 'round',
      }),
    );
  }
  svg.append(grid);

  const holes = svgEl('g', { id: 'holes', 'aria-hidden': 'true' });
  {
    const rows = new Map<number, SVGGElement>();
    for (const cell of BOARD.cells) {
      let row = rows.get(cell.y);
      if (!row) {
        row = svgEl('g');
        rows.set(cell.y, row);
        holes.append(row);
      }
      const p = project(cell);
      row.append(
        svgEl('circle', {
          cx: p.x,
          cy: p.y,
          r: 8,
          class: 'cc-hole',
          fill: 'url(#cc-hole)',
          stroke: '#8A744A',
          'stroke-opacity': 0.55,
          'stroke-width': 1,
        }),
      );
    }
  }
  svg.append(holes);

  const lastMovePathLayer = svgEl('g', { id: 'last-move-path' });
  const markerLayer = svgEl('g', { id: 'legal-markers' });
  const pieceLayer = svgEl('g', { id: 'pieces' });
  const overlayLayer = svgEl('g', { id: 'overlay' });
  const draftLayer = svgEl('g', { id: 'draft' });
  const campLabelLayer = svgEl('g', { id: 'camp-labels', class: 'cc-camp-labels' });
  const animationLayer = svgEl('g', { id: 'animation-layer', 'pointer-events': 'none' });

  const makeCampLabel = (
    x: number,
    y: number,
    colorClass: string,
  ): { group: SVGGElement; text: SVGTextElement } => {
    const group = svgEl('g', { class: 'cc-camp-label' });
    group.append(
      svgEl('rect', {
        x: x - 79,
        y: y - 13,
        width: 158,
        height: 26,
        rx: 13,
        class: 'cc-camp-label-bg',
      }),
      svgEl('circle', {
        cx: x - 65,
        cy: y,
        r: 4.2,
        class: colorClass,
      }),
    );
    const text = svgEl('text', {
      x: x + 2,
      y: y + 4.4,
      'text-anchor': 'middle',
      class: 'cc-camp-label-text',
    });
    group.append(text);
    campLabelLayer.append(group);
    return { group, text };
  };

  const topLabel = makeCampLabel(172, -218, 'cc-camp-dot-blue');
  const bottomLabel = makeCampLabel(-172, 218, 'cc-camp-dot-red');
  labelTop = topLabel.text;
  labelBottom = bottomLabel.text;

  svg.append(
    lastMovePathLayer,
    markerLayer,
    pieceLayer,
    overlayLayer,
    draftLayer,
    campLabelLayer,
    animationLayer,
  );

  // 命中层同时承载 cell-* 的语义与 data-testid：位于最上层，测试与真实点击都直接命中。
  const hitLayer = svgEl('g', { id: 'hit-layer' });
  {
    const rows = new Map<number, SVGGElement>();
    for (const cell of BOARD.cells) {
      let row = rows.get(cell.y);
      if (!row) {
        row = svgEl('g', { role: 'row', 'aria-label': `第 ${cell.y} 行` });
        rows.set(cell.y, row);
        hitLayer.append(row);
      }
      const p = project(cell);
      const circle = svgEl('circle', {
        cx: p.x,
        cy: p.y,
        r: HIT_RADIUS,
        fill: 'transparent',
        'pointer-events': 'all',
        class: 'cc-hit',
        role: 'gridcell',
        'data-testid': cellTestId(cell),
        'data-hit-for': `${cell.x},${cell.y},${cell.z}`,
        'data-x': cell.x,
        'data-y': cell.y,
        'data-z': cell.z,
        'data-occupant': 'none',
        'data-selectable': 'false',
        'data-target': 'none',
        'data-selected': 'false',
        'aria-label': `孔位 ${cell.x},${cell.y},${cell.z}，空`,
      });
      cells.set(coordKey(cell), circle);
      row.append(circle);
    }
  }
  svg.append(hitLayer);

  // ---------------------------------------------------------------------------
  // 点击命中
  // ---------------------------------------------------------------------------
  const handleClick = (event: MouseEvent): void => {
    if (destroyed) return;
    const target = event.target as Element | null;
    const hit = target?.closest('[data-hit-for]');
    if (hit) {
      const raw = hit.getAttribute('data-hit-for');
      if (!raw) return;
      const [x, y, z] = raw.split(',').map(Number);
      const coord = { x, y, z };
      if (isCell(coord)) onCellClick(coord);
      return;
    }
    const ctm = svg.getScreenCTM();
    if (!ctm) return;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const local = point.matrixTransform(ctm.inverse());
    const coord = nearestCell(local.x, local.y);
    if (coord) onCellClick(coord);
  };
  svg.addEventListener('click', handleClick);

  mount.append(svg);

  // ---------------------------------------------------------------------------
  // 动态渲染
  // ---------------------------------------------------------------------------
  interface ActiveAnimation {
    gameId: number;
    finalKey: string;
    finalEl: SVGGElement | null;
    flying: SVGGElement;
    points: Array<{ x: number; y: number }>;
    duration: number;
    segmentMs: number;
    segmentCount: number;
    start: number | null;
    frame: number | null;
  }

  let activeAnimation: ActiveAnimation | null = null;

  function finishAnimation(): void {
    if (!activeAnimation) return;
    if (activeAnimation.frame !== null) cancelAnimationFrame(activeAnimation.frame);
    activeAnimation.flying.remove();
    if (activeAnimation.finalEl) activeAnimation.finalEl.style.visibility = '';
    activeAnimation = null;
  }

  function clearDynamic(): void {
    lastMovePathLayer.replaceChildren();
    markerLayer.replaceChildren();
    pieceLayer.replaceChildren();
    overlayLayer.replaceChildren();
    draftLayer.replaceChildren();
    pieceElements = new Map();
  }

  function drawPiece(
    pos: CubeCoord,
    color: Player,
    parent: SVGGElement,
    options2: {
      selected?: boolean;
      lastMove?: boolean;
      testid?: string;
      className?: string;
    } = {},
  ): SVGGElement {
    const p = project(pos);
    const group = svgEl('g', {
      class: `cc-piece ${options2.className ?? ''}`.trim(),
      transform: `translate(${p.x} ${p.y})`,
      'data-color': color,
      'data-selected': options2.selected ? 'true' : 'false',
      'data-last-move': options2.lastMove ? 'true' : 'false',
      'aria-hidden': 'true',
      ...(options2.testid ? { 'data-testid': options2.testid } : {}),
    });
    group.append(
      svgEl('circle', {
        r: 16,
        fill: color === 'R' ? 'url(#cc-piece-red)' : 'url(#cc-piece-blue)',
        stroke: color === 'R' ? '#7E2119' : '#1C3550',
        'stroke-width': 1.2,
        filter: 'url(#cc-piece-shadow)',
      }),
      svgEl('ellipse', {
        cx: -4.7,
        cy: -5.4,
        rx: 6.1,
        ry: 4.1,
        transform: 'rotate(-28 -4.7 -5.4)',
        fill: '#FFFFFF',
        'fill-opacity': 0.4,
      }),
    );
    parent.append(group);
    return group;
  }

  function drawSelectedRing(pos: CubeCoord, parent: SVGGElement): void {
    const p = project(pos);
    const group = svgEl('g', {
      'data-testid': 'selected-piece',
      class: 'cc-selected-ring',
    });
    group.append(
      svgEl('circle', {
        cx: p.x,
        cy: p.y,
        r: 22.5,
        fill: 'none',
        stroke: 'var(--cc-gold)',
        'stroke-width': 3.2,
      }),
      svgEl('circle', {
        cx: p.x,
        cy: p.y,
        r: 19,
        fill: 'none',
        stroke: 'var(--cc-surface)',
        'stroke-opacity': 0.9,
        'stroke-width': 1.4,
      }),
    );
    parent.append(group);
  }

  function drawLegalMarkers(selection: SelectionState): void {
    for (const step of selection.steps) {
      const p = project(step);
      const group = svgEl('g', { 'data-testid': cellTestId(step, 'legal-step') });
      group.append(
        svgEl('circle', {
          cx: p.x,
          cy: p.y,
          r: 10.5,
          fill: 'var(--cc-jade)',
          'fill-opacity': 0.16,
        }),
        svgEl('circle', {
          cx: p.x,
          cy: p.y,
          r: 5.6,
          fill: 'var(--cc-jade)',
          stroke: 'var(--cc-surface)',
          'stroke-width': 1.4,
        }),
      );
      markerLayer.append(group);
    }
    for (const jump of selection.jumps) {
      const p = project(jump);
      const group = svgEl('g', { 'data-testid': cellTestId(jump, 'legal-jump') });
      group.append(
        svgEl('circle', {
          cx: p.x,
          cy: p.y,
          r: 10.5,
          fill: 'var(--cc-jade)',
          'fill-opacity': 0.14,
        }),
        svgEl('circle', {
          cx: p.x,
          cy: p.y,
          r: 6.4,
          fill: 'var(--cc-surface)',
          stroke: 'var(--cc-jade)',
          'stroke-width': 2.4,
        }),
        svgEl('circle', {
          cx: p.x,
          cy: p.y,
          r: 1.8,
          fill: 'var(--cc-jade)',
        }),
      );
      markerLayer.append(group);
    }
  }

  function renderLastMove(lastMove: UiAppliedMove | null): void {
    if (!lastMove) return;
    const points = [lastMove.piece, ...lastMove.path].map((c) => project(c));
    const polyline = svgEl('polyline', {
      points: points.map((p) => `${p.x},${p.y}`).join(' '),
      fill: 'none',
      stroke: 'var(--cc-gold)',
      'stroke-width': 2,
      'stroke-opacity': 0.55,
      'stroke-dasharray': '5 6',
      'stroke-linecap': 'round',
      'data-testid': 'last-move-path',
    });
    lastMovePathLayer.append(polyline);

    const end = points[points.length - 1];
    const marker = svgEl('g', {
      'data-testid': 'last-move-marker',
      class: 'cc-last-move-marker',
    });
    marker.append(
      svgEl('circle', {
        cx: end.x,
        cy: end.y,
        r: 18.5,
        fill: 'none',
        stroke: 'var(--cc-gold)',
        'stroke-width': 2,
        'stroke-dasharray': '3 3',
      }),
      svgEl('circle', {
        cx: end.x + 11,
        cy: end.y - 11,
        r: 4.2,
        fill: 'var(--cc-gold)',
        stroke: 'var(--cc-surface)',
        'stroke-width': 1.4,
      }),
    );
    overlayLayer.append(marker);
  }

  function renderDraft(selection: SelectionState | null, humanColor: Player | null): void {
    if (!selection || selection.mode !== 'jumping') return;
    if (!humanColor) return;
    const origin = project(selection.origin);
    draftLayer.append(
      svgEl('circle', {
        cx: origin.x,
        cy: origin.y,
        r: 16,
        fill: 'none',
        stroke: 'var(--cc-muted)',
        'stroke-width': 1.6,
        'stroke-dasharray': '4 4',
        class: 'cc-origin-ghost',
      }),
    );

    const points = [selection.origin, ...selection.path].map((c) => project(c));
    draftLayer.append(
      svgEl('polyline', {
        points: points.map((p) => `${p.x},${p.y}`).join(' '),
        fill: 'none',
        stroke: 'var(--cc-gold)',
        'stroke-width': 2.6,
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
        class: 'cc-draft-path',
      }),
    );

    selection.path.forEach((step, index) => {
      const p = project(step);
      const badge = svgEl('g', { class: 'cc-hop-badge' });
      badge.append(
        svgEl('circle', { cx: p.x + 18, cy: p.y + 18, r: 8, fill: 'var(--cc-gold)' }),
        svgEl('text', {
          x: p.x + 18,
          y: p.y + 21.5,
          'text-anchor': 'middle',
          class: 'cc-hop-text',
        }),
      );
      const label = badge.lastElementChild as SVGTextElement;
      label.textContent = String(index + 1);
      draftLayer.append(badge);
    });

    drawPiece(selection.current, humanColor, draftLayer, {
      selected: true,
      testid: `piece-${selection.current.x}_${selection.current.y}_${selection.current.z}`,
      className: 'cc-draft-piece',
    });
    drawSelectedRing(selection.current, draftLayer);
  }

  function renderWin(winner: Player | null): void {
    if (!winner) return;
    const targetCamp = BOARD.camps[BOARD.targetCamp[winner]];
    const keys = new Set(targetCamp.map(coordKey));
    for (const piece of currentBoard?.pieces ?? []) {
      if (piece.color !== winner) continue;
      if (!keys.has(coordKey(piece.pos))) continue;
      const p = project(piece.pos);
      overlayLayer.append(
        svgEl('circle', {
          cx: p.x,
          cy: p.y,
          r: 19,
          fill: 'none',
          stroke: 'var(--cc-gold)',
          'stroke-width': 2.4,
        }),
      );
    }
    if (winner === 'R' && campTop) campTop.classList.add('cc-camp--win');
    if (winner === 'B' && campBottom) campBottom.classList.add('cc-camp--win');
  }

  let currentBoard: BoardSnapshot | null = null;
  let currentGameId = 0;

  function render(snapshot: GameSnapshot, animation?: MoveAnimation | null): void {
    if (destroyed) return;
    const board = snapshot.board;
    if (!board) {
      finishAnimation();
      return;
    }

    clearDynamic();
    currentBoard = board;
    currentGameId = snapshot.gameId;

    mount.setAttribute('data-phase', snapshot.phase);
    mount.setAttribute('data-game-id', String(snapshot.gameId));

    const locked = snapshot.phase === 'AI_THINKING' || snapshot.phase === 'GAME_OVER';
    const busy = snapshot.phase === 'AI_THINKING';
    // 设计规格 §10.4：棋盘容器（board）与 SVG 同步暴露思考/锁定状态。
    mount.setAttribute('aria-busy', busy ? 'true' : 'false');
    mount.setAttribute('aria-disabled', locked ? 'true' : 'false');
    svg.setAttribute('aria-busy', busy ? 'true' : 'false');
    svg.setAttribute('aria-disabled', locked ? 'true' : 'false');
    svg.classList.toggle('cc-board--locked', locked);

    if (campTop) campTop.classList.remove('cc-camp--win');
    if (campBottom) campBottom.classList.remove('cc-camp--win');

    const humanColor = snapshot.humanColor;
    const selection = board.selection;

    // 视觉占用（连跳草稿隐藏 origin、显示草稿棋子）。
    const occupant = new Map<string, Player>();
    for (const piece of board.pieces) occupant.set(coordKey(piece.pos), piece.color);

    let selectedKey: string | null = null;
    if (selection) {
      occupant.delete(coordKey(selection.origin));
      if (selection.mode === 'jumping' && humanColor) {
        occupant.set(coordKey(selection.current), humanColor);
        selectedKey = coordKey(selection.current);
      } else {
        occupant.set(coordKey(selection.origin), humanColor ?? 'R');
        selectedKey = coordKey(selection.origin);
      }
    }

    const stepKeys = new Set((selection?.steps ?? []).map(coordKey));
    const jumpKeys = new Set((selection?.jumps ?? []).map(coordKey));

    // 可选棋子：人类回合、己方、有合法着法；连跳中不可切换。
    const selectableKeys = new Set<string>();
    if (
      snapshot.phase === 'HUMAN_TURN' &&
      humanColor &&
      (!selection || selection.mode === 'selected') &&
      snapshot.turn
    ) {
      const engineView: GameState = {
        gameId: snapshot.gameId,
        phase: 'PLAYING',
        turn: snapshot.turn,
        ply: snapshot.ply,
        pieces: board.pieces as readonly Piece[],
        consecutivePasses: 0,
        result: null,
        lastMove: null,
      };
      for (const piece of board.pieces) {
        if (piece.color !== humanColor) continue;
        if (getLegalMoves(engineView, { piece: piece.pos }).length > 0) {
          selectableKeys.add(coordKey(piece.pos));
        }
      }
    }

    // 更新孔位属性。
    for (const cell of BOARD.cells) {
      const key = coordKey(cell);
      const el = cells.get(key);
      if (!el) continue;
      const color = occupant.get(key) ?? null;
      const isSelected = selectedKey === key;
      const target = stepKeys.has(key) ? 'step' : jumpKeys.has(key) ? 'jump' : 'none';
      const selectable = selectableKeys.has(key);
      el.setAttribute('data-occupant', color ?? 'none');
      el.setAttribute('data-selectable', selectable ? 'true' : 'false');
      el.setAttribute('data-target', target);
      el.setAttribute('data-selected', isSelected ? 'true' : 'false');
      const occupantText = color === 'R' ? '红方棋子' : color === 'B' ? '蓝方棋子' : '空';
      const parts = [`孔位 ${cell.x},${cell.y},${cell.z}`, occupantText];
      if (selectable) parts.push('可选');
      if (target === 'step') parts.push('可落子');
      if (target === 'jump') parts.push('可跳跃');
      el.setAttribute('aria-label', parts.join('，'));
    }

    // 最后一手路径（棋子下层）。
    renderLastMove(board.lastMove);

    // 选中 / 合法落点标记（棋子下层）。
    if (selection) {
      if (selection.mode === 'selected') drawSelectedRing(selection.origin, markerLayer);
      drawLegalMarkers(selection);
    }

    // 棋子。
    const lastMoveKey = board.lastMove ? coordKey(board.lastMove.to) : null;
    const hideOriginKey =
      selection && selection.mode === 'jumping' ? coordKey(selection.origin) : null;
    for (const piece of board.pieces) {
      const key = coordKey(piece.pos);
      if (hideOriginKey === key) continue;
      const el = drawPiece(piece.pos, piece.color, pieceLayer, {
        selected: selectedKey === key,
        lastMove: lastMoveKey === key,
        testid: `piece-${piece.pos.x}_${piece.pos.y}_${piece.pos.z}`,
      });
      pieceElements.set(key, el);
    }

    // 若上一帧启动了走子动画：后续事件触发的重绘不能取消它，
    // 且需要把新生成的终点棋子继续隐藏到动画结束。
    if (activeAnimation) {
      if (
        activeAnimation.gameId !== snapshot.gameId ||
        activeAnimation.finalKey !== lastMoveKey
      ) {
        finishAnimation();
      } else {
        const finalEl = pieceElements.get(activeAnimation.finalKey) ?? null;
        activeAnimation.finalEl = finalEl;
        if (finalEl) finalEl.style.visibility = 'hidden';
      }
    }

    // 终局胜负标记。
    const winner = snapshot.result?.reason === 'goal' ? snapshot.result.winner : null;
    if (winner === 'R' || winner === 'B') renderWin(winner);

    // 连跳草稿。
    renderDraft(selection, humanColor);

    // 营地标签归属。
    const humanIsBlue = humanColor === 'B';
    if (labelTop) labelTop.textContent = `蓝方营地 · ${humanIsBlue ? '你' : 'AI'}`;
    if (labelBottom) labelBottom.textContent = `红方营地 · ${humanIsBlue ? 'AI' : '你'}`;

    // 动画：单步/AI 完整路径；auto-end / end-turn 由草稿态直接收尾，不重放。
    if (
      animation &&
      (animation.move.committedBy === 'step' || animation.move.committedBy === 'ai') &&
      !prefersReducedMotion()
    ) {
      animate(animation.move, pieceElements);
    }
  }

  // ---------------------------------------------------------------------------
  // 走子动画（单步 170ms；AI 每跳 90ms，总长 ≤450ms）
  // ---------------------------------------------------------------------------
  function prefersReducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  function animate(move: UiAppliedMove, pieceEls: Map<string, SVGGElement>): void {
    finishAnimation();

    const finalKey = coordKey(move.to);
    const finalEl = pieceEls.get(finalKey) ?? null;
    const points = [move.piece, ...move.path].map(project);
    if (!finalEl || points.length < 2) return;

    const isAi = move.committedBy === 'ai';
    const segmentCount = points.length - 1;
    const perSegment = isAi ? (segmentCount > 5 ? 60 : 90) : 170;
    const duration = isAi ? Math.min(450, perSegment * segmentCount) : perSegment;
    const segmentMs = duration / segmentCount;

    finalEl.style.visibility = 'hidden';

    const flying = drawPiece(move.piece, move.player, animationLayer, {
      className: 'cc-flying-piece',
    });
    flying.style.pointerEvents = 'none';

    const anim: ActiveAnimation = {
      gameId: currentGameId,
      finalKey,
      finalEl,
      flying,
      points,
      duration,
      segmentMs,
      segmentCount,
      start: null,
      frame: null,
    };
    activeAnimation = anim;

    const step = (timestamp: number): void => {
      if (activeAnimation !== anim) return;
      if (anim.start === null) anim.start = timestamp;
      const elapsed = timestamp - anim.start;
      const segmentIndex = Math.min(
        anim.segmentCount - 1,
        Math.floor(elapsed / anim.segmentMs),
      );
      const local = Math.min(1, (elapsed - segmentIndex * anim.segmentMs) / anim.segmentMs);
      const p0 = anim.points[segmentIndex];
      const p1 = anim.points[segmentIndex + 1];
      const control = {
        x: (p0.x + p1.x) / 2,
        y: (p0.y + p1.y) / 2 - 0.18 * S,
      };
      const mt = 1 - local;
      const x = mt * mt * p0.x + 2 * mt * local * control.x + local * local * p1.x;
      const y = mt * mt * p0.y + 2 * mt * local * control.y + local * local * p1.y;
      const scale = 1 + 0.06 * Math.sin(Math.PI * local);
      anim.flying.setAttribute('transform', `translate(${x} ${y}) scale(${scale})`);

      if (elapsed < anim.duration) {
        anim.frame = requestAnimationFrame(step);
      } else {
        finishAnimation();
      }
    };

    anim.frame = requestAnimationFrame(step);
  }

  return {
    render,
    focus(): void {
      svg.focus();
    },
    destroy(): void {
      destroyed = true;
      finishAnimation();
      svg.removeEventListener('click', handleClick);
      clearDynamic();
      svg.remove();
      cells.clear();
      pieceElements.clear();
      campTop = null;
      campBottom = null;
      labelTop = null;
      labelBottom = null;
      currentBoard = null;
    },
  };
}
