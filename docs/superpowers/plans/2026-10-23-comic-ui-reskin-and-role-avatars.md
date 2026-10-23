# 漫画工坊 UI 装修 + 角色头像 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Anvil 前端统一成「漫画工坊 / Ink & Paper」视觉，并让角色在创建时可选、创建后可改卡通头像。

**Architecture:** 先在后端给 `roles` 增加 `avatar` 字段和独立更新接口；前端新增 `RoleAvatar` / `AvatarPicker` 组件和本地头像素材；随后把旧 `style.css` 拆成 tokens / base / pages / components / layout 五层，先保留旧页面规则作为兼容底座，再用 components / layout 覆盖成新视觉；最后逐个页面调整结构（壳层、任务大厅、任务详情、角色工坊、配置表格页）并做响应式与可访问性收尾。

**Tech Stack:** React 18 + TypeScript + Vite，FastAPI + SQLite，纯 CSS，本地 SVG 头像，不新增 npm 依赖。

**Spec:** `docs/superpowers/specs/2026-10-23-comic-ui-reskin-and-role-avatars-design.md`

---

## 文件地图

**新增：**

| 文件 | 职责 |
| --- | --- |
| `dev/frontend/src/styles/tokens.css` | 颜色、间距、圆角、阴影、字体 token；兼容旧变量名 |
| `dev/frontend/src/styles/base.css` | reset、body 背景、排版、滚动条、焦点、reduced-motion |
| `dev/frontend/src/styles/pages.css` | 旧页面规则兼容底座（从旧 `style.css` 机械迁移） |
| `dev/frontend/src/styles/components.css` | 新组件视觉：按钮、输入、徽章、卡片、表格、弹窗、头像、空态 |
| `dev/frontend/src/styles/layout.css` | app shell、侧栏、页头、面板、响应式 |
| `dev/frontend/src/components/Icon.tsx` | Lucide 路径内联图标组件 |
| `dev/frontend/src/components/ui.tsx` | PageHeader / SectionHeader / EmptyState / StatusPill |
| `dev/frontend/src/components/avatarPresets.ts` | 15 个头像预设、默认映射、src 解析 |
| `dev/frontend/src/components/RoleAvatar.tsx` | 全站统一的角色头像渲染 |
| `dev/frontend/src/components/AvatarPicker.tsx` | 内联/弹窗两种头像选择器 |
| `dev/frontend/src/avatarColor.ts` | 共享首字母兜底颜色 |
| `dev/frontend/public/avatars/big-smile/*.svg` | 15 个本地头像素材 |
| `dev/frontend/public/avatars/CREDITS.md` | CC BY 4.0 署名 |
| `dev/tests/api/test_role_avatar.py` | 头像创建/更新/迁移测试 |

**修改：**

| 文件 | 改动 |
| --- | --- |
| `dev/backend/anvil/db.py` | roles 表加 `avatar` 列 + 迁移 |
| `dev/backend/anvil/schemas.py` | RoleIn / RoleOut 加 `avatar` |
| `dev/backend/anvil/services/roles.py` | create 写入 avatar；新增 update_avatar |
| `dev/backend/anvil/api/roles.py` | 创建/自动创建/更新支持 avatar；新增 PUT avatar 端点 |
| `dev/frontend/src/main.tsx` | 改为导入五层样式 |
| `dev/frontend/src/App.tsx` | 导航换 Icon，壳层 class 保持 |
| `dev/frontend/src/api/client.ts` | Role 类型 + 三个头像相关方法 |
| `dev/frontend/src/pages/Lobby.tsx` | PageHeader、卡片结构、RoleAvatar、空列 |
| `dev/frontend/src/pages/TaskDetail.tsx` | 任务单头部、线程气泡、系统事件、输入条、任务档案 |
| `dev/frontend/src/pages/Roles.tsx` | 创建面板 + 头像选择 + 花名册 + 详情头部 |
| `dev/frontend/src/pages/NewTaskModal.tsx` | 成员列表 RoleAvatar |
| `dev/frontend/src/pages/RoleSessions.tsx` | 角色行 RoleAvatar |
| `dev/frontend/src/pages/AgentSessions.tsx` | 角色头部 RoleAvatar |
| `dev/frontend/src/pages/Runtimes.tsx` 等 | PageHeader + 表格/工具栏统一样式 |
| `dev/frontend/src/style.css` | 删除（内容迁入 styles/） |

---

### Task 1: 后端 avatar 列 + 创建链路

**Files:**
- Modify: `dev/backend/anvil/db.py:22-38`（roles 表定义）与 `init_schema` 迁移区
- Modify: `dev/backend/anvil/schemas.py:72-101`
- Modify: `dev/backend/anvil/services/roles.py:45-66`
- Modify: `dev/backend/anvil/api/roles.py:95-122`（auto）与 `193-222`（create/update）
- Test: `dev/tests/api/test_role_avatar.py`

- [ ] **Step 1: 写失败的测试**

创建 `dev/tests/api/test_role_avatar.py`：

```python
"""角色头像：创建持久化、独立更新、校验与旧库迁移。"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, role_payload, runtime_payload  # noqa: E402

GEN = {
    "display_name": "头像测试角色",
    "description": "测试头像",
    "working_mode": "自主",
    "skill_description": "测试",
    "capabilities": {},
    "responsibilities": ["测试"],
    "system_prompt": "你是测试角色。",
}


async def test_create_role_with_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    r = await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:ace"),
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "big-smile:ace"

    got = await client.get(f"{API}/roles/dev")
    assert got.status_code == 200
    assert got.json()["avatar"] == "big-smile:ace"
    assert got.json()["definition"]["avatar"] == "big-smile:ace"


async def test_auto_create_role_with_avatar(client, monkeypatch):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    monkeypatch.setattr(
        "anvil.api.roles.utility.generate",
        lambda conn, prompt, **kwargs: json.dumps(GEN, ensure_ascii=False),
    )
    r = await client.post(
        f"{API}/roles/auto",
        json={"brief": "头像测试", "avatar": "big-smile:bee"},
        headers=human_headers(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "big-smile:bee"


def test_legacy_roles_table_gets_avatar_column(tmp_path):
    from anvil.db import connect

    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE roles ("
        "slug TEXT PRIMARY KEY, display_name TEXT NOT NULL, avatar_color TEXT, "
        "home_path TEXT NOT NULL, definition_json TEXT NOT NULL DEFAULT '{}', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1)"
    )
    raw.commit()
    raw.close()

    conn = connect(str(path))
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(roles)")}
        assert "avatar" in cols
    finally:
        conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd dev
source .venv/bin/activate
python -m pytest tests/api/test_role_avatar.py::test_create_role_with_avatar -v
```

Expected: FAIL —— 响应里没有 `avatar`，或 `PRAGMA table_info` 里没有 `avatar`。

- [ ] **Step 3: 实现最小代码**

`dev/backend/anvil/db.py` 的 `CREATE TABLE IF NOT EXISTS roles` 中，在 `avatar_color TEXT,` 后加：

```sql
    avatar          TEXT,
```

在同文件 `init_schema` 的迁移区（其他 `ALTER TABLE roles` 附近）加：

```python
    # 迁移：roles 补 avatar 列（角色头像预设；NULL = 按 slug 自动匹配）
    try:
        conn.execute("ALTER TABLE roles ADD COLUMN avatar TEXT")
    except sqlite3.OperationalError:
        pass
```

`dev/backend/anvil/schemas.py` 的 `RoleIn` 和 `RoleOut` 中，在 `avatar_color` 后加：

```python
    avatar: str | None = None
```

`dev/backend/anvil/services/roles.py` 的 `create` 中，把 INSERT 改成：

```python
    conn.execute(
        """INSERT INTO roles (slug, display_name, avatar_color, avatar, home_path,
                              definition_json, created_at, updated_at, enabled)
           VALUES (?,?,?,?,?,?,?,?,1)
           ON CONFLICT(slug) DO UPDATE SET
             display_name=excluded.display_name,
             avatar_color=excluded.avatar_color,
             avatar=excluded.avatar,
             definition_json=excluded.definition_json,
             updated_at=excluded.updated_at""",
        (slug, display_name, definition.get("avatar_color"), definition.get("avatar"),
         str(home), jdumps(definition), now, now),
    )
```

在 `_row` 的 `d["definition"] = jloads(...)` 后加一层兼容：列没有值时回退到 definition 里镜像的 avatar。

```python
    if not d.get("avatar"):
        definition = d.get("definition") or {}
        d["avatar"] = definition.get("avatar") if isinstance(definition, dict) else None
```

`dev/backend/anvil/api/roles.py` 的 `auto_create_role` 中，在 `brief` 校验后加：

```python
    avatar = _validate_avatar(body.get("avatar"))
```

并在 `definition = { ... }` 里加：

```python
        "avatar": avatar,
```

`create_role` 中，在 `definition.setdefault("kind", "member")` 后加：

```python
    definition["avatar"] = _validate_avatar(body.avatar)
```

并在文件顶部（`_extract_json` 前）加：

```python
AVATAR_RE = re.compile(r"^big-smile:[a-z0-9-]{1,32}$")


def _validate_avatar(value: object) -> str | None:
    """头像只接受 null / initials / big-smile:<id>。"""
    if value is None or value == "initials":
        return value  # type: ignore[return-value]
    if not isinstance(value, str) or not AVATAR_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail="avatar must be null, 'initials', or 'big-smile:<id>'",
        )
    return value
```

`update_role` 中，在 `definition.pop("display_name", None)` 后加：

```python
    # 头像以 roles.avatar 列为准；纯头像更新走 PUT /roles/{slug}/avatar，
    # 避免通用 PUT 写入与列不一致的 definition 值。
    definition.pop("avatar", None)
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
cd dev
source .venv/bin/activate
python -m pytest tests/api/test_role_avatar.py::test_create_role_with_avatar tests/api/test_role_avatar.py::test_auto_create_role_with_avatar tests/api/test_role_avatar.py::test_legacy_roles_table_gets_avatar_column -v
```

Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
git add dev/backend/anvil/db.py dev/backend/anvil/schemas.py dev/backend/anvil/services/roles.py dev/backend/anvil/api/roles.py dev/tests/api/test_role_avatar.py
git commit -m "feat(roles): persist role avatar on create

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 头像独立更新接口 + 校验测试

**Files:**
- Modify: `dev/backend/anvil/services/roles.py`（新增 `update_avatar`；`update` 同步 avatar 列）
- Modify: `dev/backend/anvil/api/roles.py`（新增 `PUT /roles/{slug}/avatar`；`clone_role` 复制头像）
- Test: `dev/tests/api/test_role_avatar.py`

- [ ] **Step 1: 追加失败的测试**

在 `dev/tests/api/test_role_avatar.py` 末尾追加：

```python
async def test_update_role_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "big-smile:cid"},
        headers=human_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == "big-smile:cid"

    got = await client.get(f"{API}/roles/dev")
    assert got.json()["avatar"] == "big-smile:cid"


async def test_avatar_accepts_initials_and_null(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar", json={"avatar": "initials"}, headers=human_headers(),
    )
    assert r.status_code == 200
    assert r.json()["avatar"] == "initials"

    r = await client.put(
        f"{API}/roles/dev/avatar", json={"avatar": None}, headers=human_headers(),
    )
    assert r.status_code == 200
    assert r.json()["avatar"] is None


async def test_invalid_avatar_is_rejected(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "javascript:alert(1)"},
        headers=human_headers(),
    )
    assert r.status_code == 422


async def test_avatar_update_requires_human(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload("dev"), headers=human_headers())

    r = await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "big-smile:ace"},
        headers=auth_headers(role="developer"),
    )
    assert r.status_code == 403


async def test_clone_role_preserves_avatar(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:ace"),
        headers=human_headers(),
    )
    r = await client.post(f"{API}/roles/dev/clone", headers=human_headers())
    assert r.status_code == 201, r.text
    assert r.json()["avatar"] == "big-smile:ace"

    got = await client.get(f"{API}/roles/dev-clone")
    assert got.json()["avatar"] == "big-smile:ace"


async def test_rollback_keeps_avatar_consistent(client):
    await client.post(f"{API}/runtimes", json=runtime_payload(), headers=auth_headers())
    await client.post(
        f"{API}/roles",
        json=role_payload("dev", avatar="big-smile:ace"),
        headers=human_headers(),
    )
    # 第二次配置更新会生成 version 2；随后头像改为 bee。
    await client.put(
        f"{API}/roles/dev",
        json=role_payload("dev", display_name="Dev 2"),
        headers=human_headers(),
    )
    await client.put(
        f"{API}/roles/dev/avatar",
        json={"avatar": "big-smile:bee"},
        headers=human_headers(),
    )
    # 回滚到 version 1 应把 definition 里的 ace 同步回 avatar 列。
    r = await client.post(f"{API}/roles/dev/rollback/1", headers=human_headers())
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == "big-smile:ace"

    got = await client.get(f"{API}/roles/dev")
    assert got.json()["avatar"] == "big-smile:ace"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd dev
source .venv/bin/activate
python -m pytest tests/api/test_role_avatar.py::test_update_role_avatar -v
```

Expected: FAIL —— 404，路由不存在。

- [ ] **Step 3: 实现**

`dev/backend/anvil/services/roles.py` 在 `update` 后面加：

```python
def update_avatar(conn: sqlite3.Connection, slug: str, avatar: str | None) -> dict | None:
    """只改头像：更新角色列 + definition_json，不产生画像版本快照。"""
    role = get(conn, slug)
    if role is None:
        return None
    definition = dict(role.get("definition") or {})
    definition["avatar"] = avatar
    conn.execute(
        "UPDATE roles SET avatar=?, definition_json=?, updated_at=? WHERE slug=?",
        (avatar, jdumps(definition), now_iso(), slug),
    )
    conn.commit()
    _write_profile(slug, role["display_name"], definition)
    return get(conn, slug)
```

`dev/backend/anvil/services/roles.py` 顶部加共享校验：

```python
AVATAR_RE = re.compile(r"^big-smile:[a-z0-9-]{1,32}$")


def is_valid_avatar(value: object) -> bool:
    return value is None or value == "initials" or (
        isinstance(value, str) and AVATAR_RE.fullmatch(value) is not None
    )
```

`update` 中把 definition 写入分支改成：

```python
    if definition is not None:
        if "avatar" in definition:
            avatar = definition.get("avatar")
            if is_valid_avatar(avatar):
                fields["avatar"] = avatar
            else:
                definition.pop("avatar", None)
        fields["definition_json"] = jdumps(definition)
```

这样 rollback / ensure_onboarding 等走 `update` 的路径会把 definition 里显式携带的合法 avatar 同步回列；非法值（例如提案系统传入的 dict）会被丢弃且不覆盖已有列，避免 500 或无效值入库。

`api/roles.py` 的 `_validate_avatar` 复用 `services.roles.AVATAR_RE` / `services.roles.is_valid_avatar`，保持 422 行为不变，避免两套正则。

