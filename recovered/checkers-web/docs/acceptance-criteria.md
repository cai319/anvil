# 跳跃冒险：晴空原野 验收标准（Acceptance Criteria）

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 文档状态 | **验收基线**（与 `docs/prd.md` 同步冻结） |
| 任务 | S1「需求规格与验收基线（PRD）」 |
| 父任务 | `024d590a-e65c-465b-ace6-df92454db5f9`「开发一个马里奥闯关的小游戏」 |
| 工作流 | `21214745-a8e6-4491-84c5-122a2eea94db` |
| 关联 PRD | `docs/prd.md`（FR-01 ~ FR-40） |
| 使用角色 | tester（独立执行） / developer（自测） / designer（设计评审） / PM（发布判定） |
| 最后更新 | 2026-10-19 |

> **使用说明**
>
> 1. 本文件是唯一验收基线；每条验收项均以 Given / When / Then 描述可观察结果。测试报告应逐条引用 AC 编号并附证据（命令输出、截图、日志、缺陷 ID）。
> 2. 优先级：**P0** = 发布阻断；**P1** = 发布前应完成，未完成须 PM/人类书面接受；**P2** = 可遗留（由 PM 在发布评审中决定）。
> 3. 本文件共 **33** 条：P0 31 条、P1 1 条、P2 1 条（统计见 §3）。
> 4. 缺陷等级：P0（无法加载、无法键盘控制、碰撞穿透、无法通关、崩溃）立即阻塞并升级 human；P1/P2/P3 按影响递减，必须附复现步骤、预期/实际、截图/日志。

---

## 1. 验收环境与命令

| 项目 | 要求 |
|---|---|
| Node.js / npm | Node ≥20、npm ≥10 |
| 浏览器 | Chrome / Edge / Firefox / Safari 最近两个大版本（桌面） |
| Playwright | Chromium + Firefox + WebKit；首次需 `npx playwright install` |
| 安装 | `npm ci` |
| 构建 | `npm run build` → `dist/` |
| 预览 | `npm run preview` |
| 单元测试 | `npm test`（Vitest，非 watch） |
| 端到端测试 | `npm run test:e2e`（Playwright，内置构建/静态服务） |
| 测试钩子 | 仅 URL 带 `?e2e=1` 时启用 `window.__GAME__`；正常入口不暴露 |
| 素材合规 | 仅原创或明确许可素材；不得出现任天堂角色/名称/图像/音乐/音效 |

> 若依赖安装或浏览器环境不可用，按项目反馈路径立即 `@cindy` 并升级 human，不得以“无法测试”代替验收结论。

---

## 2. 测试钩子契约与数值基线（供后续测试直接引用）

`?e2e=1` 时暴露 `window.__GAME__`；正常入口（无参数）必须为 `undefined`。测试钩子不得改变正常游玩逻辑。

### 2.1 坐标约定

- **世界坐标**：单位 px，原点在关卡左上角，x 向右、y 向下；世界 **5760×480**（180×15 瓦片，32px/瓦片）。
- **容器/画布**：`game-root` 逻辑 **960×540**（16:9），按视口 letterbox 缩放（比例容差 ±0.01），无裁切/变形；`game-canvas` 逻辑 **960×480**，绘制游戏世界；HUD 为 **60px 高的 DOM 条**（不进入世界坐标）；世界 y=0 对应 canvas 顶部。
- **实体坐标**：`getState()` 中所有实体 `x,y` 为 **AABB 左上角**（世界 px）；玩家 AABB **24×28**；敌人 **30×30**；金币 **20×20**；块 **32×32**。
- **出生点**：玩家 AABB 左上角 **(84, 388)**（脚底中心 x=96、y=416，站在地面第 13 行顶面）；`vx=0`、`vy=0`、`grounded=true`、`facing='right'`。
- **摄像机**：`camera.x = clamp(playerCenterX − 480, 0, 4800)`，`camera.y=0`；`playerCenterX = player.x + player.w/2`；S2 若增加死区，必须在接口契约中锁定等价公式，AC-08 按 S2 公式断言 ±1px。
- **旗杆**：`x=5504`（172 瓦片），从地面向上约 320px；摄像机夹紧到 4800 时屏幕 x≈704px。
- **坑底部杀伤线**：世界 `y > 544`（480+64）。

### 2.2 状态快照与关卡摘要 schema

`getState()` 返回 **JSON 可序列化、不可变**快照：

```ts
{
  state: 'TITLE'|'PLAYING'|'PAUSED'|'DYING'|'GAME_OVER'|'LEVEL_CLEAR',
  frame: number;                 // 已推进的固定步数
  fixedStepAccumulator: number;  // 0..16.666 ms
  paused: boolean;
  lives: number; score: number; coins: number;
  player: { x,y,w,h,vx,vy,grounded,facing,invulnerableMs };
  enemies: [{ id,x,y,w,h,vx,vy,alive,defeated }];
  coinsInWorld: [{ id,x,y,w,h,collected }];
  blocks: [{ id,type:'question'|'brick',x,y,w,h,used }];
  flag: { x,y,height,activated,slideOffset };
  camera: { x,y };
  deathTimerMs: number; clearTimerMs: number;
  input: { left,right,jump,run,pause,restart };
}
```

`getLevelSummary()` 返回关卡结构/坐标（**瓦片单位**）：

