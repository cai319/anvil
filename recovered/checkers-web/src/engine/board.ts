/**
 * 棋盘定义（PRD §4.1）与坐标工具（契约 §2.3）。
 *
 * 立方坐标 (x, y, z)，恒满足 x + y + z = 0。
 * 121 个孔位；中心六边形 61 孔；6 个营地各 10 孔。
 */

import type {
  BoardDefinition,
  CampId,
  CellKey,
  CoordTuple,
  CubeCoord,
  GameState,
  Piece,
  Player,
  Result,
} from '../types/index.js';
import { err, ok } from '../types/index.js';

/** y → [x 左界, x 右界]，规范顺序 y = +8 … −8。 */
const ROW_RANGES: ReadonlyArray<readonly [number, number, number]> = [
  [8, -4, -4],
  [7, -4, -3],
  [6, -4, -2],
  [5, -4, -1],
  [4, -8, 4],
  [3, -7, 4],
  [2, -6, 4],
  [1, -5, 4],
  [0, -4, 4],
  [-1, -4, 5],
  [-2, -4, 6],
  [-3, -4, 7],
  [-4, -4, 8],
  [-5, 1, 4],
  [-6, 2, 4],
  [-7, 3, 4],
  [-8, 4, 4],
];

/** PRD §4.1.4 的 D0..D5（顺序固定）。 */
const DIRECTIONS: readonly CubeCoord[] = [
  { x: 1, y: -1, z: 0 }, // D0
  { x: 1, y: 0, z: -1 }, // D1
  { x: 0, y: 1, z: -1 }, // D2
  { x: -1, y: 1, z: 0 }, // D3
  { x: -1, y: 0, z: 1 }, // D4
  { x: 0, y: -1, z: 1 }, // D5
];

const CAMP_PREDICATES: Readonly<Record<CampId, (x: number, y: number, z: number) => boolean>> = {
  // 上营：y ∈ [5,8]，x ∈ [−4, 4−y]
  'C+': (x, y) => y >= 5 && y <= 8 && x >= -4 && x <= 4 - y,
  // 下营：y ∈ [−8,−5]，x ∈ [−y−4, 4]
  'C-': (x, y) => y <= -5 && y >= -8 && x >= -y - 4 && x <= 4,
  // 右下营：x ∈ [5,8]，z ∈ [−4, 4−x]
  'C+x': (x, _y, z) => x >= 5 && x <= 8 && z >= -4 && z <= 4 - x,
  // 左上营：x ∈ [−8,−5]，z ∈ [−x−4, 4]
  'C-x': (x, _y, z) => x <= -5 && x >= -8 && z >= -x - 4 && z <= 4,
  // 左下营：z ∈ [5,8]，y ∈ [−4, 4−z]
  'C+z': (_x, y, z) => z >= 5 && z <= 8 && y >= -4 && y <= 4 - z,
  // 右上营：z ∈ [−8,−5]，y ∈ [−z−4, 4]
  'C-z': (_x, y, z) => z <= -5 && z >= -8 && y >= -z - 4 && y <= 4,
};

const CAMP_IDS: readonly CampId[] = ['C+', 'C-', 'C+x', 'C-x', 'C+z', 'C-z'];

function makeCells(): readonly CubeCoord[] {
  const cells: CubeCoord[] = [];
  for (const [y, left, right] of ROW_RANGES) {
    for (let x = left; x <= right; x += 1) {
      cells.push({ x, y, z: -x - y });
    }
  }
  return cells;
}

function buildBoard(): BoardDefinition {
  const cells = makeCells();

  const camps = {} as Record<CampId, readonly CubeCoord[]>;
  for (const campId of CAMP_IDS) {
    const predicate = CAMP_PREDICATES[campId];
    camps[campId] = cells.filter((c) => predicate(c.x, c.y, c.z));
  }

  const centerCells = cells.filter(
    (c) => Math.max(Math.abs(c.x), Math.abs(c.y), Math.abs(c.z)) <= 4,
  );

  const initialPieces: Piece[] = [
    ...camps['C-']
      .slice()
      .sort(compareCoordAsc)
      .map((pos) => ({ color: 'R' as const, pos })),
    ...camps['C+']
      .slice()
      .sort(compareCoordAsc)
      .map((pos) => ({ color: 'B' as const, pos })),
  ];

  return {
    cells,
    centerCells,
    camps,
    targetCamp: { R: 'C+', B: 'C-' },
    directions: DIRECTIONS,
    initialPieces,
  };
}

/** 坐标升序：x → y → z。 */
export function compareCoordAsc(a: CubeCoord, b: CubeCoord): number {
  if (a.x !== b.x) return a.x - b.x;
  if (a.y !== b.y) return a.y - b.y;
  return a.z - b.z;
}