`dev/backend/anvil/api/roles.py` 在 `update_role` 后面加：

```python
@router.put("/roles/{slug}/avatar", response_model=RoleOut)
def update_role_avatar(slug: str, body: dict, conn=Depends(get_db),
                       auth: Auth = Depends(require_human)):
    """角色头像独立更新：只改外观，不重存整份角色配置。"""
    role = services.roles.get(conn, slug)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    avatar = _validate_avatar(body.get("avatar"))
    return services.roles.update_avatar(conn, slug, avatar)
```

`clone_role` 中，读取 `definition` 后加：

```python
    if src.get("avatar"):
        definition["avatar"] = src["avatar"]
```

这样克隆角色会继承当前头像，而不是只依赖可能已被通用 PUT 清掉 avatar 的 definition_json。

- [ ] **Step 4: 运行全部头像测试**

```bash
cd dev
source .venv/bin/activate
python -m pytest tests/api/test_role_avatar.py -v
```

Expected: `test_role_avatar.py` 全部通过（Task 1 的持久化/迁移测试 + 本任务的更新、克隆、回滚测试）。

- [ ] **Step 5: 回归角色相关测试**

```bash
cd dev
source .venv/bin/activate
python -m pytest tests/api/test_roles.py tests/api/test_role_auto.py tests/api/test_role_profile.py -v
```

Expected: 全部 passed。

- [ ] **Step 6: 提交**

```bash
git add dev/backend/anvil/services/roles.py dev/backend/anvil/api/roles.py dev/tests/api/test_role_avatar.py
git commit -m "feat(roles): add dedicated avatar update endpoint

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

#### Review hardening（2026-10-23 质量审查追加）

通用 `roles.update()` 的 avatar 同步必须校验来源：提案系统等内部调用可能传入 dict / 非法字符串。追加 `is_valid_avatar` 服务层守卫（非法值丢弃、不覆盖已有列），并补以下回归测试：

- `test_update_ignores_invalid_definition_avatar`：直接调用 `services.roles.update` 传 dict / 非法字符串，列保持原值，definition 不再包含 avatar。
- `test_avatar_update_creates_no_version_snapshot`：头像更新前后 `/roles/{slug}/versions` 长度不变。
- `test_generic_put_does_not_clear_avatar`：通用 PUT 不带头像不会清空列。
- `test_avatar_update_missing_role_404`：缺失角色返回 404。
- 强化 `test_invalid_avatar_is_rejected`：422 后列保持原值。
- 强化 `test_update_role_avatar`：definition 里镜像了新头像。

提交信息：`fix(roles): validate avatar writes from generic updates` + Co-Authored-By trailer。

---

### Task 3: 头像素材、预设解析与 RoleAvatar

**Files:**
- Create: `dev/frontend/public/avatars/big-smile/*.svg`
- Create: `dev/frontend/public/avatars/CREDITS.md`
- Create: `dev/frontend/src/avatarColor.ts`
- Create: `dev/frontend/src/components/avatarPresets.ts`
- Create: `dev/frontend/src/components/RoleAvatar.tsx`

- [ ] **Step 1: 生成本地头像素材**

```bash
mkdir -p dev/frontend/public/avatars/big-smile
while IFS=: read -r id seed; do
  curl -fsSL "https://api.dicebear.com/9.x/big-smile/svg?seed=${seed}" \
    -o "dev/frontend/public/avatars/big-smile/${id}.svg"
done <<'EOF'
ace:Ace
bee:Bee
cid:Cid
dot:Dot
eli:Eli
fay:Fay
gus:Gus
hana:Hana
ivan:Ivan
june:June
kip:Kip
luna:Luna
max:Max
nia:Nia
omar:Omar
EOF
ls -1 dev/frontend/public/avatars/big-smile | wc -l
```

Expected: `15`；每个 SVG 约 5–10KB。

如果当前环境没有网络，暂停该步骤，让用户提供 15 个 SVG 文件；不要在运行期改用外链。

- [ ] **Step 2: 写 CREDITS**

创建 `dev/frontend/public/avatars/CREDITS.md`：

```markdown
# Avatar credits

The SVG avatars in `big-smile/` are generated from DiceBear's "Big Smile" style.

- Style: Big Smile
- Creator: Ashley Seo
- Source: https://www.figma.com/community/file/881358461963645496
- License: CC BY 4.0 — https://creativecommons.org/licenses/by/4.0/
- Generator: DiceBear 9.x — https://www.dicebear.com/

The generated files are bundled locally; the app does not call DiceBear at runtime.
```

- [ ] **Step 3: 写共享首字母颜色**

创建 `dev/frontend/src/avatarColor.ts`：

```ts
export const AVATAR_COLORS = [
  "#6d7cff", "#34d399", "#fbbf24", "#60a5fa",
  "#f472b6", "#2dd4bf", "#fb923c", "#a78bfa",
];

export function avatarColor(slug: string): string {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
```

- [ ] **Step 4: 写预设解析**

创建 `dev/frontend/src/components/avatarPresets.ts`：

```ts
export const AVATAR_PRESETS = [
  "ace", "bee", "cid", "dot", "eli",
  "fay", "gus", "hana", "ivan", "june",
  "kip", "luna", "max", "nia", "omar",
] as const;

const PRESET_SET = new Set<string>(AVATAR_PRESETS);
// 固定默认映射的取模基数：以后追加新预设不会改变已有角色的默认头像。
const DEFAULT_PRESET_COUNT = 15;

/** 稳定映射：同一 slug 永远得到同一默认头像。 */
export function defaultAvatarFor(slug: string): string {
  let h = 2166136261;
  for (const ch of slug) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  const index = (h >>> 0) % DEFAULT_PRESET_COUNT;
  return `big-smile:${AVATAR_PRESETS[index]}`;
}

/** 首字母兜底：用 Array.from 避免拆开 emoji/代理对。 */
export function initialFor(name: string, slug: string): string {
  const chars = Array.from((name || slug).trim());
  return chars[0]?.toUpperCase() ?? "?";
}

export type ResolvedAvatar =
  | { kind: "image"; src: string }
  | { kind: "initials" };

export function resolveAvatar(
  value: string | null | undefined,
  slug: string,
): ResolvedAvatar {
  if (value === "initials") return { kind: "initials" };

  const preset = value?.match(/^big-smile:([a-z0-9-]+)$/)?.[1];
  const id = preset && PRESET_SET.has(preset)
    ? preset
    : defaultAvatarFor(slug).slice("big-smile:".length);

  return { kind: "image", src: `/avatars/big-smile/${id}.svg` };
}
```

- [ ] **Step 5: 写 RoleAvatar**

创建 `dev/frontend/src/components/RoleAvatar.tsx`：

```tsx
import { useState } from "react";
import { avatarColor } from "../avatarColor";
import { initialFor, resolveAvatar } from "./avatarPresets";

type Props = {
  slug: string;
  name: string;
  avatar?: string | null;
  color?: string | null;
  size?: number;
  className?: string;
};

export function RoleAvatar({
  slug,
  name,
  avatar,
  color,
  size = 28,
  className = "",
}: Props) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const resolved = resolveAvatar(avatar, slug);
  const failed = resolved.kind === "image" && failedSrc === resolved.src;

  if (resolved.kind === "image" && !failed) {
    return (
      <span
        className={`role-avatar role-avatar-image ${className}`}
        style={{ width: size, height: size }}
        aria-hidden="true"
      >
        <img
          src={resolved.src}
          alt=""
          style={{ width: "100%", height: "100%", objectFit: "cover", borderRadius: "50%" }}
          onError={() => setFailedSrc(resolved.src)}
        />
      </span>
    );
  }

  return (
    <span
      className={`role-avatar ${className}`}
      style={{
        width: size,
        height: size,
        background: color || avatarColor(slug),
        fontSize: Math.max(10, Math.round(size * 0.4)),
      }}
      aria-hidden="true"
    >
      {initialFor(name, slug)}
    </span>
  );
}
```

- [ ] **Step 6: 构建验证**

```bash
cd dev/frontend
npm run build
```

Expected: `tsc && vite build` 成功；如果失败，检查 `resolveAvatar` 的返回类型收窄和 `avatarPresets` 的 `match[1]`。

- [ ] **Step 7: 提交**

```bash
git add dev/frontend/public/avatars dev/frontend/src/avatarColor.ts dev/frontend/src/components/avatarPresets.ts dev/frontend/src/components/RoleAvatar.tsx
git commit -m "feat(ui): add bundled role avatars and RoleAvatar

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 前端 API 类型与头像方法

**Files:**
- Modify: `dev/frontend/src/api/client.ts:42-50`（Role）
- Modify: `dev/frontend/src/api/client.ts:157-159`（autoCreateRole）
- Modify: `dev/frontend/src/api/client.ts:246-256`（createRole / updateRoleRuntime 之间）

- [ ] **Step 1: 扩展 Role 类型**

```ts
export interface Role {
  slug: string;
  display_name: string;
  avatar?: string | null;
  avatar_color?: string | null;
  home_path: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  definition?: Record<string, unknown>;
}
```

- [ ] **Step 2: 扩展创建方法**

```ts
  autoCreateRole: (brief: string, runtimeId?: string, avatar?: string | null): Promise<Role> =>
    req("POST", "/roles/auto", {
      brief,
      runtime_id: runtimeId || undefined,
      avatar: avatar ?? undefined,
    }).then((r) => json<Role>(r)),
```

```ts
  createRole: (slug: string, displayName: string, runtimeId?: string, avatar?: string | null): Promise<Role> =>
    req("POST", "/roles", {
      slug,
      display_name: displayName,
      avatar: avatar ?? undefined,
      runtime: runtimeId ? { id: runtimeId } : {},
      workspace: { home: `agents/${slug}/`, repos: [] },
      tools: {},
      concurrency: {},
    }).then((r) => json<Role>(r)),
```

- [ ] **Step 3: 新增头像更新方法**

在 `updateRoleRuntime` 前加：

```ts
  updateRoleAvatar: (slug: string, avatar: string | null): Promise<Role> =>
    req("PUT", `/roles/${slug}/avatar`, { avatar }).then((r) => json<Role>(r)),
```

- [ ] **Step 4: 构建验证**

```bash
cd dev/frontend
npm run build
```

Expected: 成功。

- [ ] **Step 5: 提交**

```bash
git add dev/frontend/src/api/client.ts
git commit -m "feat(ui): add avatar fields and role avatar API client

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: AvatarPicker 组件

**Files:**
- Create: `dev/frontend/src/components/AvatarPicker.tsx`

- [ ] **Step 1: 写组件**

```tsx
import { useEffect, useRef, useState } from "react";
import { AVATAR_PRESETS, initialFor, resolveAvatar } from "./avatarPresets";

type Props = {
  mode: "inline" | "modal";
  slug: string;
  name: string;
  value: string | null;
  onChange: (value: string | null) => void | Promise<void>;
  onClose?: () => void;
};

export function AvatarPicker({
  mode,
  slug,
  name,
  value,
  onChange,
  onClose,
}: Props) {
  const [draft, setDraft] = useState<string | null>(value);
  const [saving, setSaving] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (mode !== "modal") return;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, onClose]);

  const resolved = resolveAvatar(draft, slug);
  const currentPreset = resolved.kind === "image"
    ? resolved.src.slice("/avatars/big-smile/".length, -".svg".length)
    : null;

  const pick = (next: string) => {
    if (mode === "inline") {
      void onChange(next);
    } else {
      setDraft(next);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      await onChange(draft);
      onClose?.();
    } finally {
      setSaving(false);
    }
  };

  const grid = (
    <div className="avatar-grid">
      {AVATAR_PRESETS.map((id) => {
        const selected = currentPreset === id;
        return (
          <button
            key={id}
            type="button"
            className={`avatar-tile ${selected ? "selected" : ""}`}
            onClick={() => pick(`big-smile:${id}`)}
            aria-pressed={selected}
            aria-label={`头像 ${id}`}
            title={`头像 ${id}`}
          >
            <img src={`/avatars/big-smile/${id}.svg`} alt="" />
          </button>
        );
      })}
      <button
        type="button"
        className={`avatar-tile avatar-tile-initials ${draft === "initials" ? "selected" : ""}`}
        onClick={() => pick("initials")}
        aria-pressed={draft === "initials"}
        aria-label="使用首字母头像"
        title="使用首字母"
      >
        <span>{initialFor(name, slug)}</span>
      </button>
    </div>
  );

  if (mode === "inline") {
    return (
      <div className="avatar-picker-inline">
        <div className="avatar-picker-head">
          <span className="avatar-picker-label">头像</span>
          <small>默认按 slug 自动匹配，点击可更换</small>
        </div>
        {grid}
      </div>
    );
  }

  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <div
        className="modal avatar-modal"
        role="dialog"
        aria-modal="true"
        aria-label="选择角色头像"
      >
        <div className="modal-head">
          <h2>选择角色头像</h2>
          <button
            ref={closeRef}
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="关闭"
          >
            ×
          </button>
        </div>
        <p className="avatar-picker-hint">
          给「{name}」选一个头像。创建后也可以随时更换。
        </p>
        {grid}
        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>取消</button>
          <button type="button" className="primary" onClick={save} disabled={saving}>
            {saving ? "保存中…" : "保存头像"}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 构建验证**

```bash
cd dev/frontend
npm run build
```

Expected: 成功。组件暂时没有被引用，但 `tsc` 会检查它。

- [ ] **Step 3: 提交**

```bash
git add dev/frontend/src/components/AvatarPicker.tsx
git commit -m "feat(ui): add avatar picker component

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

#### Review hardening（2026-10-23 质量审查追加）

- `save()` 捕获 `onChange` rejection，只成功时关闭弹窗，并在弹窗内显示「保存失败，请重试」；Task 6 的 `onChange` 在 `setError` 后 `throw e`。
- 新增 `dismiss()`：`saving` 时忽略 Escape / 遮罩 / × / 取消；`pick()` 在 saving 时 no-op；遮罩只响应左键。
- 聚焦 effect 只依赖 `[mode]`，Escape 监听单独依赖 `[mode, onClose, saving]`，避免父组件重渲染抢焦点。
- `ResolvedAvatar` 的 image 分支增加 `presetId`，AvatarPicker 不再从 `src` 反解 id。

---

### Task 6: 角色工坊接入头像选择与修改

**Files:**
- Modify: `dev/frontend/src/pages/Roles.tsx`（imports、state、create、autoCreate、create form、roles list、detail header、basic tab、modal）

- [ ] **Step 1: 加 imports 与 state**

在文件顶部加：

```tsx
import { AvatarPicker } from "../components/AvatarPicker";
import { RoleAvatar } from "../components/RoleAvatar";
```

在 `const [genBusy, setGenBusy] = useState(false);` 后加：

```tsx
  const [newAvatar, setNewAvatar] = useState<string | null>(null);
  const [avatarOpen, setAvatarOpen] = useState(false);