```ts
{
  widthTiles, heightTiles, tileSize,
  spawn: {x,y},
  groundSegments: [{x,width}],
  pipes: [{x,height}],
  pits: [{x,width}],
  stairs: [{x,levels}],
  questionBlocks: [{x,y}],
  bricks: [{x,y}],
  coins: [{x,y}],
  enemies: [{id,x,y,type}],
  flagpole: {x,y,height}
}
```

### 2.3 数值/坐标/容差基线

| 项目 | 基线 | 容差/断言 |
|---|---|---|
| 瓦片 | 32 px | — |
| 世界/摄像机 | 5760×480；`camera.x∈[0,4800]`，`camera.y=0` | ±1 px |
| 玩家 AABB/出生点 | 24×28；左上角 (84,388)；vx=vy=0、grounded=true | 精确 |
| 走速上限 | 128 px/s（4 瓦片/s） | ±5% |
| 跑速上限 | 224 px/s（7 瓦片/s） | ±5% |
| 松键停止 | `vx` 衰减至 0 | ≤12 固定步（200 ms），\|vx\|≤1 px/s |
| 跳跃 | **固定高度**（短按/按住峰值相同，不做可变跳跃）；峰值 120 px；滞空 750 ms；v0=640 px/s、g=1706.56 px/s²（S2 锁定） | 峰值 ±2 px；滞空 ±50 ms |
| 终端下落速度 | 960 px/s | `vy ≤ 960` |
| 巡逻怪速度 | 48 px/s（1.5 瓦片/s）；AABB 30×30 | ±5%；碰墙反向位置 ±1 px |
| 坑底杀伤线 | `world y > 544` | 边界 ±0.5 px |
| 死亡动画/重生 | 动画 48 步（800 ms）；死亡→可操作 ≤90 步（1500 ms） | 精确步数 |
| 无敌 | 90 步（1500 ms） | 1499 ms 仍无敌；1500 ms 起可受伤 |
| 旗帜下滑 | 60 步（1000 ms）；+1000 只一次 | 精确步数 |
| HUD 更新 | 事件后同一固定步或下一渲染帧 | ≤16.7 ms |
| 完整通关 | 首次 PLAYING 步 → LEVEL_CLEAR | ≤180 s（10800 步） |
| 固定步 | 1/60 s；`frame` 每步 +1 | 精确 |
| 计分 | 金币 +100/+1；踩敌 +200；旗杆 +1000 | 精确 |

### 2.4 输入注入、场景构造与生命周期

| 能力 | 建议接口 | 语义 |
|---|---|---|
| 读取状态 | `getState()` | 按 §2.2 schema 返回不可变快照 |
| 读取关卡摘要 | `getLevelSummary()` | 按 §2.2 schema（瓦片单位） |
| 输入注入 | `setInput({left,right,jump,run})` | 设置按住状态；`jump` 仅在 false→true 边沿触发跳跃；配合 `step(n)` |
| 按键注入（可选等价） | `pressKey(key)` / `releaseKey(key)` | 支持方向/跳跃/Shift；不得绕过正常输入规则 |
| 设置玩家 | `setPlayerState({x?,y?,vx?,vy?,grounded?,facing?,invulnerableMs?})` | 部分更新；提供 x/y 时 vx/vy 默认 0；grounded 未提供时默认 false；不清空输入 |
| 场景构造 | `loadScenario(scenario)` | 设置玩家/敌人/金币/块/旗杆的位置与状态；非法场景应报错；用于 AC-09~AC-16 的确定性构造 |
| 固定步推进 | `step(n)` | 精确推进 n 个固定步；**PAUSED 下 no-op**；返回/可读取新状态 |
| 暂停 | `setPaused(bool)` | 仅 PLAYING↔PAUSED；其他状态 no-op |
| 重置 | `reset()` | 开始全新一局 PLAYING（生命 3/得分 0/金币 0/实体重置/输入清空），等价 R；不回 TITLE |
| 设置无敌 | `setInvulnerable(ms)` | 等价 `setPlayerState({invulnerableMs})` |

- 所有快照 JSON 可序列化、不可变；正常入口 `window.__GAME__ === undefined`。
- 测试钩子只用于测试；生产构建默认不暴露。

---

## 3. 验收项总览

| 分组 | AC | 数量 |
|---|---|---:|
| A. 加载/开始/输入 | AC-01 ~ AC-05 | 5 |
| B. 移动/物理/碰撞 | AC-06 ~ AC-08 | 3 |
| C. 金币/块 | AC-09 ~ AC-11 | 3 |
| D. 敌人/死亡/重生 | AC-12 ~ AC-17, AC-33 | 7 |
| E. 通关/暂停/重开/HUD | AC-18 ~ AC-23 | 6 |
| F. 关卡/确定性/性能/浏览器/合规/交付 | AC-24 ~ AC-32 | 9 |
| **合计** | **AC-01 ~ AC-33** | **33** |

优先级统计：**P0 31 条、P1 1 条、P2 1 条**（见各 AC 标题；统计口径以标题中的 P 级为准）。

---

## 4. A. 加载、开始与输入

### AC-01 页面加载、标题界面与 Canvas 缩放（FR-27、FR-34、FR-39，P0）