export function compareCoordTuple(a: CoordTuple, b: CoordTuple): number {
  if (a[0] !== b[0]) return a[0] - b[0];
  if (a[1] !== b[1]) return a[1] - b[1];
  return a[2] - b[2];
}

export const BOARD: BoardDefinition = buildBoard();

const CELL_KEYS: ReadonlySet<CellKey> = new Set(BOARD.cells.map(coordKey));

const TARGET_CAMP_KEYS: Readonly<Record<Player, ReadonlySet<CellKey>>> = {
  R: new Set(BOARD.camps[BOARD.targetCamp.R].map(coordKey)),
  B: new Set(BOARD.camps[BOARD.targetCamp.B].map(coordKey)),
};

/** 规范坐标 key：`${x},${y},${z}`（ASCII）。 */
export function coordKey(c: CubeCoord): CellKey {
  return `${c.x},${c.y},${c.z}`;
}

export function isCell(coord: CubeCoord): boolean {
  return (
    Number.isInteger(coord.x) &&
    Number.isInteger(coord.y) &&
    Number.isInteger(coord.z) &&
    coord.x + coord.y + coord.z === 0 &&
    CELL_KEYS.has(coordKey(coord))
  );
}

function isCoordLike(value: unknown): value is CubeCoord {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as { x?: unknown; y?: unknown; z?: unknown };
  return (
    typeof v.x === 'number' &&
    typeof v.y === 'number' &&
    typeof v.z === 'number'
  );
}

function isTupleLike(value: unknown): value is CoordTuple {
  return (
    Array.isArray(value) &&
    value.length === 3 &&
    value.every((n) => typeof n === 'number')
  );
}

/**
 * 将 CubeCoord 或 [x,y,z] 转为 CubeCoord。
 * 编程错误（非整数、和不为 0）抛 RangeError；夹具入口会捕获并转为 INVALID_FIXTURE。
 */
export function toCoord(input: CubeCoord | CoordTuple): CubeCoord {
  let x: number;
  let y: number;
  let z: number;
  if (isTupleLike(input)) {
    [x, y, z] = input;
  } else if (isCoordLike(input)) {
    x = input.x;
    y = input.y;
    z = input.z;
  } else {
    throw new RangeError('非法坐标输入');
  }
  if (!Number.isInteger(x) || !Number.isInteger(y) || !Number.isInteger(z)) {
    throw new RangeError('坐标必须为整数');
  }
  if (x + y + z !== 0) {
    throw new RangeError('坐标必须满足 x + y + z = 0');
  }
  return { x, y, z };
}

export function toTuple(c: CubeCoord): CoordTuple {
  return [c.x, c.y, c.z] as const;
}

/** 解析坐标 key；非法/越界返回 INVALID_STATE。 */
export function parseCoordKey(key: CellKey): Result<CubeCoord> {
  if (typeof key !== 'string') return err('INVALID_STATE', '坐标 key 必须为字符串');
  const parts = key.split(',');
  if (parts.length !== 3) return err('INVALID_STATE', `坐标 key 非法: ${key}`);
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isInteger(n))) {
    return err('INVALID_STATE', `坐标 key 含非整数: ${key}`);
  }
  const coord: CubeCoord = { x: nums[0], y: nums[1], z: nums[2] };
  if (coord.x + coord.y + coord.z !== 0) {
    return err('INVALID_STATE', `坐标 key 不满足 x + y + z = 0: ${key}`);
  }
  if (!isCell(coord)) return err('INVALID_STATE', `坐标 key 越界: ${key}`);
  return ok(coord);
}

/** 返回该孔位上的棋子；无棋子/非法孔位返回 null。重复棋子时警告并返回第一个。 */
export function getPieceAt(state: GameState, pos: CubeCoord): Piece | null {
  if (!isCell(pos)) return null;
  const key = coordKey(pos);
  let found: Piece | null = null;
  for (const piece of state.pieces) {
    if (coordKey(piece.pos) === key) {
      if (found) {
        console.warn('[CC][board] 同一孔位出现多枚棋子', key);
        return found;
      }
      found = piece;
    }
  }
  return found;
}

export function isInTargetCamp(player: Player, pos: CubeCoord): boolean {
  return TARGET_CAMP_KEYS[player].has(coordKey(pos));
}

export function countInTargetCamp(state: GameState, player: Player): number {
  const target = TARGET_CAMP_KEYS[player];
  let count = 0;
  for (const piece of state.pieces) {
    if (piece.color === player && target.has(coordKey(piece.pos))) count += 1;
  }
  return count;
}

/** 仅供引擎内部快速判定使用。 */
export function targetCampKeys(player: Player): ReadonlySet<CellKey> {
  return TARGET_CAMP_KEYS[player];
}

export const OTHER_PLAYER: Readonly<Record<Player, Player>> = { R: 'B', B: 'R' };