```

- [ ] **Step 2: 创建时传头像**

`create` 中把：

```tsx
      const r = await api.createRole(slug, displayName, runtimeId);
```

改成：

```tsx
      const r = await api.createRole(slug, displayName, runtimeId, newAvatar);
```

并在清空输入后加：

```tsx
      setNewAvatar(null);
```

`autoCreate` 中把：

```tsx
      const r = await api.autoCreateRole(autoBrief.trim(), runtimeId || undefined);
```

改成：

```tsx
      const r = await api.autoCreateRole(autoBrief.trim(), runtimeId || undefined, newAvatar);
```

并在清空 `autoBrief` 后加：

```tsx
      setNewAvatar(null);
```

- [ ] **Step 3: 创建面板加内联头像选择**

把现在的两个 `<div className="toolbar">`、slug 提示和运行时警告整体替换成下面的纸面面板：

```tsx
      <section className="create-role-panel">
        <div className="toolbar">
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="slug（小写字母/数字/-/_）"
            pattern="[a-z0-9][a-z0-9_-]{1,63}"
            maxLength={64}
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            title={SLUG_HINT}
            aria-invalid={slugInvalid}
          />
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="显示名"
          />
          <select
            value={runtimeId}
            onChange={(e) => setRuntimeId(e.target.value)}
            title="角色绑定的运行时"
          >
            {runtimes.length === 0 && <option value="">（无可用运行时）</option>}
            {runtimeId && !runtimes.some((r) => r.id === runtimeId) && (
              <option value={runtimeId}>{runtimeId}（未注册）</option>
            )}
            {runtimes.map((r) => (
              <option key={r.id} value={r.id}>{r.display_name} · {r.status}</option>
            ))}
          </select>
          <button onClick={create} disabled={!slug || !displayName || !runtimeId || slugInvalid}>
            新建角色
          </button>
        </div>
        <p className={slugInvalid ? "error role-slug-hint" : "muted role-slug-hint"}>
          {slugInvalid ? SLUG_HINT : "slug：小写字母、数字、-、_，2–64 位；不支持中文/大写/空格。"}
        </p>
        <div className="toolbar">
          <input
            value={autoBrief}
            onChange={(e) => setAutoBrief(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && autoCreate()}
            placeholder="一句话描述角色，AI 生成能力与定位后创建：如「负责后端 API、数据库迁移和性能优化」"
            style={{ flex: 1 }}
          />
          <button
            className="primary"
            onClick={autoCreate}
            disabled={autoBusy || !autoBrief.trim() || !runtimeId}
          >
            {autoBusy ? "生成中…" : "AI 生成并创建"}
          </button>
        </div>
        {runtimes.length === 0 && (
          <p className="muted">
            没有可用运行时：请先到「运行时」页扫描，或在角色创建前新增自定义运行时。
          </p>
        )}
        <AvatarPicker
          mode="inline"
          slug={slug || displayName || "new-role"}
          name={displayName || "新角色"}
          value={newAvatar}
          onChange={setNewAvatar}
        />
      </section>
```

注意：`slug` 为空时用 `"new-role"` 给默认预览。

- [ ] **Step 4: 角色列表加头像**

把 `roles.map((r) => (` 的 `<li>` 里原来的 `<a>` 改成包含头像的横向结构（保留原文字和 badges）：

```tsx
              <a
                href="#"
                className="role-list-main"
                onClick={(e) => { e.preventDefault(); setSelected(r.slug); setTab("basic"); }}
              >
                <RoleAvatar
                  slug={r.slug}
                  name={r.display_name}
                  avatar={r.avatar}
                  color={r.avatar_color}
                  size={28}
                />
                <span className="role-list-text">
                  {r.display_name} <small>({r.slug})</small>
                  {isOnboardingRole(r) && <span className="badge badge-in_progress" title="默认助理，不可删除">默认助理</span>}
                  {!isOnboardingRole(r) && !SLUG_RE.test(r.slug) && (
                    <span className="badge wf-gate" title="slug 含中文/非法字符，建议删除并重建">slug 不合规</span>
                  )}
                </span>
              </a>
```

删除原来那段 `<a>...</a>`，避免重复。

- [ ] **Step 5: 详情头部 + 更换头像**

在 `<div className="roles-detail">` 内、`{current ? (` 的 `<>` 后加：

```tsx
              <div className="role-detail-head">
                <button
                  type="button"
                  className="role-detail-avatar"
                  onClick={() => setAvatarOpen(true)}
                  aria-label="更换角色头像"
                  title="更换头像"
                >
                  <RoleAvatar
                    slug={current.slug}
                    name={current.display_name}
                    avatar={current.avatar}
                    color={current.avatar_color}
                    size={52}
                  />
                </button>
                <div className="role-detail-title">
                  <h2>{current.display_name}</h2>
                  <span className="mono">{current.slug}</span>
                </div>
                <button type="button" className="small" onClick={() => setAvatarOpen(true)}>
                  更换头像
                </button>
              </div>
```

在 `tab === "basic"` 的 `<dl>` 中加：

```tsx
                    <dt>头像</dt>
                    <dd>
                      <button type="button" className="small" onClick={() => setAvatarOpen(true)}>
                        更换头像
                      </button>
                    </dd>
```

在组件 return 的最后（`</div>` 前）加 modal：

```tsx
      {avatarOpen && current && (
        <AvatarPicker
          mode="modal"
          slug={current.slug}
          name={current.display_name}
          value={current.avatar ?? null}
          onChange={async (value) => {
            try {
              await api.updateRoleAvatar(current.slug, value);
              await refresh();
            } catch (e) {
              setError(String(e));
              throw e; // 让 AvatarPicker 保持弹窗打开并显示本地错误
            }
          }}
          onClose={() => setAvatarOpen(false)}
        />
      )}
```

- [ ] **Step 6: 构建验证**

```bash
cd dev/frontend
npm run build
```

Expected: 成功；如果报 `current` 可能为 null，确认 modal 在 `current &&` 内。

- [ ] **Step 7: 手工冒烟**

启动后端与前端（或 `npm run build` 后用后端静态页），打开「角色工坊」：

1. 输入 slug / 显示名 → 头像默认预览出现；
2. 点击某个头像 → 选中态变化；
3. 新建角色 → 列表和详情显示所选头像；
4. 详情点击「更换头像」→ 弹窗选择 → 保存 → 列表/详情刷新；
5. AI 生成创建也能带上所选头像。

- [ ] **Step 8: 提交**

```bash
git add dev/frontend/src/pages/Roles.tsx
git commit -m "feat(roles): choose and change avatars in the role workshop

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 全站替换角色头像渲染点

**Files:**
- Modify: `dev/frontend/src/pages/Lobby.tsx`
- Modify: `dev/frontend/src/pages/TaskDetail.tsx`
- Modify: `dev/frontend/src/pages/NewTaskModal.tsx`
- Modify: `dev/frontend/src/pages/RoleSessions.tsx`
- Modify: `dev/frontend/src/pages/AgentSessions.tsx`

- [ ] **Step 1: Lobby 会话卡**

在 `Lobby.tsx` 顶部 import：

```tsx
import { RoleAvatar } from "../components/RoleAvatar";
```

把右栏会话卡里的：

```tsx
                <span className="role-avatar" style={{ background: avatarColor(r.role_slug), width: 28, height: 28, fontSize: 12 }}>
                  {roleName.slice(0, 1)}
                </span>
```

替换成：

```tsx
                <RoleAvatar
                  slug={r.role_slug}
                  name={roleName}
                  avatar={roles.find((x) => x.slug === r.role_slug)?.avatar}
                  color={roles.find((x) => x.slug === r.role_slug)?.avatar_color}
                  size={28}
                />
```

删除本文件不再使用的 `AVATAR_COLORS` / `avatarColor`（如果其他地方没有用到）。

- [ ] **Step 2: TaskDetail 消息头像与状态行**

在 `TaskDetail.tsx` 顶部 import：

```tsx
import { RoleAvatar } from "../components/RoleAvatar";
```

在线程 map 内、`return (` 前加：

```tsx
              const roleInfo = roles.find((r) => r.slug === role);
```

把 `<span className="thread-avatar" ...>...</span>` 整块替换成：

```tsx
                  {d.mention ? (
                    <span className="thread-avatar mention-avatar">@</span>
                  ) : isHuman ? (
                    <span className="thread-avatar human-avatar">人</span>
                  ) : systemLabel ? null : (
                    <RoleAvatar
                      slug={role}
                      name={displayRole}
                      avatar={roleInfo?.avatar}
                      color={roleInfo?.avatar_color}
                      size={32}
                      className="thread-role-avatar"
                    />
                  )}
```

把这一行的 className：

```tsx
                  className={`thread-item ${ev.kind === "comment" ? "is-message" : ""} ${d.mention ? "is-mention" : ""} ${systemLabel || isHuman ? "is-system" : ""}`}
```

改成：

```tsx
                  className={`thread-item ${ev.kind === "comment" ? "is-message" : ""} ${d.mention ? "is-mention" : ""} ${isHuman ? "is-human" : ""} ${systemLabel ? "is-system-event" : ""}`}
```

在 `renderRoleLive` 返回的 `<span className="role-live-chip">` 内、状态点前加：

```tsx
      <RoleAvatar
        slug={live.slug}
        name={roles.find((r) => r.slug === live.slug)?.display_name ?? live.slug}
        avatar={roles.find((r) => r.slug === live.slug)?.avatar}
        color={roles.find((r) => r.slug === live.slug)?.avatar_color}
        size={18}
      />
```

删除本文件不再使用的 `AVATAR_COLORS` / `avatarColor`。

- [ ] **Step 3: NewTaskModal 成员头像**

import：

```tsx
import { RoleAvatar } from "../components/RoleAvatar";
```

把：

```tsx
                  <span className="member-avatar" style={{ background: avatarColor(r.slug) }}>
                    {r.display_name.slice(0, 1)}
                  </span>
```

替换成：

```tsx
                  <RoleAvatar
                    slug={r.slug}
                    name={r.display_name}
                    avatar={r.avatar}
                    color={r.avatar_color}
                    size={30}
                  />
```

删除本地 `AVATAR_COLORS` / `avatarColor`。

- [ ] **Step 4: RoleSessions 角色行**

import：

```tsx
import { RoleAvatar } from "../components/RoleAvatar";
```

在现有的 `const roleName = (slug: string) => ...` 后加：

```tsx
  const roleFor = (slug: string) => roles.find((r) => r.slug === slug);
```

把：

```tsx
              <span className="role-avatar" style={{ background: avatarColor(slug) }}>
                {roleName(slug).slice(0, 1)}
              </span>
```

替换成：

```tsx
              <RoleAvatar
                slug={slug}
                name={roleName(slug)}
                avatar={roleFor(slug)?.avatar}
                color={roleFor(slug)?.avatar_color}
                size={28}
              />
```

删除本文件顶部的 `AVATAR_COLORS` / `avatarColor`。

- [ ] **Step 5: AgentSessions 头部**

import：

```tsx
import { RoleAvatar } from "../components/RoleAvatar";
```

把：

```tsx
          <span className="role-avatar" style={{ background: avatarColor(slug), width: 34, height: 34 }}>{role.display_name.slice(0, 1)}</span>
```

替换成：

```tsx
          <RoleAvatar
            slug={slug}
            name={role.display_name}
            avatar={role.avatar}
            color={role.avatar_color}
            size={34}
          />
```

删除本地 `AVATAR_COLORS` / `avatarColor`。

- [ ] **Step 6: 构建验证**

```bash
cd dev/frontend
npm run build
```

Expected: 成功。若某文件删除 `avatarColor` 后仍被引用，构建会报错，按提示清理。

- [ ] **Step 7: 提交**

```bash
git add dev/frontend/src/pages/Lobby.tsx dev/frontend/src/pages/TaskDetail.tsx dev/frontend/src/pages/NewTaskModal.tsx dev/frontend/src/pages/RoleSessions.tsx dev/frontend/src/pages/AgentSessions.tsx
git commit -m "feat(ui): use RoleAvatar across role surfaces

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: 样式层 tokens 与 base

**Files:**
- Create: `dev/frontend/src/styles/tokens.css`
- Create: `dev/frontend/src/styles/base.css`

- [ ] **Step 1: 创建 tokens.css**

```css
:root {
  color-scheme: dark;

  /* 墨色壳层 */
  --ink-900: #14131a;
  --ink-800: #1b1a23;
  --ink-700: #241f2e;
  --ink-line: rgba(247, 239, 227, 0.16);
  --ink-line-strong: rgba(247, 239, 227, 0.28);

  /* 纸面 */
  --paper: #f7efe3;
  --paper-2: #efe4d2;
  --paper-3: #fffaf1;

  /* 文字 */
  --text-on-ink: #f7efe3;
  --text-on-ink-muted: #a8a0b4;
  --text-on-ink-faint: #6f6878;
  --text-on-paper: #14131a;
  --text-on-paper-soft: #4b4652;
  --text-on-paper-muted: #6b6375;
  --link-on-paper: #0e7490;
  --red-on-paper: #b91c1c;

  /* 状态与强调 */
  --yellow: #facc15;
  --yellow-hover: #ffe066;
  --cyan: #22d3ee;
  --red: #f87171;
  --green: #4ade80;
  --blue: #60a5fa;
  --paper-hover: #fff3c4;

  /* 描边、圆角、阴影 */
  --stroke: 2px;
  --radius-sm: 6px;
  --radius: 10px;
  --radius-lg: 14px;
  --shadow-1: 2px 2px 0 rgba(20, 19, 26, 0.22);
  --shadow-2: 4px 4px 0 rgba(0, 0, 0, 0.4);
  --shadow-3: 6px 6px 0 rgba(0, 0, 0, 0.42);
  --shadow-yellow: 3px 3px 0 var(--yellow);
  --shadow-cyan: 3px 3px 0 var(--cyan);
  --shadow-red: 3px 3px 0 var(--red);

  /* 字体与尺度 */
  --font-body: "Inter", system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --mono: var(--font-mono);
  --sidebar-w: clamp(200px, 12vw, 248px);
  --sidebar-rail-w: 68px;
  --panel-w: clamp(320px, 20vw, 440px);
  --content-pad: clamp(14px, 1.15vw, 30px);
  --fs-base: clamp(13px, 0.3vw + 9px, 15px);
  --fs-h1: clamp(1.4rem, 0.25vw + 1.15rem, 1.85rem);
  --header-h: 56px;

  /* 旧变量兼容：pages.css 里的历史规则继续可用 */
  --bg: var(--ink-900);
  --bg-soft: var(--ink-800);
  --surface: var(--ink-700);
  --surface-2: #2d2738;
  --border: var(--ink-line);
  --border-strong: var(--ink-line-strong);
  --text: var(--text-on-ink);
  --muted: var(--text-on-ink-muted);
  --faint: var(--text-on-ink-faint);
  --accent: var(--yellow);
  --accent-strong: var(--cyan);
  --amber: var(--yellow);
}
```

- [ ] **Step 2: 创建 base.css**

```css
*,
*::before,
*::after {
  box-sizing: border-box;
}

html,
body,
#root {
  height: 100%;
}

body {
  margin: 0;
  overflow: hidden;
  background-color: var(--ink-900);
  background-image:
    radial-gradient(circle at 82% -10%, rgba(250, 204, 21, 0.08), transparent 32%),
    radial-gradient(circle at 8% 108%, rgba(34, 211, 238, 0.06), transparent 34%),
    radial-gradient(rgba(247, 239, 227, 0.05) 1px, transparent 1.2px);
  background-size: auto, auto, 7px 7px;
  color: var(--text-on-ink);
  font-family: var(--font-body);
  font-size: var(--fs-base);
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

a {
  color: var(--cyan);
  text-decoration: none;
}

a:hover {
  text-decoration: underline;
  text-underline-offset: 2px;
}

button,
input,
select,
textarea {
  font: inherit;
  color: inherit;
}

button {
  cursor: pointer;
}

:focus-visible {
  outline: 3px solid var(--cyan);
  outline-offset: 2px;
}

::selection {
  background: var(--yellow);
  color: var(--ink-900);
}

h1,
h2,
h3,
h4,
p {
  margin: 0;
}

h1 {
  font-size: var(--fs-h1);
  font-weight: 900;
  letter-spacing: -0.03em;
}

h2 {
  font-size: 17px;
  font-weight: 850;
}

h3 {
  font-size: 13px;
  font-weight: 800;
}

.mono {
  font-family: var(--font-mono);
}

.error {
  color: var(--red);
}

.faint {
  color: var(--text-on-ink-faint);
}

::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}

::-webkit-scrollbar-track {
  background: rgba(247, 239, 227, 0.04);
}

::-webkit-scrollbar-thumb {
  background: rgba(247, 239, 227, 0.18);
  border: 2px solid var(--ink-900);
  border-radius: 99px;
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

- [ ] **Step 3: 提交**

```bash
git add dev/frontend/src/styles/tokens.css dev/frontend/src/styles/base.css
git commit -m "refactor(ui): add comic design tokens and base styles

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: pages.css 兼容底座

**Files:**
- Create: `dev/frontend/src/styles/pages.css`（由旧 `style.css` 机械迁移）

- [ ] **Step 1: 复制旧规则**

```bash
mkdir -p dev/frontend/src/styles
{
  sed -n '45,742p' dev/frontend/src/style.css
  echo
  sed -n '1060,1091p' dev/frontend/src/style.css
} > dev/frontend/src/styles/pages.css
```

说明：1–44 是旧 tokens/reset/body/a，已由 `tokens.css` + `base.css` 取代；45–742 才是旧组件/页面规则。1060–1091 是角色工坊/表格/宽屏卡片覆盖。743–1059 是旧 app shell 布局，1092–1161 是旧响应式，会由 `layout.css` 取代。不要从 36 开始——36 仍在旧 `body` 规则内部，会生成断头声明。

- [ ] **Step 2: 替换旧硬编码颜色**

```bash
perl -0pi -e '
s/#0b0e15/var(--ink-900)/g;
s/#10141f/var(--ink-800)/g;
s/#151a28/var(--ink-700)/g;
s/#1b2133/var(--ink-700)/g;
s/#232a3d/var(--ink-line)/g;
s/#2e3850/var(--ink-line-strong)/g;
s/#e7ebf4/var(--text-on-ink)/g;
s/#8b94a9/var(--text-on-ink-muted)/g;
s/#5c6579/var(--text-on-ink-faint)/g;
s/#6d7cff/var(--yellow)/g;
s/#8a96ff/var(--cyan)/g;
s/#34d399/var(--green)/g;
s/#fbbf24/var(--yellow)/g;
s/#60a5fa/var(--blue)/g;
s/#f87171/var(--red)/g;
s/#262c3d/var(--ink-700)/g;
s/#9aa3b8/var(--text-on-ink-muted)/g;
s/#6b7387/var(--text-on-ink-faint)/g;
s/#0a0d13/var(--ink-900)/g;
s/#0f1117/var(--ink-900)/g;
s/#c7d0e0/var(--text-on-ink)/g;
s/#d7dae0/var(--text-on-ink)/g;
s/#fdeaea/var(--paper-2)/g;
s/#c0392b/var(--red)/g;
s/#7a6ff0/var(--cyan)/g;
s/#2f9e6e/var(--green)/g;
s/#f5b301/var(--yellow)/g;
s/#ef5350/var(--red)/g;
s/#2ecc71/var(--green)/g;
s/#5aa9ff/var(--blue)/g;
s/#9aa4b2/var(--text-on-ink-muted)/g;
' dev/frontend/src/styles/pages.css
```

- [ ] **Step 3: 检查残留**

```bash
grep -nE '#(0b0e15|10141f|151a28|1b2133|232a3d|2e3850|6d7cff|8a96ff|fdeaea|7a6ff0|0a0d13|0f1117)' dev/frontend/src/styles/pages.css || echo "no legacy core colors"
```

Expected: `no legacy core colors`。

- [ ] **Step 4: 提交**

```bash
git add dev/frontend/src/styles/pages.css
git commit -m "refactor(ui): migrate legacy page rules into pages.css

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: components.css（新组件视觉）

**Files:**
- Create: `dev/frontend/src/styles/components.css`
- Modify: `dev/frontend/src/styles/tokens.css`（补充 `--link-on-paper` / `--red-on-paper`，如 Task 8 未提前加入）

- [ ] **Step 1: 写 components.css**

```css
/* ===== 文本上下文 ===== */
.muted { color: var(--text-on-ink-muted); }
.card .muted,
.role-card .muted,
.run-card .muted,
.run-row .muted,
.run-timeline li .muted,
.rs-row-wrap .muted,
.child-task .muted,
.inbox-item .muted,
.drafts > li .muted,
.roles-list li .muted,
.roles-detail .muted,
.task-side .muted,
.session-head .muted,
.create-role-panel .muted,
.modal .muted,
.member-row .muted,
.kanban-col .muted,
.card .empty .muted,
.kanban-col .empty .muted,
.roles-detail .empty .muted,
.modal .empty .muted,
.board .muted,
.history-table .muted,
.table-scroll .muted,
.thread-item.is-message .muted {
  color: var(--text-on-paper-muted);
}
.faint { color: var(--text-on-ink-faint); }

/* 纸面链接与错误色：全局 a 用青色，纸面上改用深青；错误用深红 */
.card :where(a),
.inbox-item :where(a),
.modal :where(a),
.board :where(a),
.history-table :where(a),
.roles-detail :where(a),
.task-side :where(a),
.session-head :where(a),
.create-role-panel :where(a),
.rs-row-wrap :where(a),
.child-task :where(a),
.run-card :where(a),
.drafts > li :where(a) {
  color: var(--link-on-paper);
}
.card .error,
.modal .error,
.roles-detail .error,
.task-side .error,
.session-head .error,
.create-role-panel .error {
  color: var(--red-on-paper);
}

/* ===== 页头 ===== */
.page-header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 16px;
}
.page-header-main { min-width: 0; }
.page-kicker {
  display: inline-block;
  margin-bottom: 5px;
  padding: 1px 7px;
  background: var(--cyan);
  color: var(--ink-900);
  border: 2px solid var(--ink-900);
  box-shadow: 2px 2px 0 var(--ink-900);
  font-family: var(--font-mono);
  font-size: 8px;
  font-weight: 900;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  transform: rotate(-1deg);
}
.page-title {
  margin: 0;
  color: var(--text-on-ink);
  font-size: var(--fs-h1);
  font-weight: 900;
  line-height: 1.05;
  letter-spacing: -0.03em;
}
.page-header h1.page-title { margin: 0; }
.page-subtitle {
  margin-top: 4px;
  color: var(--text-on-ink-muted);
  font-size: 11px;
  font-weight: 600;
}
.page-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  flex-wrap: wrap;
}
.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0 8px;
}
.section-header h2,
.section-title {
  color: var(--text-on-ink);
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.card .section-title,
.roles-detail .section-title,
.modal .section-title,
.task-side .section-title {
  color: var(--text-on-paper);
}
.section-header .count {
  padding: 0 6px;
  border: 1.5px solid var(--ink-900);
  border-radius: 99px;
  background: var(--paper-2);
  color: var(--ink-900);
  font-size: 9px;
  font-weight: 900;
}
.section-header .actions { margin-left: auto; }

/* ===== 按钮 ===== */
button {
  min-height: 30px;
  padding: 0 11px;
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--text-on-paper);
  font-size: 12px;
  font-weight: 800;
  white-space: nowrap;
  box-shadow: var(--shadow-1);
  transition: transform 0.1s ease, box-shadow 0.1s ease, background 0.1s ease;
}
button:hover:not(:disabled) {
  background: var(--paper-2);
  border-color: var(--ink-900);
  transform: translate(-1px, -1px);
  box-shadow: var(--shadow-2);
}
button:active:not(:disabled) {
  transform: translate(2px, 2px);
  box-shadow: none;
}
button:disabled { opacity: 0.45; cursor: not-allowed; }
button.primary {
  background: var(--yellow);
  color: var(--ink-900);
  box-shadow: var(--shadow-red);
}
button.primary:hover:not(:disabled) { background: var(--yellow-hover); }
button.danger {
  background: var(--paper);
  border-color: var(--red);
  color: var(--red);
  box-shadow: var(--shadow-red);
}
button.danger:hover:not(:disabled) {
  background: var(--red);
  color: var(--ink-900);
}
button.small {
  min-height: 25px;
  padding: 0 8px;
  font-size: 11px;
  box-shadow: 2px 2px 0 rgba(20, 19, 26, 0.2);
}
button.ghost {
  background: transparent;
  border-color: var(--ink-line-strong);
  color: var(--text-on-ink);
  box-shadow: none;
}
button.ghost:hover:not(:disabled) { background: rgba(247, 239, 227, 0.08); }
.sidebar button,
.app-header button {
  background: transparent;
  color: var(--text-on-ink);
  border-color: var(--ink-line-strong);
  box-shadow: none;
}
.sidebar button:hover:not(:disabled),
.app-header button:hover:not(:disabled) {
  background: rgba(247, 239, 227, 0.08);
}
.tab,
.mention-item,
.member-row,
.icon-btn,
.modal-close,
.wf-node,
.wf-slice-row-btn,
.rs-row,
.rail-btn,
.hamburger,
.drawer-mask {
  box-shadow: none;
}

/* ===== 输入 ===== */
input,
select,
textarea {
  min-height: 34px;
  padding: 6px 9px;
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius-sm);
  background: var(--paper-3);
  color: var(--text-on-paper);
  font-weight: 600;
}
input::placeholder,
textarea::placeholder { color: var(--text-on-paper-muted); }
input:focus,
select:focus,
textarea:focus {
  outline: none;
  border-color: var(--ink-900);
}
input:focus-visible,
select:focus-visible,
textarea:focus-visible {
  box-shadow: var(--shadow-cyan);
  outline: 2px solid transparent; /* forced-colors 下仍可见 */
  outline-offset: 2px;
}
input[type="checkbox"],
input[type="radio"] {
  min-height: 0;
  height: auto;
  accent-color: var(--ink-900);
}
select { height: 34px; }
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.toolbar > input,
.toolbar > select {
  flex: 0 1 300px;
  max-width: 420px;
}
.toolbar > button { flex: none; }
.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 12px;
  color: var(--text-on-paper);
  font-size: 12px;
  font-weight: 700;
}
.field > span { font-weight: 800; }
.field small,
.field .muted {
  color: var(--text-on-paper-muted);
  font-weight: 600;
}

/* ===== 徽章 ===== */
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border: var(--stroke) solid var(--ink-line-strong);
  border-radius: 99px;
  background: transparent;
  color: var(--text-on-ink-muted);
  font-size: 10px;
  font-weight: 800;
}
.status,
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border: 1.5px solid var(--ink-900);
  border-radius: 4px;
  background: var(--paper-2);
  color: var(--ink-900);
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
}
.status::before,
.badge::before {
  content: "";
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.status-todo, .badge-todo,
.status-closed, .badge-closed,
.status-abandoned, .badge-abandoned,
.status-missing {
  background: var(--paper-2);
  color: var(--text-on-paper-soft);
}
.status-in_progress, .badge-in_progress,
.status-running, .badge-running {
  background: var(--yellow);
  color: var(--ink-900);
}
.status-in_review, .badge-in_review,
.status-queued, .badge-queued,
.status-unverified {
  background: var(--paper);
  border-color: var(--yellow);
  color: var(--ink-900);
}
.status-done, .badge-done,
.status-completed, .badge-completed,
.status-ready {
  background: var(--green);
  color: var(--ink-900);
}
.status-busy, .badge-busy,
.status-failed, .badge-failed,
.status-unhealthy {
  background: var(--red);
  color: var(--ink-900);
}
.status-submitted,
.status-reviewing,
.status-projecting,
.status-created {
  background: var(--cyan);
  color: var(--ink-900);
}
.review-flag {
  background: var(--yellow);
  color: var(--ink-900);
  border: 2px solid var(--ink-900);
  border-radius: 4px;
}

/* ===== 卡片 ===== */
.card,
.role-card,
.run-card,
.run-row,
.child-task,
.inbox-item,
.drafts > li,
.roles-list li {
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius);
  box-shadow: var(--shadow-1);
  color: var(--text-on-paper);
}
.card:hover,
.role-card:hover,
.run-card:hover,
.run-row:hover,
.child-task:hover {
  border-color: var(--ink-900);
  transform: translate(-1px, -1px);
  box-shadow: var(--shadow-2);
}
.card-link strong { color: var(--text-on-paper); }
.card-link:hover strong { color: var(--ink-900); }
.card-meta,
.card-link .muted { color: var(--text-on-paper-muted); }
.card-actions { display: flex; gap: 5px; margin-top: 7px; flex-wrap: wrap; }
.card-roles { display: inline-flex; align-items: center; gap: 3px; }
.card-progress,
.progress {
  height: 5px;
  margin-top: 8px;
  border: 1.5px solid var(--ink-900);
  background: var(--paper-2);
}
.card-progress i,
.progress i {
  display: block;
  height: 100%;
  background: var(--yellow);
  border-right: 1.5px solid var(--ink-900);
}

/* ===== 看板 ===== */
.kanban { gap: 10px; }
.kanban-col {
  background: var(--paper);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
  padding: 8px;
  color: var(--text-on-paper);
}
.kanban-col h2 {
  padding-bottom: 6px;
  border-bottom: var(--stroke) solid var(--ink-900);
  color: var(--text-on-paper);
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.kanban-col h2 .n {
  background: var(--paper-2);
  border: 1.5px solid var(--ink-900);
  border-radius: 99px;
  color: var(--ink-900);
}
.kanban-empty {
  border: 2px dashed var(--paper-2);
  padding: 12px 8px;
  font-size: 10px;
  text-align: center;
}

/* ===== 表格 ===== */
.table-scroll { width: 100%; overflow-x: auto; }
.board,
.history-table {
  width: 100%;
  border-collapse: collapse;
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  box-shadow: var(--shadow-2);
  color: var(--text-on-paper);
}
.board th,
.history-table th {
  padding: 8px 10px;
  background: var(--ink-900);
  color: var(--text-on-ink);
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  text-align: left;
}
.board td,
.history-table td {
  padding: 8px 10px;
  border-bottom: 1.5px dashed var(--paper-2);
  text-align: left;
}
.board tbody tr:last-child td,
.history-table tbody tr:last-child td { border-bottom: 0; }
.board tbody tr:hover td,
.history-table tbody tr:hover td { background: var(--paper-hover); }
.board thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--ink-900);
}

/* 纸面容器使用浅色原生控件/滚动条 */
.card,
.modal,
.roles-detail,
.task-side,
.session-head,
.create-role-panel,
.board,
.history-table,
.table-scroll { color-scheme: light; }
.modal,
.table-scroll,
.session-thread,
.task-side,
.roles-detail {
  scrollbar-color: rgba(20, 19, 26, 0.28) rgba(239, 228, 210, 0.6);
}
.modal::-webkit-scrollbar-thumb,
.table-scroll::-webkit-scrollbar-thumb,
.session-thread::-webkit-scrollbar-thumb,
.task-side::-webkit-scrollbar-thumb,
.roles-detail::-webkit-scrollbar-thumb {
  background: rgba(20, 19, 26, 0.28);
  border-color: var(--paper);
}

/* ===== 页签 ===== */
.tabs,
.detail-tabs {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  width: fit-content;
  max-width: 100%;
  margin-bottom: 12px;
  padding: 4px;
  background: var(--ink-800);
  border: var(--stroke) solid var(--ink-line-strong);
  border-radius: var(--radius);
}
.tabs button,
.detail-tabs .tab {
  min-height: 28px;
  padding: 0 10px;
  border: var(--stroke) solid transparent;
  background: transparent;
  color: var(--text-on-ink-muted);
  box-shadow: none;
  font-size: 11px;
  font-weight: 800;
}
.tabs button:hover:not(:disabled),
.detail-tabs .tab:hover:not(:disabled) {
  transform: none;
  background: rgba(247, 239, 227, 0.06);
  color: var(--text-on-ink);
}
.tabs button.active,
.detail-tabs .tab.active {
  background: var(--paper);
  border-color: var(--ink-900);
  color: var(--ink-900);
  box-shadow: var(--shadow-yellow);
}
.tab-count {
  margin-left: 4px;
  padding: 0 5px;
  border: 1.5px solid var(--ink-900);
  border-radius: 99px;
  background: var(--red);
  color: var(--ink-900);
  font-size: 9px;
  font-weight: 900;
}

/* ===== 弹窗 ===== */
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background-color: rgba(20, 19, 26, 0.72);
  background-image: radial-gradient(rgba(247, 239, 227, 0.06) 1px, transparent 1.2px);
  background-size: 7px 7px;
}
.modal {
  width: min(620px, 94vw);
  max-height: 88vh;
  overflow: auto;
  padding: 14px;
  background: var(--paper);
  color: var(--text-on-paper);
  border: 3px solid var(--ink-900);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-3);
}
.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: -14px -14px 12px;
  padding: 12px 14px;
  border-bottom: var(--stroke) solid var(--ink-900);
  background: var(--paper-2);
}
.modal-head h2 { font-size: 15px; font-weight: 900; }
.modal-close {
  width: 26px;
  height: 26px;
  min-height: 0;
  padding: 0;
  border-radius: 50%;
  background: var(--red);
  border: var(--stroke) solid var(--ink-900);
  color: var(--ink-900);
  font-weight: 900;
  box-shadow: 2px 2px 0 var(--ink-900);
}
.modal-close:hover:not(:disabled) { transform: none; background: #ff8f8f; }
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin: 12px -14px -14px;
  padding: 12px 14px;
  border-top: var(--stroke) solid var(--ink-900);
}
.modal input:not([type="checkbox"]),
.modal textarea {
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  color: var(--text-on-paper);
}
.modal .muted { color: var(--text-on-paper-muted); }

/* ===== 成员选择 ===== */
.member-row {
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius-sm);
  box-shadow: 2px 2px 0 rgba(20, 19, 26, 0.16);
  color: var(--text-on-paper);
}
.member-row:hover {
  background: var(--paper-2);
  border-color: var(--ink-900);
}
.member-row.picked {
  background: var(--yellow);
  border-color: var(--ink-900);
  box-shadow: var(--shadow-red);
}
.member-desc { color: var(--text-on-paper-muted); }
.member-avatar {
  border: 2px solid var(--ink-900);
  color: var(--ink-900);
}
.member-check {
  border: var(--stroke) solid var(--ink-900);
  background: var(--paper-2);
  color: var(--ink-900);
}
.member-row.picked .member-check {
  background: var(--ink-900);
  color: var(--yellow);
}

/* ===== 空态 / 提示 ===== */
.empty {
  padding: 22px 14px;
  border: 2px dashed var(--ink-line-strong);
  border-radius: var(--radius);
  background: rgba(247, 239, 227, 0.04);
  color: var(--text-on-ink-muted);
  text-align: center;
}
.card .empty,
.kanban-col .empty,
.roles-detail .empty,
.modal .empty {
  border-color: var(--paper-2);
  background: var(--paper-3);
  color: var(--text-on-paper-muted);
}
.session-thread .empty {
  border-color: var(--ink-line-strong);
  background: transparent;
  color: var(--text-on-ink-muted);
}
.empty-icon {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  margin: 0 auto 8px;
  border: var(--stroke) solid var(--ink-line-strong);
  border-radius: 12px;
  background: radial-gradient(circle at 30% 20%, rgba(250, 204, 21, 0.35), rgba(34, 211, 238, 0.12));
  color: var(--yellow);
}
.review-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: var(--stroke) solid var(--ink-900);
  border-left-width: 8px;
  border-left-color: var(--yellow);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--ink-900);
  box-shadow: var(--shadow-1);
}
.review-banner-text { flex: 1; font-weight: 800; color: var(--ink-900); }

/* ===== 角色头像 ===== */
.role-avatar {
  display: grid;
  place-items: center;
  flex: none;
  overflow: hidden;
  border: var(--stroke) solid var(--ink-900);
  border-radius: 50%;
  box-shadow: 2px 2px 0 var(--cyan);
  color: var(--ink-900);
  font-weight: 900;
}
.role-avatar-image { background: var(--paper); }
.role-avatar-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 50%;
}
.role-card { display: flex; align-items: center; gap: 8px; padding: 8px; }
.role-name { font-weight: 800; font-size: 12px; }
.role-sub { color: var(--text-on-paper-muted); font-size: 10px; }
.status-light {
  width: 8px;
  height: 8px;
  border: 1.5px solid var(--ink-900);
  border-radius: 50%;
}
.status-light.running { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 1.6s infinite; }
.status-light.idle { background: var(--text-on-paper-muted); }
.status-light.error { background: var(--red); box-shadow: 0 0 8px var(--red); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* ===== 角色点灯（保留五色语义） ===== */
.role-live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-on-paper-muted); flex: none; }
.role-live-dot.green { background: var(--green); box-shadow: 0 0 0 3px rgba(74, 222, 128, 0.18); animation: role-live-pulse 1.8s ease-in-out infinite; }
.role-live-dot.red { background: var(--red); box-shadow: 0 0 0 3px rgba(248, 113, 113, 0.18); animation: role-live-pulse 1.8s ease-in-out infinite; }
.role-live-dot.amber { background: var(--yellow); box-shadow: 0 0 0 3px rgba(250, 204, 21, 0.18); animation: role-live-pulse 1.1s ease-in-out infinite; }
.role-live-dot.blue { background: var(--blue); box-shadow: 0 0 0 3px rgba(96, 165, 250, 0.16); }
.role-live-dot.gray { background: var(--text-on-paper-muted); }
.role-live-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px 2px 5px;
  border: 1.5px solid var(--ink-900);
  border-radius: 99px;
  background: var(--paper-2);
  color: var(--ink-900);
  font-size: 10px;
}
.role-live-name { font-family: var(--font-mono); font-size: 9px; color: var(--text-on-paper-soft); }
.role-live-queue { color: var(--red-on-paper); background: rgba(185, 28, 28, 0.08); }
.role-live-chip .status::before { display: none; }
.role-live-legend { color: var(--text-on-paper-muted); }
@keyframes role-live-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

/* ===== 子任务 / run 详情纸面化 ===== */
.rs-row-wrap {
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-1);
  color: var(--text-on-paper);
}
.rs-row { background: transparent; color: var(--text-on-paper); }
.rs-detail {
  background: var(--paper);
  border-top: 1.5px solid var(--paper-2);
  color: var(--text-on-paper);
}
.rs-detail-meta, .rs-summary { color: var(--text-on-paper-muted); }
.rs-chevron { color: var(--text-on-paper-muted); }
.rs-timeline li { border-bottom-color: var(--paper-2); }
.run-timeline li {
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius-sm);
  color: var(--text-on-paper);
}

/* ===== 活动 / 右栏 ===== */
.activity { list-style: none; margin: 0; padding: 0; }
.activity li {
  margin: 0;
  padding: 6px 2px;
  border: 0;
  border-bottom: 1.5px dashed var(--ink-line);
  border-radius: 0;
  background: transparent;
  color: var(--text-on-ink-muted);
  font-family: var(--font-mono);
  font-size: 9px;
}
.activity .act-meta { color: var(--text-on-ink-faint); }
.activity .act-body { color: var(--text-on-ink); }
.run-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
  margin-bottom: 7px;
}
.run-title { color: var(--text-on-paper); font-size: 11px; font-weight: 800; }
.run-sub { color: var(--text-on-paper-muted); font-size: 9px; }
.run-live { color: var(--green); }
.lobby-side {
  background: var(--ink-800);
  border: var(--stroke) solid var(--ink-line-strong);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
  color: var(--text-on-ink);
}
.lobby-side .pane-title { color: var(--cyan); }
.pane-title {
  color: var(--text-on-ink-muted);
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.pane-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.icon-btn {
  min-height: 26px;
  padding: 0 8px;
  background: var(--paper);
  border: var(--stroke) solid var(--ink-900);
  color: var(--ink-900);
  box-shadow: 2px 2px 0 rgba(20, 19, 26, 0.2);
}

/* ===== 任务详情 ===== */
.session-back a {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-on-ink-muted);
  font-size: 11px;
  font-weight: 700;
}
.session-head {
  padding: 12px 14px;
  background: var(--paper);
  color: var(--text-on-paper);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
}
.session-title h1 { color: var(--ink-900); font-size: 24px; font-weight: 900; letter-spacing: -0.03em; }
.session-meta { color: var(--text-on-paper-muted); font-size: 10px; }
.session-meta-item { display: inline-flex; align-items: center; gap: 3px; }
.session-head .muted { color: var(--text-on-paper-muted); }
.session-head .mono { color: var(--text-on-paper-soft); }
.task-sheet { margin-bottom: 12px; }
.thread-item {
  display: flex;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1.5px dashed var(--paper-2);
}
.thread-avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  flex: none;
  border: var(--stroke) solid var(--ink-900);
  border-radius: 50%;
  box-shadow: 2px 2px 0 var(--cyan);
  color: var(--ink-900);
  font-weight: 900;
}
.thread-avatar.mention-avatar { background: var(--yellow); }
.thread-avatar.human-avatar { background: var(--cyan); }
.thread-role-avatar { width: 32px; height: 32px; }
.thread-item.is-message .thread-body {
  padding: 8px 10px;
  border: var(--stroke) solid var(--ink-900);
  border-radius: 2px 10px 10px 10px;
  background: var(--paper-3);
  color: var(--ink-900);
  box-shadow: var(--shadow-1);
}
.thread-item.is-human .thread-body {
  background: var(--yellow);
  box-shadow: var(--shadow-red);
}
.thread-item.is-mention {
  border-color: var(--yellow);
  background: rgba(250, 204, 21, 0.06);
  border-radius: var(--radius-sm);
  padding: 8px;
}
.thread-item.is-system-event {
  display: block;
  padding: 10px 0;
  border-bottom: 0;
  text-align: center;
}
.thread-item.is-system-event .thread-body {
  border: 0;
  background: transparent;
  box-shadow: none;
  padding: 0;
}
.thread-item.is-system-event .thread-meta {
  justify-content: center;
  color: var(--text-on-ink-muted);
  font-family: var(--font-mono);
  font-size: 9px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.thread-item.is-system-event .thread-text {
  color: var(--text-on-ink-muted);
  font-size: 10px;
}
.thread-meta { color: var(--text-on-paper-muted); }
.thread-item.is-message .thread-meta { color: var(--text-on-paper-muted); }
.thread-meta strong { color: var(--text-on-paper); }
.thread-text { color: inherit; }
.mention-tag {
  display: inline-block;
  padding: 0 5px;
  border: 1.5px solid var(--ink-900);
  border-radius: 4px;
  background: var(--cyan);
  color: var(--ink-900);
  font-weight: 900;
}
.button-like {
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius-sm);
  color: var(--ink-900);
  text-decoration: none;
}
.session-compose {
  padding: 7px;
  background: var(--paper);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius);
  box-shadow: var(--shadow-1);
}
.session-compose input {
  border: var(--stroke) solid var(--ink-900);
  background: var(--paper-3);
}
.compose-at {
  display: grid;
  place-items: center;
  width: 32px;
  min-height: 32px;
  padding: 0;
}
.mention-menu {
  background: var(--paper);
  border: var(--stroke) solid var(--ink-900);
  box-shadow: var(--shadow-2);
}
.mention-item {
  border: 0;
  background: transparent;
  color: var(--text-on-paper);
  text-align: left;
}
.mention-item.active,
.mention-item:hover {
  background: var(--yellow);
  color: var(--ink-900);
}
.mention-item-slug { color: var(--text-on-paper-muted); }
.thread-item.is-human .thread-meta,
.thread-item.is-human .muted { color: var(--text-on-paper-soft); }
.task-side {
  padding: 12px;
  background: var(--paper);
  color: var(--text-on-paper);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
}
.task-side .side-h {
  margin-top: 14px;
  padding-bottom: 4px;
  border-bottom: 1.5px solid var(--paper-2);
  color: var(--text-on-paper-soft);
}
.task-side .side-desc,
.task-side .side-p,
.task-side .side-list li { color: var(--text-on-paper); }
.task-side .muted { color: var(--text-on-paper-muted); }
.task-side .pane-title,
.roles-detail .pane-title { color: var(--text-on-paper-soft); }
.task-meta dt { color: var(--text-on-paper-muted); }
.task-meta dd { color: var(--text-on-paper); }
.side-roles .side-role { border-bottom: 1.5px dashed var(--paper-2); }
.side-role-detail, .side-role-summary { color: var(--text-on-paper-muted); }

/* ===== 角色工坊 ===== */
.create-role-panel {
  margin-bottom: 16px;
  padding: 14px;
  background: var(--paper);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
}
.create-role-panel .toolbar { margin-bottom: 10px; }
.create-role-panel .role-slug-hint { margin: 2px 2px 10px; font-size: 10px; }
.roles-list a { color: var(--text-on-paper); }
.roles-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
}
.roles-list li.active {
  border-color: var(--ink-900);
  background: var(--paper-3);
  box-shadow: var(--shadow-yellow);
}
.role-list-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
  color: var(--text-on-paper);
}
.role-list-text { min-width: 0; }
.role-list-text small { color: var(--text-on-paper-muted); }
.roles-detail {
  padding: 14px;
  background: var(--paper);
  color: var(--text-on-paper);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
}
.role-detail-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: var(--stroke) solid var(--ink-900);
}
.role-detail-avatar {
  min-height: 0;
  padding: 0;
  border: 0;
  background: transparent;
  box-shadow: none;
}
.role-detail-avatar:hover:not(:disabled) { transform: none; box-shadow: none; }
.role-detail-title { min-width: 0; }
.role-detail-title h2 { color: var(--ink-900); font-size: 18px; font-weight: 900; }
.role-detail-title .mono { color: var(--text-on-paper-muted); font-size: 10px; }
.roles-detail .muted { color: var(--text-on-paper-muted); }
.roles-detail dl { grid-template-columns: minmax(120px, 180px) minmax(0, 1fr); }
.roles-detail dt { color: var(--text-on-paper-muted); }
.roles-detail dd { color: var(--text-on-paper); }

/* ===== 头像选择器 ===== */
.avatar-picker-inline {
  margin-top: 6px;
  padding-top: 10px;
  border-top: 1.5px dashed var(--paper-2);
}
.avatar-picker-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}
.avatar-picker-label {
  color: var(--text-on-paper);
  font-size: 11px;
  font-weight: 900;
}
.avatar-picker-head small {
  color: var(--text-on-paper-muted);
  font-size: 9px;
}
.avatar-grid {
  display: grid;
  grid-template-columns: repeat(8, minmax(0, 1fr));
  gap: 7px;
}
.avatar-picker-inline .avatar-grid { max-width: 560px; }
.avatar-tile {
  position: relative;
  min-height: 0;
  aspect-ratio: 1;
  padding: 2px;
  border: var(--stroke) solid var(--ink-900);
  border-radius: 50%;
  background: var(--paper-3);
  box-shadow: 2px 2px 0 rgba(20, 19, 26, 0.2);
}
.avatar-tile:hover:not(:disabled) {
  transform: translate(-1px, -1px);
  box-shadow: var(--shadow-2);
}
.avatar-tile img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 50%;
}
.avatar-tile.selected { box-shadow: var(--shadow-yellow); }
.avatar-tile.selected::after {
  content: "✓";
  position: absolute;
  right: -5px;
  bottom: -5px;
  display: grid;
  place-items: center;
  width: 17px;
  height: 17px;
  border: var(--stroke) solid var(--ink-900);
  border-radius: 50%;
  background: var(--yellow);
  color: var(--ink-900);
  font-size: 9px;
  font-weight: 900;
}
.avatar-tile-initials {
  display: grid;
  place-items: center;
  background: var(--cyan);
  color: var(--ink-900);
  font-size: 16px;
  font-weight: 900;
}
.avatar-picker-hint {
  margin-bottom: 12px;
  color: var(--text-on-paper-muted);
  font-size: 11px;
  font-weight: 600;
}
.avatar-modal .avatar-grid { grid-template-columns: repeat(8, minmax(0, 1fr)); }
@media (max-width: 560px) {
  .avatar-grid,
  .avatar-modal .avatar-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}

/* ===== 工作流页面的纸面化 ===== */
.wf-step,
.wf-node,
.wf-flow-phase {
  background: var(--paper-3);
  border: var(--stroke) solid var(--ink-900);
  border-radius: var(--radius-sm);
  color: var(--ink-900);
  box-shadow: var(--shadow-1);
}
.wf-node.selected { box-shadow: var(--shadow-yellow); }
.wf-node:hover { transform: translate(-1px, -1px); box-shadow: var(--shadow-2); }
.wf-graph-scroll {
  background: var(--ink-800);
  border: var(--stroke) solid var(--ink-line-strong);
}
.wf-role, .wf-deps, .wf-summary, .wf-node-role { color: var(--text-on-paper-muted); }
.wf-id, .wf-node-id, .wf-node-role, .wf-flow-phase-role,
.wf-flow-phase-count, .wf-flow-phase-sub { color: var(--text-on-paper-muted); }
.wf-flow-current { color: var(--ink-900); }
.wf-gate { background: var(--yellow); color: var(--ink-900); }
.wf-loop-progress { background: var(--paper-2); }
.wf-loop-list,
.wf-slice-list,
.wf-slice-step-list,
.wf-slice-item-summary,
.wf-slice-step,
.wf-detail {
  background: var(--paper-3);
  color: var(--text-on-paper);
  border-color: var(--paper-2);
}
.wf-loop-list > summary,
.wf-slice-details summary,
.wf-slice-steps summary { color: var(--text-on-paper-soft); }
.wf-step .muted,
.wf-detail .muted,
.wf-loop-list .muted,
.wf-slice-list .muted,
.wf-slice-item .muted { color: var(--text-on-paper-muted); }
.wf-detail-text { color: var(--text-on-paper); }
.wf-node-gate { color: var(--red); }
.wf-slice-row-btn { color: var(--text-on-paper); }
.wf-slice-row-btn:hover { background: var(--paper-hover); }
.wf-slice-row-btn.active { background: var(--paper-2); }
.wf-slice-step.active { background: var(--paper-2); }
.wf-matrix-cell {
  background: var(--paper-2);
  border: 1.5px solid var(--ink-900);
  color: var(--text-on-paper);
}
.run-log {
  background: var(--ink-900);
  color: var(--text-on-ink);
  border: var(--stroke) solid var(--ink-line-strong);
  border-radius: var(--radius-sm);
}
```

- [ ] **Step 2: 检查 CSS 括号是否平衡**

```bash
python3 - <<'PY'
from pathlib import Path
path = Path("dev/frontend/src/styles/components.css")
text = path.read_text()
assert text.count("{") == text.count("}"), (text.count("{"), text.count("}"))
print("brace counts balanced")
PY
```

Expected: `brace counts balanced`。完整语法验证在 Task 12 切换导入后由 Vite 完成。

- [ ] **Step 3: 提交**

```bash
git add dev/frontend/src/styles/components.css
git commit -m "feat(ui): add comic component styles

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

#### Review hardening（2026-10-24 质量审查追加）

质量审查发现 WorkflowGraph 的内联深色背景与纸面化冲突、线程审计行被误套纸面文字色。追加提交 `fix(ui): keep workflow and audit rows readable`：

- 线程：只有 `.is-message` 的消息气泡使用纸面文字；`is-human` 非消息事件不再填充黄色；审计/系统事件保持深色画布浅色文字。
- 纸面按钮/链接/焦点：`button.danger` 用 `--red-on-paper`，`.run-live` 用新增 `--green-on-paper`，纸面容器焦点环改墨色，modal 输入 focus 覆盖旧黄色描边。
- 通用 `button:hover` 的状态保护：`.drawer-mask`、`.member-row.picked`、`.mention-item.active`、`.avatar-tile.selected`、`.wf-slice-row-btn`。
- 工作流：图谱节点保留内联深色背景并改浅色文字；`.wf-flow-phase` 实际在纸面 `.wf-detail` 内，文字保持墨色；`.wf-node-title` 允许换行；纸面切片行/详情里的内联状态色强制墨色；`.wf-flow-arrow`/`.wf-loop-phases` 改纸面 muted；`.wf-loop-mark` 改墨色。
- 补充：纸面焦点列表扩展到 review-banner / rs-row-wrap / wf-detail / mention-menu / session-compose / roles-list / run-timeline / 切片列表；修正 wf-slice-row-btn / avatar-tile.selected / mention-item 的 hover 竞态；kanban-empty 改用纸面可见描边。

---

### Task 11: layout.css（壳层与响应式）

**Files:**
- Create: `dev/frontend/src/styles/layout.css`

- [ ] **Step 1: 写 layout.css**

```css
.app-shell {
  display: grid;
  grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
  height: 100vh;
  height: 100dvh;
}
.app-shell.sidebar-collapsed {
  grid-template-columns: var(--sidebar-rail-w) minmax(0, 1fr);
}

.sidebar {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 10px;
  background: var(--ink-800);
  border-right: var(--stroke) solid var(--text-on-ink);
}
.sidebar .brand { padding: 2px 8px 12px; }
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-on-ink);
  font-size: 15px;
  font-weight: 900;
  letter-spacing: -0.02em;
}
.brand .dot { display: none; }
.brand-name {
  display: inline-block;
  padding: 2px 8px;
  border: var(--stroke) solid var(--ink-900);
  background: var(--yellow);
  color: var(--ink-900);
  font-size: 13px;
  font-weight: 900;
  letter-spacing: -0.02em;
  text-transform: uppercase;
  transform: rotate(-2deg);
  box-shadow: 2px 2px 0 var(--red);
}
.app-shell.sidebar-collapsed .brand .dot {
  display: block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--ink-900);
  border-radius: 2px;
  background: var(--yellow);
  box-shadow: 2px 2px 0 var(--red);
}
.sidebar-nav { display: flex; flex-direction: column; gap: 16px; }
.nav-group { display: flex; flex-direction: column; gap: 2px; }
.nav-label {
  padding: 0 9px 4px;
  color: var(--text-on-ink-faint);
  font-family: var(--font-mono);
  font-size: 8px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 34px;
  padding: 0 9px;
  border: var(--stroke) solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-on-ink-muted);
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}
.nav-item:hover {
  background: rgba(247, 239, 227, 0.06);
  color: var(--text-on-ink);
  text-decoration: none;
}
.nav-item.active {
  background: var(--paper);
  border-color: var(--ink-900);
  color: var(--ink-900);
  box-shadow: var(--shadow-yellow);
  font-weight: 800;
}
.nav-item.active::before {
  content: "";
  position: absolute;
  left: -1px;
  top: 6px;
  bottom: 6px;
  width: 3px;
  border-radius: 2px;
  background: var(--red);
}
.nav-icon {
  display: grid;
  place-items: center;
  flex: none;
  width: 20px;
  height: 20px;
  color: var(--cyan);
}
.nav-item.active .nav-icon { color: var(--ink-900); }
.nav-text { min-width: 0; }
.nav-badge {
  margin-left: auto;
  padding: 0 5px;
  border: 1.5px solid var(--ink-900);
  border-radius: 3px;
  background: var(--red);
  color: var(--ink-900);
  font-size: 9px;
  font-weight: 900;
}
.sidebar-foot {
  margin-top: auto;
  padding-top: 12px;
  border-top: var(--stroke) solid var(--ink-line);
}
.rail-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  padding: 8px;
  border: var(--stroke) solid var(--ink-line-strong);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-on-ink-muted);
  box-shadow: none;
}
.rail-btn:hover:not(:disabled) {
  background: rgba(247, 239, 227, 0.06);
  color: var(--text-on-ink);
}
.app-shell.sidebar-collapsed .nav-text,
.app-shell.sidebar-collapsed .nav-label,
.app-shell.sidebar-collapsed .nav-badge,
.app-shell.sidebar-collapsed .brand-name { display: none; }
.app-shell.sidebar-collapsed .nav-item { justify-content: center; padding: 0; }
.app-shell.sidebar-collapsed .sidebar .brand { justify-content: center; padding: 0; }
.app-shell.sidebar-collapsed .rail-btn { justify-content: center; }