- **Given**：应用从静态服务器加载完成，URL 为 `/`（无 `?e2e=1`）。
- **When**：等待首屏可交互并检查 DOM/Canvas。
- **Then**：
  - `screen-title` 可见且文案含「晴空原野」，`start-hint` 显示「Enter / Space 开始」；
  - `game-root` 逻辑 **960×540**；`game-canvas` 逻辑 **960×480**；HUD 为 60px DOM 条（世界坐标 0..480，HUD 不进入世界坐标）；
  - 画面按 **16:9 letterbox** 适配视口（比例 ±0.01），无拉伸变形/裁切；
  - `hud-score`、`hud-coins`、`hud-lives`、`hud-level` 均存在，初始文本分别为 `0`、`0`、`3`、`第一关`；
  - 页面无横向/纵向滚动条（`scrollWidth == clientWidth`、`scrollHeight == clientHeight`）；
  - 无 console error、无 pageerror、无 404；
  - `window.__GAME__` 为 `undefined`。
- **验证方式**：Playwright `tests/e2e/boot.spec.ts`（Chromium/Firefox/WebKit）；人工视觉检查。

### AC-02 开始游戏（Enter/Space）（FR-24、FR-39，P0）

- **Given**：处于 `TITLE`。
- **When**：按 `Enter`（或 `Space`）。
- **Then**：
  - 状态变为 `PLAYING`；`screen-title` 隐藏；
  - 玩家 AABB 左上角在 **(84,388)**（脚底中心 x=96、y=416，站在地面）；`vx=0`、`vy=0`、`grounded=true`、`facing='right'`；
  - 生命 3、得分 0、金币 0、关卡显示 `第一关`（内部 level id `1-1`）；
  - 玩家可立即移动/跳跃；
  - 在 `TITLE` 按其他键（如左右/Shift/R/P）不会开始游戏。
- **验证方式**：Playwright `tests/e2e/start.spec.ts` + 测试钩子 `getState()`；Vitest 状态机测试。

### AC-03 左右移动、停止与页面滚动拦截（FR-02、FR-22、FR-23，P0）

- **Given**：`PLAYING`，玩家在地面，页面处于前台。
- **When**：按住 `ArrowRight`（或 `D`）0.5s 后松开；再按住 `ArrowLeft`（或 `A`）0.5s。
- **Then**：
  - 右移时玩家 x 增大、面朝右；松开后 `vx` 在 **≤12 固定步（200 ms）** 内衰减到 0（\|vx\|≤1 px/s）；
  - 左移对称；
  - `window.scrollX/scrollY` 保持 0，页面不滚动；
  - 按 `ArrowDown` / `Space` 不产生页面滚动；
  - 无按键时玩家不移动。
- **验证方式**：Playwright 真实键盘事件 + `getState()`；Vitest `tests/unit/input.test.ts`。

### AC-04 跑（Shift）（FR-03，P0）

- **Given**：`PLAYING`，前方有足够长的平地。
- **When**：
  1. 按住右方向 1.0s（走）；
  2. 重置到起点，按住 `Shift` + 右方向 1.0s（跑）。
- **Then**：
  - 跑的位移显著大于走（设计目标跑速约 7 瓦片/s、走速约 4 瓦片/s；测试断言跑位移 ≥ 走位移×1.4）；
  - 松开 Shift 后速度回落到走速上限；
  - 跑 + 跳可组合；
  - 页面不滚动。
- **验证方式**：Vitest 固定步物理测试；Playwright `tests/e2e/movement.spec.ts`。

### AC-05 跳跃（FR-04，P0）

- **Given**：`PLAYING`，玩家着地。
- **When**：
  1. 按下 `Space`（短按）；
  2. 在空中再次按 `Space`；
  3. 落地后再次按 `Space`；
  4. 按住 `Space` 不放。
- **Then**：
  - 第 1 次按键触发一次跳跃；玩家离地上升后落回地面；
  - 空中按键不产生二段跳；
  - 落地后按键可再次跳跃；
  - 按住不产生连续自动跳；
  - 跳跃为**固定高度**（短按与按住峰值相同，不做可变跳跃）；峰值 **120 px（3.75 瓦片）±2 px**（脚底上升高度，不是必须落上的平台高度）；滞空 **750 ms ±50 ms**；跑动跳跃可越过 3 瓦片高管道 / 4 瓦片宽坑（按 S2 路线）；所有必须落点 ≤3 瓦片；
  - 跳跃期间页面不滚动。
- **验证方式**：Vitest 固定步（输入序列 → 峰值/落地状态）；Playwright `tests/e2e/jump.spec.ts`。

---

## 5. B. 移动、物理与碰撞

### AC-06 重力、落地与终端速度（FR-05，P0）

- **Given**：通过测试钩子将玩家放在地面上方 3 瓦片处。
- **When**：固定步推进至落地。
- **Then**：
  - 玩家受重力下落；`vy ≤ 960 px/s`（终端速度）；
  - 落到地面顶面时 `vy=0`、`grounded=true`，AABB 位置稳定；
  - 停留在地面时不抖动/不下沉（位置波动 ≤1 px）；
  - 状态保持 `PLAYING`。
- **验证方式**：Vitest `tests/unit/physics.test.ts`。

### AC-07 实心碰撞与无穿透（FR-06，P0）

- **Given**：玩家在地面；前方有 1 瓦片厚实心墙/管道；上方有砖块/问号块；管道可站立。
- **When**：
  1. 以最大跑速撞向墙/管道；
  2. 跳到块下方顶击；
  3. 跳到管道顶部站立。