.app-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.app-header {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--header-h);
  padding: 0 var(--content-pad);
  border-bottom: var(--stroke) solid var(--text-on-ink);
  background: var(--ink-900);
}
.page-crumb {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--text-on-ink);
  font-weight: 800;
}
.page-crumb .muted {
  color: var(--text-on-ink-muted);
  font-size: 10px;
  font-weight: 600;
}
.header-spacer { flex: 1; }
.hamburger {
  display: none;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  padding: 0;
  border: var(--stroke) solid var(--ink-line-strong);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-on-ink);
  box-shadow: none;
}
.content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--content-pad);
}
.content > h1 {
  margin: 0 0 14px;
  font-size: var(--fs-h1);
  font-weight: 900;
  letter-spacing: -0.03em;
}
.content > h2 { font-size: 1.05rem; }
.drawer-mask {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 50;
  padding: 0;
  border: 0;
  background: rgba(20, 19, 26, 0.6);
  cursor: default;
}

/* 大厅 */
.lobby {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 6px var(--panel-w);
  gap: 0;
  align-items: start;
}
.lobby.panel-collapsed { grid-template-columns: minmax(0, 1fr); }
.lobby.panel-collapsed > .splitter,
.lobby.panel-collapsed > .lobby-side { display: none; }
.lobby-main { min-width: 0; }
.lobby-side {
  min-width: 0;
  max-height: calc(100dvh - var(--header-h) - (var(--content-pad) * 2));
  overflow-y: auto;
  padding: 12px;
  position: sticky;
  top: 0;
}

/* 任务详情 */
.task-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 6px var(--panel-w);
  gap: 0;
  align-items: stretch;
  height: calc(100dvh - var(--header-h) - (var(--content-pad) * 2) - 200px);
  min-height: 460px;
}
.task-layout.panel-collapsed { grid-template-columns: minmax(0, 1fr); }
.task-layout.panel-collapsed > .splitter,
.task-layout.panel-collapsed > .task-side { display: none; }
.task-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.task-main > .session-actions,
.task-main > .session-thread:not(.session-thread-main),
.task-main > p { flex: none; }
.task-side {
  position: sticky;
  top: 0;
  max-height: calc(100dvh - var(--header-h) - (var(--content-pad) * 2));
  overflow-y: auto;
}
.splitter {
  position: relative;
  cursor: col-resize;
  touch-action: none;
}
.splitter::before {
  content: "";
  position: absolute;
  left: 2px;
  top: 0;
  bottom: 0;
  width: 2px;
  border-radius: 2px;
  background: var(--ink-line-strong);
}
.splitter:hover::before,
.splitter.dragging::before {
  background: var(--yellow);
  box-shadow: 0 0 8px var(--yellow);
}
.splitter:focus-visible {
  outline: 2px solid var(--cyan);
  outline-offset: -2px;
}
.session-thread-main {
  flex: 1 1 auto;
  min-height: 240px;
  max-height: none;
  overflow-y: auto;
}
.session-compose {
  flex: none;
  position: static;
  margin-top: 8px;
  z-index: auto;
}
.thread-body { max-width: 1600px; }