- **Then**：
  - 玩家在墙/管道前停止，不穿透（AABB 不进入实心区域）；
  - 顶击时垂直速度归零，玩家不穿过块；
  - 玩家可站在管道顶部，`grounded=true`；
  - 固定步/最大速度下无隧穿；
  - 碰撞后位置/速度稳定，无抖动。
- **验证方式**：Vitest `tests/unit/collision.test.ts`（含高速反例）；Playwright 场景断言。

### AC-08 摄像机跟随与夹紧（FR-21，P0）

- **Given**：`PLAYING`，玩家从起点向右移动；关卡宽 180 瓦片。
- **When**：玩家移动；并用测试钩子把玩家放到关卡末端附近。
- **Then**：
  - `camera.x = clamp(playerCenterX − 480, 0, 4800)`（±1 px；若 S2 采用死区，以 S2 接口契约公式为准）；
  - 摄像机 x 夹紧在 `[0, 4800]`，不越界；`camera.y=0`，无纵向滚动；
  - 玩家在屏幕内可见；夹紧到 4800 时旗杆屏幕 x≈704 px 且可见。
- **验证方式**：Vitest camera 测试；Playwright `getState().camera`。

---

## 6. C. 金币与块

### AC-09 金币收集（FR-07、FR-20，P0）

- **Given**：场景中存在一枚金币，玩家在其附近。
- **When**：玩家 AABB 与金币重叠。
- **Then**：
  - 金币计数 +1；得分 +100；
  - 金币从场景移除；
  - `hud-coins` / `hud-score` 在同一固定步或下一渲染帧（≤16.7 ms）更新；
  - 金币不阻挡玩家移动；
  - 同一金币不能重复收集。
- **验证方式**：Vitest `tests/unit/collectibles.test.ts`；Playwright 断言 HUD/状态。

### AC-10 问号块（FR-08、FR-20，P0）

- **Given**：玩家着地，正上方有一个问号块。
- **When**：跳跃从下方顶击该块。
- **Then**：
  - 产出 1 枚金币：金币 +1、得分 +100；
  - 播放金币弹出动画；
  - 块变为「已用块」：外观变化、仍实心、不可再次产出；
  - 再次顶击不增加金币/得分；已用块仍为实心；
  - 顶击后玩家 `vy=0`；HUD 在同一固定步/下一帧更新。
- **验证方式**：Vitest `tests/unit/blocks.test.ts`；Playwright 场景。

### AC-11 砖块（FR-09，P0）

- **Given**：玩家着地，正上方有一个砖块。
- **When**：跳跃从下方顶击该块。
- **Then**：
  - 砖块播放弹动动画；
  - 不破碎、不产出金币、不得分；
  - 从上方/侧面同样按实心处理，玩家不能穿过；
  - 顶击后玩家 `vy=0`。
- **验证方式**：Vitest `tests/unit/blocks.test.ts`；Playwright 场景。

---

## 7. D. 敌人、死亡与重生

### AC-12 巡逻怪行为（FR-10，P0）

- **Given**：平地上有一只巡逻怪；另一只在坑边。
- **When**：固定步推进。
- **Then**：
  - 敌人以 **48 px/s（1.5 瓦片/s）±5%** 水平巡逻；
  - 碰到实心墙/关卡边界反向（位置容差 ±1 px）；
  - 坑边敌人掉入坑后被移除（不反弹）；
  - 敌人不穿墙；
  - 同一敌人只能被击败一次。
- **验证方式**：Vitest `tests/unit/enemies.test.ts`。

### AC-13 踩敌（FR-11、FR-20，P0）

- **Given**：玩家位于巡逻怪上方，正在下落；玩家未死亡。
- **When**：玩家脚部与敌人顶部接触。
- **Then**：
  - 敌人被击败（压扁动画后移除）；
  - 玩家获得向上反弹；
  - 得分 +200；生命不变；
  - `hud-score` 更新；
  - 被击败的敌人不再参与碰撞。
- **同帧判定**：见 AC-33（踩踏优先、不重复结算、无敌期有效）。
- **验证方式**：Vitest `tests/unit/enemies.test.ts`；Playwright 场景。

### AC-14 侧面接触死亡（FR-12、FR-14，P0）

- **Given**：玩家与巡逻怪在地面上相邻；玩家非无敌。
- **When**：玩家向敌人侧面移动并接触。
- **Then**：
  - 进入 `DYING`；生命 3→2；
  - 播放死亡动画（48 步 / 800 ms）；得分/金币不变；
  - 死亡流程结束后若生命>0，玩家回到起点；
  - 得分/金币保留；
  - 死亡后本固定步剩余的玩家-敌人碰撞不再结算。
- **验证方式**：Vitest `tests/unit/death.test.ts`；Playwright 场景。

### AC-15 掉坑死亡（FR-13、FR-14，P0）

- **Given**：玩家位于坑上方或坑边。
- **When**：玩家掉到 `world y > 544`（关卡高度 480 + 2 瓦片；边界 ±0.5 px）。
- **Then**：
  - 进入 `DYING`；生命 -1；
  - 生命>0 时重生回起点；得分/金币保留；
  - 关卡实体重置；
  - 不出现无限下落或卡死。
- **验证方式**：Vitest `tests/unit/death.test.ts`；Playwright 通过测试钩子放置玩家。

### AC-16 重生与无敌（FR-14，P0）