/* 角色工坊 / 表格 */
.roles-layout {
  display: grid;
  grid-template-columns: clamp(240px, 18vw, 320px) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}
.roles-detail { min-width: 0; }
dl {
  display: grid;
  grid-template-columns: minmax(120px, 180px) minmax(0, 1fr);
  row-gap: 6px;
}
dt { font-weight: 700; }
dd { margin: 0; }
.table-scroll { width: 100%; overflow-x: auto; }
.table-scroll .board { min-width: 720px; }
.table-scroll .history-table { min-width: 760px; }

@media (min-width: 1500px) {
  .drafts {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(480px, 1fr));
    gap: 12px;
  }
  .drafts > li { margin-bottom: 0; }
}

@media (max-width: 1199px) {
  .app-shell,
  .app-shell.sidebar-collapsed {
    grid-template-columns: var(--sidebar-rail-w) minmax(0, 1fr);
  }
  .sidebar .nav-text,
  .sidebar .nav-label,
  .sidebar .nav-badge,
  .sidebar .brand-name { display: none; }
  .sidebar .nav-item { justify-content: center; padding: 0; }
  .sidebar .brand { justify-content: center; padding: 0; }
  .sidebar .brand .dot {
    display: block;
    width: 12px;
    height: 12px;
    border: 2px solid var(--ink-900);
    border-radius: 2px;
    background: var(--yellow);
    box-shadow: 2px 2px 0 var(--red);
  }
  .sidebar .rail-btn { justify-content: center; }
}