- **Given**：玩家死亡且生命>0。
- **When**：死亡动画结束。
- **Then**：
  - 玩家在起点重生；生命已递减；
  - 无敌 **90 固定步（1500 ms）**，闪烁；1499 ms 时仍无敌，1500 ms 起可受伤；
  - 无敌期间侧碰敌人不死亡；踩敌仍有效（敌人被击败）；
  - 无敌结束后侧碰敌人恢复正常死亡判定；
  - 敌人/金币/问号块状态重置到关卡初始；
  - 得分/金币保留；
  - 从死亡到可操作 ≤90 固定步（1500 ms）。
- **验证方式**：Vitest 固定步 + 测试钩子 `setInvulnerable`；Playwright。

### AC-17 Game Over 与 R 重开（FR-15、FR-18，P0）

- **Given**：生命 = 1；玩家死亡。
- **When**：死亡流程完成。
- **Then**：
  - 状态 `GAME_OVER`；`screen-gameover` 可见，显示最终得分/金币；
  - 移动/跳跃/暂停输入被忽略；
  - 按 `R`：状态 `PLAYING`，生命 3、得分 0、金币 0、玩家回起点、实体重置；
  - 无死亡动画/覆盖层残留。
- **验证方式**：Vitest 状态机；Playwright `tests/e2e/gameover.spec.ts`。

### AC-33 同帧踩踏优先（FR-11、FR-12，P0）

- **Given**：玩家下落（`vy>0`），同一固定步内玩家 AABB 同时与敌人顶部和侧面重叠。
- **When**：`step(1)` 结算。
- **Then**：
  - 按踩踏优先：敌人被击败、得分 +200、玩家向上反弹、玩家不死亡；
  - 同帧只结算一次，不重复加分；
  - 本步剩余敌人碰撞不再结算；
  - 无敌期间同样有效（踩敌仍击败敌人）。
- **验证方式**：Vitest `tests/unit/enemies.test.ts`（同帧正例/反例）；Playwright 用 `setPlayerState` 将玩家置于敌人上缘且水平重叠，`step(1)` 后断言。

---

## 8. E. 通关、暂停、重开与 HUD

### AC-18 旗杆通关（FR-16、FR-18，P0）

- **Given**：`PLAYING`，玩家接近旗杆（测试钩子可传送到旗杆附近以缩短路径）。
- **When**：玩家碰到旗杆杆体。
- **Then**：
  - 得分 +1000；
  - 状态 `LEVEL_CLEAR`；`screen-clear` 可见，显示得分/金币/生命；
  - 玩家输入锁定；播放旗帜下滑动画（约 1s）；
  - 按 `R` 开始全新一局（生命 3/得分 0/金币 0）；
  - 不进入下一关。
- **验证方式**：Vitest flagpole 测试；Playwright `tests/e2e/clear.spec.ts`。

### AC-19 暂停/继续（FR-17，P0）

- **Given**：`PLAYING`，玩家/敌人正在移动。
- **When**：按 `P` 或 `Escape`；再按同一键。
- **Then**：
  - 状态 `PAUSED`；`screen-pause` 可见；
  - 玩家/敌人的位置、速度、动画、`frame`、`fixedStepAccumulator` 全部冻结；
  - 暂停 10s（不推进）后恢复，`step(1)` 只推进一步，无补帧跳变；
  - 按住方向/跳跃不产生移动；
  - 得分/生命/金币不变；
  - 再次按键恢复 `PLAYING`，从冻结点继续；
  - `P`/`Esc` 仅在 `PLAYING`/`PAUSED` 生效，在 `DYING`/`GAME_OVER`/`LEVEL_CLEAR` 被忽略；
  - 暂停中按 `R` 直接重开；暂停覆盖层不阻塞全局键盘。
- **验证方式**：Vitest `tests/unit/state.test.ts`（固定步）；Playwright。

### AC-20 失焦自动暂停与清键（FR-19、FR-23，P0）

- **Given**：`PLAYING`，按住右方向键（玩家正在移动）。
- **When**：用 Playwright 派发 synthetic `blur` + `visibilitychange`（必要时覆盖 `document.hidden`/`visibilityState`，或用 CDP/context 失焦）模拟窗口失焦/页面隐藏。
- **Then**：
  - 自动进入 `PAUSED`；`screen-pause` 可见；
  - 所有按键状态清空；`getState().input` 全为 false；玩家停止，不出现“卡住持续移动”；
  - 失焦不改变生命/得分/金币/位置/敌人状态；
  - 回到前台后保持 `PAUSED`，不自动恢复；恢复后按键需重新按下；
  - 失焦在 `TITLE`/`PAUSED`/`DYING`/`GAME_OVER`/`LEVEL_CLEAR` 不改变状态；
  - 跳跃/下落中失焦同样暂停，恢复后需重新按键；
  - 按 `P`/`Esc` 恢复，按 `R` 重开。
- **验证方式**：Playwright `page.evaluate` 触发 synthetic blur/visibilitychange（或 CDP/context 失焦）；Vitest input reset。

### AC-21 R 重开（各状态）（FR-18、FR-24，P0）

- **Given**：分别处于 `PLAYING`、`PAUSED`、`DYING`、`GAME_OVER`、`LEVEL_CLEAR`。
- **When**：按 `R`（无 Ctrl/Meta/Alt）。
- **Then**：
  - 进入 `PLAYING`；
  - 生命 3、得分 0、金币 0、玩家回起点、关卡实体重置；
  - 覆盖层隐藏；无暂停/死亡/通关残留；
  - `TITLE` 下按 `R` 不生效（需 Enter/Space 开始）；`Ctrl`/`Meta`/`Alt`+R 不触发游戏重开（浏览器快捷键保留）。