@media (max-width: 1099px) {
  .lobby,
  .lobby.panel-collapsed,
  .task-layout,
  .task-layout.panel-collapsed { grid-template-columns: minmax(0, 1fr); }
  .lobby > .splitter,
  .task-layout > .splitter { display: none; }
  .lobby-side,
  .task-side {
    position: static;
    max-height: none;
    margin-top: 12px;
  }
  .task-layout { height: auto; min-height: 0; }
  .session-thread-main { max-height: 60vh; }
  .kanban-col { min-width: 230px; }
}

@media (max-width: 899px) {
  :root { --header-h: 52px; }
  .app-shell,
  .app-shell.sidebar-collapsed { grid-template-columns: minmax(0, 1fr); }
  .sidebar {
    position: fixed;
    z-index: 60;
    inset: 0 auto 0 0;
    width: 248px;
    transform: translateX(-105%);
    transition: transform 0.18s ease;
    box-shadow: 20px 0 50px rgba(0, 0, 0, 0.45);
  }
  .app-shell.drawer-open .sidebar { transform: none; }
  .hamburger { display: inline-flex; }
  .app-shell.drawer-open .drawer-mask { display: block; }
  .app-shell .nav-text,
  .app-shell .nav-label,
  .app-shell .nav-badge,
  .app-shell .brand-name { display: initial !important; }
  .app-shell .nav-item {
    justify-content: flex-start !important;
    padding: 0 9px !important;
  }
  .app-shell .sidebar .brand { justify-content: flex-start !important; }
  .app-shell .brand .dot { display: none !important; }
  .app-shell .rail-btn::after { content: none; }
  .session-thread { max-height: none; }
  .roles-layout { grid-template-columns: 1fr; }
  .page-header { flex-direction: column; }
  .page-actions { margin-left: 0; }
  .modal { padding: 12px; }
  .modal-head { margin: -12px -12px 10px; }
  .modal-actions { margin: 10px -12px -12px; }
}

@media (prefers-reduced-motion: reduce) {
  .sidebar,
  .splitter { transition: none !important; }
}
```

- [ ] **Step 2: 检查 CSS 括号是否平衡**

```bash
python3 - <<'PY'
from pathlib import Path
path = Path("dev/frontend/src/styles/layout.css")
text = path.read_text()
assert text.count("{") == text.count("}"), (text.count("{"), text.count("}"))
print("brace counts balanced")
PY
```

Expected: `brace counts balanced`。

- [ ] **Step 3: 提交**

```bash
git add dev/frontend/src/styles/layout.css
git commit -m "feat(ui): add comic app shell and responsive layout

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

#### Review hardening（2026-10-24 质量审查追加）

- Lobby 的 `.lobby` 使用 `align-items: start`，空 `.splitter` 高度为 0；追加 `.lobby > .splitter { align-self: stretch; }` 恢复分隔线和拖拽/键盘命中区。
- 同时修正：`.drawer-mask` 全屏遮罩圆角归零；`.brand` 链接 hover 取消下划线；`.nav-label` 改用 `--text-on-ink-muted` 提升对比度；≤1199 隐藏无意义的 rail 切换按钮；≤899 恢复移动端品牌内边距。

---

### Task 12: 切换样式入口并删除旧 style.css

**Files:**
- Modify: `dev/frontend/src/main.tsx`
- Delete: `dev/frontend/src/style.css`

- [ ] **Step 1: 改 main.tsx**

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/pages.css";
import "./styles/components.css";
import "./styles/layout.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
```

- [ ] **Step 2: 删除旧样式**

```bash
rm dev/frontend/src/style.css
```

- [ ] **Step 3: 构建**

```bash
cd dev/frontend
npm run build
```

Expected: 成功。若 Vite 报 CSS 语法错误，按行号修复 `components.css` / `layout.css` / `pages.css`。

- [ ] **Step 4: 启动后端静态页做一次视觉冒烟**

```bash
cd dev/backend
source ../.venv/bin/activate
python run_server.py
```

打开 `http://127.0.0.1:7317`，检查：

- 侧栏是墨底纸面激活项，页面没有塌陷；
- 大厅看板、表格、弹窗有纸面边框和硬阴影；
- 浏览器控制台无 404（头像素材路径正确）。

- [ ] **Step 5: 提交**

```bash
git add dev/frontend/src/main.tsx
git rm dev/frontend/src/style.css
git commit -m "refactor(ui): switch to layered comic styles

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

#### Review hardening（2026-10-24 真实页面审查追加）

- `.field` 默认改为 `--text-on-ink`，只在 `.modal` / `.create-role-panel` / `.roles-detail` / `.task-side` / `.session-head` 内使用纸面墨色；helper 文本同理。修复 Runtimes 深色页面上「默认运行时」标签 1:1 不可见。
- `.create-role-panel` 补 `color: var(--text-on-paper)`。
- `.history-table thead th` 补 sticky 表头。
- ≤1199 隐藏空 `.sidebar-foot` 条。

---

### Task 13: Icon 组件与导航图标

**Files:**
- Create: `dev/frontend/src/components/Icon.tsx`
- Modify: `dev/frontend/src/App.tsx`

- [ ] **Step 1: 写 Icon.tsx**

```tsx
// Icon paths adapted from Lucide (https://lucide.dev), ISC License.
import type { ReactNode, SVGProps } from "react";

const ICONS = {
  dashboard: <><rect width="7" height="9" x="3" y="3" rx="1" /><rect width="7" height="5" x="14" y="3" rx="1" /><rect width="7" height="9" x="14" y="12" rx="1" /><rect width="7" height="5" x="3" y="16" rx="1" /></>,
  inbox: <><path d="M22 12h-6l-2 3h-4l-2-3H2" /><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></>,
  "git-branch": <><line x1="6" x2="6" y1="3" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" /></>,
  hash: <><line x1="4" x2="20" y1="9" y2="9" /><line x1="4" x2="20" y1="15" y2="15" /><line x1="10" x2="8" y1="3" y2="21" /><line x1="16" x2="14" y1="3" y2="21" /></>,
  users: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></>,
  user: <><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></>,
  cpu: <><rect width="16" height="16" x="4" y="4" rx="2" /><rect width="6" height="6" x="9" y="9" rx="1" /><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2" /></>,
  book: <><path d="M12 7v14" /><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z" /></>,
  bell: <><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" /></>,
  wrench: <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />,
  shield: <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />,
  search: <><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></>,
  plus: <path d="M5 12h14M12 5v14" />,
  send: <><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z" /><path d="m21.854 2.147-10.94 10.939" /></>,
  "at-sign": <><circle cx="12" cy="12" r="4" /><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-3.92 7.94" /></>,
  x: <path d="M18 6 6 18M6 6l12 12" />,
  check: <path d="M20 6 9 17l-5-5" />,
  "chevron-left": <path d="m15 18-6-6 6-6" />,
  "chevron-right": <path d="m9 18 6-6-6-6" />,
  "chevron-down": <path d="m6 9 6 6 6-6" />,
  clock: <><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></>,
  alert: <><circle cx="12" cy="12" r="10" /><line x1="12" x2="12" y1="8" y2="12" /><line x1="12" x2="12.01" y1="16" y2="16" /></>,
  message: <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />,
  "panel-right": <><rect width="18" height="18" x="3" y="3" rx="2" /><path d="M15 3v18" /></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  trash: <><path d="M3 6h18" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></>,
  refresh: <><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" /><path d="M21 3v5h-5" /><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" /><path d="M3 21v-5h5" /></>,
  play: <path d="m6 3 14 9-14 9V3z" />,
  filter: <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />,
  "file-text": <><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z" /><path d="M14 2v4a2 2 0 0 0 2 2h4" /><path d="M16 13H8M16 17H8M10 9H8" /></>,
  terminal: <><polyline points="4 17 10 11 4 5" /><line x1="12" x2="20" y1="19" y2="19" /></>,
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof ICONS;

export function Icon({
  name,
  size = 16,
  strokeWidth = 2,
  className,
  ...rest
}: {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
} & SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      {...rest}
    >
      {ICONS[name]}
    </svg>
  );
}
```

- [ ] **Step 2: App.tsx 导航改用 Icon**

在 `App.tsx` 顶部加：

```tsx
import { Icon, type IconName } from "./components/Icon";
```

把 `NAV_GROUPS` 改成：

```tsx
const NAV_GROUPS = [
  {
    label: "工作台",
    items: [
      { href: "/inbox", label: "收件箱", icon: "inbox" },
      { href: "/", label: "任务大厅", icon: "dashboard" },
      { href: "/decompose", label: "目标拆解", icon: "git-branch" },
      { href: "/channels", label: "频道", icon: "hash" },
    ],
  },
  {
    label: "角色与知识",
    items: [
      { href: "/roles", label: "角色工坊", icon: "users" },
      { href: "/runtimes", label: "运行时", icon: "cpu" },
      { href: "/knowledge", label: "知识库", icon: "book" },
      { href: "/reminders", label: "提醒", icon: "bell" },
    ],
  },
  {
    label: "系统",
    items: [
      { href: "/tooling", label: "工具", icon: "wrench" },
      { href: "/governance", label: "治理", icon: "shield" },
    ],
  },
] as const satisfies ReadonlyArray<{
  label: string;
  items: ReadonlyArray<{ href: string; label: string; icon: IconName }>;
}>;
```

把导航渲染里的：

```tsx
                  <span className="nav-icon" aria-hidden="true">{item.icon}</span>
```

改成：

```tsx
                  <span className="nav-icon" aria-hidden="true">
                    <Icon name={item.icon} />
                  </span>
```

把汉堡按钮的 `☰` 改成：

```tsx
            <Icon name="menu" size={18} />
```

把侧栏折叠按钮的箭头：

```tsx
            <span aria-hidden="true">{sidebarCollapsed ? "→" : "←"}</span>
```

改成：

```tsx
            <Icon name={sidebarCollapsed ? "chevron-right" : "chevron-left"} />
```

- [ ] **Step 3: 构建**

```bash
cd dev/frontend
npm run build
```

Expected: 成功。

- [ ] **Step 4: 提交**

```bash
git add dev/frontend/src/components/Icon.tsx dev/frontend/src/App.tsx
git commit -m "feat(ui): replace unicode nav icons with inline SVG

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 14: 共享 UI 组件与页面标题

**Files:**
- Create: `dev/frontend/src/components/ui.tsx`
- Modify: `dev/frontend/src/pages/ActivityHistory.tsx`
- Modify: `dev/frontend/src/pages/Channels.tsx`
- Modify: `dev/frontend/src/pages/Decompose.tsx`
- Modify: `dev/frontend/src/pages/Governance.tsx`
- Modify: `dev/frontend/src/pages/Knowledge.tsx`
- Modify: `dev/frontend/src/pages/Reminders.tsx`
- Modify: `dev/frontend/src/pages/Roles.tsx`
- Modify: `dev/frontend/src/pages/Runtimes.tsx`
- Modify: `dev/frontend/src/pages/Tooling.tsx`

- [ ] **Step 1: 写 ui.tsx**

```tsx
import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icon";

export function PageHeader({
  kicker,
  title,
  subtitle,
  actions,
}: {
  kicker?: string;
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="page-header-main">
        {kicker && <span className="page-kicker">{kicker}</span>}
        <h1 className="page-title">{title}</h1>
        {subtitle && <div className="page-subtitle">{subtitle}</div>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function SectionHeader({
  title,
  count,
  actions,
}: {
  title: ReactNode;
  count?: number;
  actions?: ReactNode;
}) {
  return (
    <div className="section-header">
      <h2 className="section-title">{title}</h2>
      {count != null && <span className="count">{count}</span>}
      {actions && <span className="actions">{actions}</span>}
    </div>
  );
}

export function EmptyState({
  icon = "inbox",
  title,
  hint,
  action,
}: {
  icon?: IconName;
  title: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon"><Icon name={icon} size={18} /></div>
      <strong>{title}</strong>
      {hint && <p className="muted">{hint}</p>}
      {action && <div style={{ marginTop: 10 }}>{action}</div>}
    </div>
  );
}

export function StatusPill({
  status,
  label,
}: {
  status: string;
  label?: ReactNode;
}) {
  return <span className={`status status-${status}`}>{label ?? status}</span>;
}
```

- [ ] **Step 2: 替换配置页标题**

在下列文件顶部 import：

```tsx
import { PageHeader } from "../components/ui";
```

按下面逐文件替换：

- `ActivityHistory.tsx`：在 `<div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>` 之前插入 `<PageHeader kicker="ACTIVITY" title="活动历史" />`，并删除 toolbar 里的 `<h1 style={{ margin: 0 }}>活动历史</h1>`；筛选控件保留。
- `Channels.tsx`：`<h1>频道</h1>` → `<PageHeader kicker="CHANNELS" title="频道" />`
- `Decompose.tsx`：`<h1>目标拆解</h1>` → `<PageHeader kicker="DECOMPOSE" title="目标拆解" />`
- `Governance.tsx`：`<h1>治理</h1>` → `<PageHeader kicker="GOVERNANCE" title="治理" />`
- `Knowledge.tsx`：`<h1>知识库与提案</h1>` → `<PageHeader kicker="KNOWLEDGE" title="知识库与提案" />`
- `Reminders.tsx`：`<h1>提醒（Reminders）</h1>` → `<PageHeader kicker="REMINDERS" title="提醒" />`
- `Roles.tsx`：`<h1>角色工坊</h1>` → `<PageHeader kicker="CREW WORKSHOP" title="角色工坊" subtitle={`${roles.length} 个角色`} />`
- `Tooling.tsx`：`<h1>工具治理</h1>` → `<PageHeader kicker="TOOLING" title="工具治理" />`
- `Runtimes.tsx`：把 `<h1>运行时</h1>` 和紧随其后的 `<p className="muted">AI 生成画像/角色...当前默认：<strong>...</strong></p>` 合并为：