- **验证方式**：Vitest 状态机；Playwright `tests/e2e/restart.spec.ts`。

### AC-22 HUD（FR-20、FR-33，P0）

- **Given**：应用已加载 / `PLAYING`。
- **When**：依次触发金币、踩敌、受伤/死亡、通关等事件。
- **Then**：
  - `hud-score`、`hud-coins`、`hud-lives`、`hud-level` 始终存在；
  - 初始为 `0`、`0`、`3`、`第一关`；
  - 金币 +1/+100、踩敌 +200、旗杆 +1000、死亡生命 -1 后 HUD 在同一固定步或下一渲染帧（≤16.7 ms）更新；
  - `PLAYING`/`PAUSED`/`DYING`/`GAME_OVER`/`LEVEL_CLEAR` 下 HUD 可见；
  - HUD 位于顶部 60px，不遮挡 960×480 游戏区的关键操作。
- **验证方式**：Playwright `tests/e2e/hud.spec.ts`；Vitest 计分/HUD 状态。

### AC-23 键盘映射完整、无冲突与修饰键（FR-22、FR-23，P0）

- **Given**：`TITLE` / `PLAYING` / `PAUSED` / `GAME_OVER` / `LEVEL_CLEAR`。
- **When**：逐一测试：
  - `ArrowLeft`/`A`、`ArrowRight`/`D`；
  - `Space`/`W`/`ArrowUp`；
  - `ShiftLeft`/`ShiftRight`；
  - `P`/`Escape`；
  - `R`；
  - `Enter`/`Space`（TITLE）。
- **Then**：
  - 每个键在对应状态下只触发一个动作，无冲突；
  - `Space` 在 TITLE 开始、在 PLAYING 跳跃（按状态区分）；
  - `P`/`Esc` 仅在 PLAYING/PAUSED 生效，在 DYING/GAME_OVER/LEVEL_CLEAR 被忽略；
  - Shift+方向/跳跃可组合；
  - 带 `Ctrl`/`Meta`/`Alt` 的组合键不被游戏拦截（例如 Ctrl+R 不触发游戏重开；浏览器快捷键保持可用）；
  - `event.repeat` 不触发跳跃/暂停/重开/开始的重复动作；
  - 方向键/空格不滚动页面；
  - 失焦恢复后所有键需重新按下（见 AC-20）。
- **验证方式**：Vitest `tests/unit/input.test.ts`（合成事件）；Playwright 真实键盘。

---

## 9. F. 关卡、确定性、性能、浏览器、合规与交付

### AC-24 关卡结构与可通关（FR-01、FR-25、FR-36、FR-37、FR-38，P0）

- **Given**：关卡数据已加载。
- **When**：通过测试钩子读取关卡摘要并实际游玩。
- **Then**：
  - 关卡为 180×15 瓦片；视口 30×15；
  - 管道 ≥2（高 2~3 瓦片）；问号块 ≥3；砖块 ≥2 行；坑 ≥2（每个 3~4 瓦片宽）；楼梯 ≥1 段（≥4 级）；巡逻怪 ≥8；金币 ≥24；旗杆 1；
  - 所有必须落点（管道顶/楼梯/平台）≤3 瓦片；4 瓦片宽坑在跑跳下有余量（S2 给出坑前助跑距离）；
  - S2 示例预算（designer 确认）：3 根管道（2/3/2 高）、4 个问号块、2 行砖块、2 个 4 瓦片宽坑、1 段 4 级楼梯、8 个巡逻怪、26 枚场景金币、1 根旗杆；
  - 起始区安全：出生点附近 ≥8 瓦片内无巡逻怪/坑；旗杆位于末端且可到达；
  - 存在不使用调试传送的可通关路径；S2 给出关键跳跃点/坑前助跑距离/输入路线；S6/S9 实测通关；
  - S1 跳跃包络：峰值 120 px、滞空 750 ms、跑速 224 px/s；跑跳水平距离约 5.25 瓦片；
  - 关卡不包含计时器、检查点、多关卡、道具/变大/火球/龟壳/管道进入/移动平台/Boss；
  - 关卡可在 ≤180 s（10800 固定步，从首次 PLAYING 步到 LEVEL_CLEAR）内完成。
- **验证方式**：Vitest `tests/unit/level.test.ts`（摘要/数量/schema）；Playwright `tests/e2e/full-run.spec.ts`；人工通关。

### AC-25 关卡数据 schema 校验（FR-25，P1）

- **Given**：非法关卡数据（缺字段、坐标越界、宽度/高度非法、缺失出生点/旗杆、重复 ID 等）。
- **When**：加载/校验关卡。
- **Then**：
  - 校验失败并给出可读错误对象 `{code,file,field,expected,actual}`（Vitest 用非法 fixture 断言）；
  - 应用不进入 `PLAYING`，不静默生成空关卡；
  - 合法关卡数据通过校验。
- **验证方式**：Vitest `tests/unit/level-schema.test.ts`。

### AC-26 确定性与测试钩子（FR-31、FR-32，P0）

- **Given**：`?e2e=1`；相同初始状态与相同输入序列。
- **When**：以固定步运行两次，并检查测试钩子。
- **Then**：
  - 两次运行的状态快照（玩家位置/速度、敌人、金币、块、得分/生命/状态）完全一致；
  - `window.__GAME__` 按 §2.2/§2.4 提供 `getState()`（完整 schema）、`getLevelSummary()`（坐标/范围）、`setInput()`、`setPlayerState()`、`loadScenario()`、`step(n)`、`setPaused()`、`reset()`、`setInvulnerable()`；
  - 相同输入序列（`setInput` + `step`）两次运行的状态快照完全一致；
  - `step(n)` 在 `PAUSED` 下 no-op；`reset()` 进入全新 `PLAYING`（生命 3/得分 0/金币 0/实体重置/输入清空）；
  - 快照 JSON 可序列化、不可变；
  - 正常入口（无 `?e2e=1`）`window.__GAME__` 不存在；
  - 测试钩子不改变正常游玩行为。
- **验证方式**：Vitest `tests/unit/determinism.test.ts`；Playwright `tests/e2e/test-hook.spec.ts`。

### AC-27 性能目标（FR-30，P0）

- **Given**：生产构建 `dist/` 由本地静态服务托管；Chromium。
- **When**：运行 60s 脚本化移动/跳跃路线，测量帧率、输入延迟、首屏与内存。
- **Then**：
  - 用 `requestAnimationFrame` 时间戳统计 60s；帧间隔 >25ms 记为掉帧，掉帧率 ≤5%，平均约 60fps；
  - 页面内 `performance.now()` 从 keydown 到首次状态/画面更新 <50ms；
  - 首屏可交互 <2s；
  - Chromium 下用 `performance.memory`/heap 采样，5 分钟内无持续增长/泄漏；
  - 无 console error / pageerror / 404；
  - `PerformanceObserver` 长任务 >100ms 为 0（或记录并解释）；
  - 性能门禁仅在 Chromium；Firefox/WebKit 做功能 E2E + 手工冒烟（避免 CI 抖动当 P0）。
- **验证方式**：Playwright 性能测试/trace + `requestAnimationFrame` 计数；手工性能检查。

### AC-28 三浏览器兼容（FR-29、FR-34，P0）

- **Given**：Playwright 配置 Chromium、Firefox、WebKit；手工环境具备 Chrome/Edge/Firefox/Safari 最近两个大版本。
- **When**：运行完整 E2E 套件并手工冒烟。
- **Then**：
  - 三个引擎的功能 E2E 全部通过；性能门禁仅在 Chromium（AC-27），Firefox/WebKit 以功能 + 手工冒烟；
  - Chrome/Edge/Firefox/Safari 上均可：开始、移动、跑、跳、金币/块、踩敌、死亡/重生、暂停/失焦、通关、重开；
  - Canvas 缩放/letterbox 正常，无变形/裁切；
  - 键盘映射与页面滚动拦截一致；
  - 无仅在某浏览器出现的 P0/P1。
- **验证方式**：Playwright 多项目；手工冒烟记录（浏览器/版本/日期/结果）。

### AC-29 素材合规（FR-26、FR-28，P0）

- **Given**：源码与 `dist/`。
- **When**：检查资源、网络请求、UI 文案与许可声明。
- **Then**：
  - 不包含任天堂角色形象、名称、标志、音乐、音效或可识别复刻素材；
  - 所有美术为原创程序化绘制或明确许可素材；提供 `assets-manifest.json`（每个资源来源/许可/作者），无来源不明资源；
  - MVP 无音频/音乐；如后续加入，必须有原创/许可证明；
  - 发布显示名为原创「跳跃冒险：晴空原野」；未在发布物中使用 `世界 1-1`/`World 1-1` 作为产品名（内部 level id `1-1` 除外）；未使用 `Mario`/`Luigi`/`Goomba`/`Koopa`/`Nintendo` 等名称；
  - 运行时无外部网络请求（无 CDN/远程字体/远程图片音频）；
  - S2 交付「原创性对照表」；README 含素材来源与许可声明；发布前对源码/dist/素材做 `Mario`/`Luigi`/`Goomba`/`Koopa`/`Nintendo` 关键字扫描（0 命中；`1-1` 作为内部 level id 不计入扫描）。
- **验证方式**：代码/素材审计；Playwright 网络记录；关键字扫描；人工检查。

### AC-30 构建、脚本与交付形态（FR-27、FR-28、FR-35，P0）

- **Given**：干净检出，Node ≥20、npm ≥10。
- **When**：依次执行：
  ```bash
  npm ci
  npm run build
  npm test
  npm run test:e2e
  npm run preview
  ```
- **Then**：
  - 所有命令退出码为 0；
  - `npm run build` 生成 `dist/`（含 `dist/index.html` 与静态资源）；
  - `npm run preview` 可访问，页面可玩；
  - 无后端、无数据库、无环境变量依赖；`dist` 为纯静态；
  - favicon 为 data URI 或随包资源，无 `/favicon.ico` 404；
  - README 覆盖安装/运行/构建/测试/键位/玩法/浏览器/限制/素材许可。
- **验证方式**：命令执行记录；Playwright 网络/控制台断言；文档检查。

### AC-31 完整通关（无传送路径）（FR-01、FR-36，P0）