```tsx
      <PageHeader
        kicker="RUNTIMES"
        title="运行时"
        subtitle={
          <>
            AI 生成画像/角色会复用默认运行时的登录态，无需单独配置 API Key。当前默认：
            <strong>{defaultRuntime?.display_name ?? defaultId ?? "未设置"}</strong>
          </>
        }
      />
```

删除原来的 `<p className="muted">` 段落；其后的默认运行时选择工具栏和扫描/新增按钮保持原位。

- [ ] **Step 3: 构建**

```bash
cd dev/frontend
npm run build
```

Expected: 成功。

- [ ] **Step 4: 提交**

```bash
git add dev/frontend/src/components/ui.tsx dev/frontend/src/pages/ActivityHistory.tsx dev/frontend/src/pages/Channels.tsx dev/frontend/src/pages/Decompose.tsx dev/frontend/src/pages/Governance.tsx dev/frontend/src/pages/Knowledge.tsx dev/frontend/src/pages/Reminders.tsx dev/frontend/src/pages/Roles.tsx dev/frontend/src/pages/Runtimes.tsx dev/frontend/src/pages/Tooling.tsx
git commit -m "feat(ui): add shared page header and empty state primitives

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 15: 任务大厅结构改造

**Files:**
- Modify: `dev/frontend/src/pages/Lobby.tsx`

- [ ] **Step 1: imports 与页头**

顶部加：

```tsx
import { Icon } from "../components/Icon";
import { PageHeader } from "../components/ui";
```

把 return 开头的：

```tsx
    <div>
      <div className="toolbar">
        <button className="primary new-task-btn" onClick={() => setShowNew(true)}>
          <span className="plus">＋</span> 新建任务
        </button>
        {panel.collapsed && (
          <button className="small" onClick={panel.toggle}>显示右栏</button>
        )}
        <small className="muted">选择参与成员后创建；多角色任务会先由 Cindy 规划工作流</small>
      </div>
```

替换为：

```tsx
    <div>
      <PageHeader
        kicker="MISSION BOARD"
        title="任务大厅"
        subtitle={`${roles.length} 个角色 · ${runs.length} 个活跃会话`}
        actions={
          <>
            {panel.collapsed && (
              <button className="small" onClick={panel.toggle}>显示右栏</button>
            )}
            <button className="primary new-task-btn" onClick={() => setShowNew(true)}>
              <Icon name="plus" /> 新建任务
            </button>
          </>
        }
      />
      <p className="muted" style={{ marginBottom: 12 }}>
        选择参与成员后创建；多角色任务会先由 Cindy 规划工作流。
      </p>
```

- [ ] **Step 2: 任务卡结构**

在 `boardTasks.filter(...).map((t) => (` 的 `<div className="card">` 中，把原有 `<a className="card-link">` 内容替换为：

```tsx
                    <a className="card-link" href={`/tasks/${t.id}`} draggable={false}>
                      <strong>{t.title}</strong>
                      <div className="card-meta">
                        {t.assignees?.length ? (
                          <span className="card-roles">
                            {t.assignees.map((slug) => {
                              const role = roles.find((r) => r.slug === slug);
                              return (
                                <RoleAvatar
                                  key={slug}
                                  slug={slug}
                                  name={role?.display_name ?? slug}
                                  avatar={role?.avatar}
                                  color={role?.avatar_color}
                                  size={20}
                                />
                              );
                            })}
                            <span>{t.assignees.join("、")}</span>
                          </span>
                        ) : t.assignee ? (
                          <span className="card-roles">
                            <RoleAvatar
                              slug={t.assignee}
                              name={roles.find((r) => r.slug === t.assignee)?.display_name ?? t.assignee}
                              avatar={roles.find((r) => r.slug === t.assignee)?.avatar}
                              color={roles.find((r) => r.slug === t.assignee)?.avatar_color}
                              size={20}
                            />
                            <span>{t.assignee}</span>
                          </span>
                        ) : (
                          <span className="muted">未指派</span>
                        )}
                        {t.tags?.length > 0 && <span className="muted">#{t.tags.join(" #")}</span>}
                      </div>
                    </a>
```

在每列 map 尾部加空态：

```tsx
                {boardTasks.filter((t) => t.status === status).length === 0 && (
                  <p className="muted kanban-empty">暂无任务</p>
                )}
```

- [ ] **Step 3: 构建 + 冒烟**

```bash
cd dev/frontend
npm run build
```

打开任务大厅，确认：

- 页头有 MISSION BOARD 标签和主按钮；
- 看板列是纸面，卡片有角色头像；
- 空列显示「暂无任务」；
- 右栏会话卡有头像，折叠/拖拽正常。

- [ ] **Step 4: 提交**

```bash
git add dev/frontend/src/pages/Lobby.tsx
git commit -m "feat(ui): restructure the mission board lobby

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 16: 任务详情结构改造

**Files:**
- Modify: `dev/frontend/src/pages/TaskDetail.tsx`

- [ ] **Step 1: imports 与任务单容器**

顶部加：

```tsx
import { Icon } from "../components/Icon";
```

把 return 开头：

```tsx
    <div className="session">
      <p className="session-back"><a href="/">← 任务大厅</a></p>

      {/* 头部 */}
      <div className="session-head">
```

改成：

```tsx
    <div className="session">
      <div className="task-sheet">
      <p className="session-back">
        <a href="/"><Icon name="chevron-left" /> 任务大厅</a>
      </p>

      {/* 头部 */}
      <div className="session-head">
```

并在 `<div className="detail-tabs">` 结束后、`{tab === "chat" && (` 前补一个 `</div>`，让任务单包裹头部/横幅/页签：

```tsx
      </div>

      {tab === "chat" && (
```

- [ ] **Step 2: 元信息换图标**

把：

```tsx
          {task.assignee && <span>👤 {task.assignee}</span>}
          {task.assignees?.length > 0 ? (
            <span>👥 {task.assignees.join("、")}</span>
          ) : !task.assignee ? (
            <span className="muted">未指派</span>
          ) : null}
```

替换为：

```tsx
          {task.assignee && (
            <span className="session-meta-item"><Icon name="user" /> {task.assignee}</span>
          )}
          {task.assignees?.length > 0 ? (
            <span className="session-meta-item"><Icon name="users" /> {task.assignees.join("、")}</span>
          ) : !task.assignee ? (
            <span className="muted">未指派</span>
          ) : null}
```

把待评审横幅里的：

```tsx
          <span className="review-banner-text">🔔 待人工评审 —— 请确认产出后「通过」或「驳回」</span>
```

改成：

```tsx
          <span className="review-banner-text"><Icon name="alert" /> 待人工评审 —— 请确认产出后「通过」或「驳回」</span>
```

- [ ] **Step 3: 输入条加 @ 按钮**

在 `syncMention` 定义之后、`chooseMention` 之前加函数：

```tsx
  const insertMention = () => {
    const el = inputRef.current;
    const pos = el?.selectionStart ?? comment.length;
    const next = comment.slice(0, pos) + "@" + comment.slice(pos);
    setComment(next);
    syncMention(next, pos + 1);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(pos + 1, pos + 1);
    });
  };
```

把：

```tsx
          <div className="session-compose">
            <div className="mention-input-wrap">
```

改成：

```tsx
          <div className="session-compose">
            <button
              type="button"
              className="compose-at"
              onClick={insertMention}
              aria-label="提及角色"
              title="提及角色"
            >
              <Icon name="at-sign" />
            </button>
            <div className="mention-input-wrap">
```

- [ ] **Step 4: 任务档案标题与图标**

把：

```tsx
            <h2 className="pane-title">任务信息</h2>
```

改成：

```tsx
            <h2 className="pane-title">任务档案</h2>
```

把折叠按钮里的 `⇥` 改成：

```tsx
              <Icon name="panel-right" />
```

- [ ] **Step 5: 构建 + 冒烟**

```bash
cd dev/frontend
npm run build
```

打开任务详情，确认：

- 头部纸面任务单、状态章、返回图标；
- 角色消息是纸面气泡，人类消息黄色，系统事件是居中虚线；
- 输入条有 @ 按钮且能插入 `@`；
- 右栏「任务档案」分区显示，角色行有头像；
- 面板拖拽/折叠、自动滚动、筛选仍正常。

- [ ] **Step 6: 提交**

```bash
git add dev/frontend/src/pages/TaskDetail.tsx
git commit -m "feat(ui): restyle task detail as a mission sheet

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

#### Review hardening（2026-10-24 质量审查追加）

- `@` 按钮点击会触发输入框 blur 的 150ms 定时器，导致刚打开的 mention 菜单被关闭。追加 `blurTimerRef`：`insertMention` 先清除定时器；`.compose-at` 加 `onMouseDown preventDefault`。
- 同时改进插入逻辑：有选区时替换选区；光标不在输入框时插入末尾；光标紧跟单词时自动补一个空格；caret 按插入 token 长度更新。

---

### Task 17: 配置/表格页统一 + 响应式与可访问性收尾

**Files:**
- Modify: `dev/frontend/src/**/*.tsx`（清理剩余 unicode 图标）
- Modify: `dev/frontend/src/styles/components.css`（QA 发现纸面/对比度缺口时修）
- Modify: `dev/frontend/src/styles/layout.css`（QA 发现溢出/断点缺口时修）

- [ ] **Step 1: 全局清理 unicode / emoji 图标**

```bash
grep -rnE '[✉▦⑂◉⚙◇◷⌘◈👥👤⏳🔔⇥＋←→↑↓▾▸×☰✅⚠️↩️]' dev/frontend/src --include='*.tsx' || echo "no unicode icons"
```

把剩余命中逐个替换：导航/按钮/状态用 `<Icon name="..." />`，纯文案里的 ✅/⚠️/↩️ 改成「通过」「注意」「驳回」等文字。

- [ ] **Step 2: 检查旧颜色残留**

```bash
grep -rnE '#(0b0e15|10141f|151a28|1b2133|232a3d|2e3850|6d7cff|8a96ff|fdeaea|7a6ff0|0a0d13|0f1117|0ea5a4|475569|f5b301|ef5350|2ecc71|5aa9ff|9aa4b2|3a4560|5b50d6|e08a3c)' dev/frontend/src --include='*.css' --include='*.tsx' || echo "no legacy core colors"
# 再检查旧 rgba 调色板
grep -rnE 'rgba\((109, 124, 255|122, 111, 240|224, 138, 60|239, 83, 80)' dev/frontend/src --include='*.css' --include='*.tsx' || echo "no legacy rgba palettes"
```

Expected: 无输出。`avatarColor.ts` 是 `.ts`，不在检查范围；它保留的头像兜底色是允许的。pages.css 中仅作为 `var()` fallback 的 `#e5e2dc`/`#eee` 等可保留。

- [ ] **Step 3: 响应式走查**

在浏览器里依次检查 1440 / 1200 / 1024 / 900 / 390：

- 侧栏在 1199 以下变 rail；899 以下变抽屉；
- 大厅/任务详情在 1099 以下上下堆叠；
- 表格横向滚动，不撑破页面；
- 头像选择器在 560 以下变 4 列；
- 按钮换行不遮挡。

- [ ] **Step 4: 可访问性检查**

- Tab 遍历：侧栏导航、按钮、输入、头像选择器、弹窗关闭；
- 头像选择器打开后焦点在关闭按钮，Escape 可关闭；
- 所有图标按钮有 `aria-label` 或可见文字；
- 开启系统「减少动态效果」，确认位移动画和脉冲停止；
- 检查正文对比度（纸面墨字、墨面奶油字）。

- [ ] **Step 5: 全量构建与测试**

```bash
cd dev/frontend
npm run build

cd ../..
cd dev
source .venv/bin/activate
python -m pytest tests/api/test_role_avatar.py tests/api/test_roles.py tests/api/test_role_auto.py -v
```

Expected: 前端构建成功；后端相关测试全部 passed。

- [ ] **Step 6: 功能回归**

按清单逐项操作：

1. 角色工坊：手动创建（带头像）→ 详情更换头像 → 首字母方案 → 旧角色默认头像；
2. 任务大厅：新建任务 → 选择成员（头像可见）→ 派发 → 拖拽状态；
3. 任务详情：发送普通消息、@ 提及、筛选、跳到最新、面板折叠/拖拽；
4. 待评审：收件箱入口、通过/驳回；
5. 配置页：运行时扫描、知识库、治理、提醒、工具、频道、活动历史；
6. 移动端：抽屉导航、单列布局、表格滚动。

- [ ] **Step 7: 提交收尾**

```bash
git add dev/frontend/src
git commit -m "test(ui): polish responsive and accessibility details

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 验收清单

- [ ] `roles.avatar` 列迁移成功，旧库自动补列。
- [ ] 创建角色（手动 + AI）可以选头像，响应和 GET 都返回 `avatar`。
- [ ] `PUT /roles/{slug}/avatar` 支持预设、`initials`、`null`，非法值 422，Agent 身份 403。
- [ ] 15 个本地头像 SVG + CREDITS 存在，运行时不请求外网。
- [ ] 角色工坊、任务大厅、任务详情、新建任务、角色会话、Agent 会话都显示角色头像。
- [ ] 全站视觉为漫画工坊：墨黑壳层、纸面内容、2px 墨线、硬阴影、半调网点。
- [ ] 导航和按钮不再有 emoji/unicode 图标。
- [ ] 1440 / 1200 / 1024 / 900 / 390 无横向溢出、无遮挡。
- [ ] 面板拖拽/折叠、抽屉、键盘焦点、reduced-motion 正常。
- [ ] `npm run build` 与相关 `pytest` 全部通过。

---

## 执行说明

- 按 Task 1 → 17 顺序执行；每个 Task 结束都提交，便于回滚。
- **仓库根与提交位置**：所有 git 操作从 `/Users/ikaruga/cctest/aliceAgent` 执行。`dev/` 目录下还有一个嵌套的 `.git`；不要 `cd dev` 后提交。提交前用 `git rev-parse --show-toplevel` 确认输出是外层仓库根。
- **测试命令**：在 `dev` 下使用 `python -m pytest ...`，不要用裸 `pytest`（pytest 9 下会有 `tests` 导入路径问题）。
- Task 8–12 是样式拆分，期间页面可能短暂出现旧/新样式混合；Task 12 切换导入后统一验收。
- 如果后端测试环境缺少依赖，先执行 `cd dev && source .venv/bin/activate && pip install -r backend/requirements.txt`。
- 头像素材生成需要一次网络访问；之后运行期完全离线。
- 遇到与 spec 冲突的情况，以 spec 为准，并在计划文档里补一条说明。