- **Given**：从 `TITLE` 开始，使用真实键盘输入（测试钩子仅用于读取状态/校验，不用于传送玩家）；人工键盘完整通关为 P0 证据。
- **When**：执行人工通关路线（或 S2 给出关键输入后以 `setInput` 回放；脚本化路线为 P1 增强）：跑/跳越过管道与坑、踩敌/躲避、上楼梯、到达旗杆。
- **Then**：
  - 在 ≤180 s（10800 步，从首次 PLAYING 步起算）内到达旗杆并进入 `LEVEL_CLEAR`；
  - 路线不需要调试传送或修改关卡；
  - 期间的得分/金币/生命变化与规则一致；
  - 无 console error / 卡死 / 非法状态；
  - 至少 1 次人工键盘完整通关记录。
- **验证方式**：人工键盘通关记录（P0）；Playwright `tests/e2e/full-run.spec.ts`（`setInput` 回放，P1 增强）；S2 给出关键跳跃点/路线。

### AC-32 覆盖层、文案与基础可访问性（FR-33、FR-40，P2）

- **Given**：`TITLE` / `PAUSED` / `GAME_OVER` / `LEVEL_CLEAR`。
- **When**：检查覆盖层与文案。
- **Then**：
  - `screen-title`、`screen-pause`、`screen-gameover`、`screen-clear` 均存在且稳定；
  - 文案为中文，无英文占位/TODO；
  - 每个覆盖层给出对应按键提示（开始/继续/重开）；
  - Canvas 具有可读的 `aria-label`（如「游戏画面」），HUD 文本可读；
  - 覆盖层不阻塞全局键盘监听；
  - 页面无 alert/confirm 等阻塞式弹窗。
- **验证方式**：Playwright DOM 断言；人工检查。

---

## 10. FR → AC 追踪矩阵

| FR | AC |
|---|---|
| FR-01 | AC-24, AC-31 |
| FR-02 | AC-03 |
| FR-03 | AC-04 |
| FR-04 | AC-05 |
| FR-05 | AC-06 |
| FR-06 | AC-07 |
| FR-07 | AC-09 |
| FR-08 | AC-10 |
| FR-09 | AC-11 |
| FR-10 | AC-12 |
| FR-11 | AC-13, AC-33 |
| FR-12 | AC-14, AC-33 |
| FR-13 | AC-15 |
| FR-14 | AC-16 |
| FR-15 | AC-17 |
| FR-16 | AC-18 |
| FR-17 | AC-19 |
| FR-18 | AC-21 |
| FR-19 | AC-20 |
| FR-20 | AC-22 |
| FR-21 | AC-08 |
| FR-22 | AC-23 |
| FR-23 | AC-03, AC-20, AC-23 |
| FR-24 | AC-02, AC-21 |
| FR-25 | AC-24, AC-25 |
| FR-26 | AC-29 |
| FR-27 | AC-01, AC-30 |
| FR-28 | AC-29, AC-30 |
| FR-29 | AC-28 |
| FR-30 | AC-27 |
| FR-31 | AC-26 |
| FR-32 | AC-26 |
| FR-33 | AC-22, AC-32 |
| FR-34 | AC-01, AC-28 |
| FR-35 | AC-30 |
| FR-36 | AC-24, AC-31 |
| FR-37 | AC-24 |
| FR-38 | AC-24 |
| FR-39 | AC-01, AC-02, AC-23 |
| FR-40 | AC-32 |

> 覆盖检查：FR-01 ~ FR-40 共 40 条，全部至少映射到 1 条 AC；无空项。

---

## 11. 验收出口与回归要求

### 11.1 发布出口准则

1. 全部 **P0** 验收项通过。
2. **P1** 验收项通过，或未通过项已由 PM/人类书面接受并登记为遗留。
3. **P2** 由 PM 在发布评审中决定接受/延期。
4. 单元测试（`npm test`）与 E2E（`npm run test:e2e`）全部通过（Chromium/Firefox/WebKit）。
5. 无未关闭 P0/P1；无碰撞穿透、无法通关、键盘失控、崩溃。
6. 性能目标（AC-27）与素材合规（AC-29）通过。
7. 三浏览器手工冒烟完成并记录。
8. 仅 PM 在 S10 可给出发布结论；存在未关闭 P0/P1、验收项不通过或关键风险未被接受时，必须升级 human。

### 11.2 回归要求

- 任何 P0 修复后：重跑相关 AC + AC-07（碰撞无穿透）+ AC-24/AC-31（可通关）+ AC-27/AC-28（性能/浏览器）+ 全量 unit/E2E。
- 物理/碰撞改动后：重跑 AC-05~AC-08、AC-12~AC-16（含同帧踩踏优先 AC-33）。
- 输入/状态机改动后：重跑 AC-02~AC-05、AC-19~AC-23、AC-26（含测试钩子 schema/确定性）。
- 测试钩子 schema 变更后：重跑 AC-26 + 所有依赖钩子的 P0 AC（AC-09~AC-16、AC-19、AC-24、AC-31）。
- 关卡/渲染改动后：重跑 AC-01、AC-08、AC-24、AC-27、AC-28、AC-31。
- 同类 P0 连续两个修复周期未关闭：按工作流 `@cindy` / 升级 human。

### 11.3 验证记录模板（tester 填写）

| AC | 结果（通过/失败/阻塞） | 证据（命令/截图/日志） | 缺陷 ID | 备注 |
|---|---|---|---|---|
| AC-01 |  |  |  |  |
| AC-02 |  |  |  |  |
| … |  |  |  |  |
| AC-32 |  |  |  |  |

> 完整测试报告由 tester 输出到工作流约定的报告文件（如 `docs/test-report.md`），本表仅作模板。
