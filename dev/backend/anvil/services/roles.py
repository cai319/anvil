"""Role CRUD + private home + effective-config + tool budget."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml  # type: ignore

from .. import config
from ..util import ConflictError, jdumps, jloads, now_iso

# Naive token estimate until the real estimator (M3) lands: fixed cost per tool.
TOKENS_PER_TOOL = 500


def create(conn: sqlite3.Connection, *, slug: str, display_name: str,
           definition: dict) -> dict:
    now = now_iso()
    home = _ensure_home(slug, display_name, definition)
    conn.execute(
        """INSERT INTO roles (slug, display_name, avatar_color, home_path,
                              definition_json, created_at, updated_at, enabled)
           VALUES (?,?,?,?,?,?,?,1)
           ON CONFLICT(slug) DO UPDATE SET
             display_name=excluded.display_name,
             avatar_color=excluded.avatar_color,
             definition_json=excluded.definition_json,
             updated_at=excluded.updated_at""",
        (slug, display_name, definition.get("avatar_color"), str(home), jdumps(definition), now, now),
    )
    tools = definition.get("tools") or {}
    conn.execute(
        """INSERT INTO role_tools (role_slug, preset, max_tool_tokens, allowlist_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(role_slug) DO UPDATE SET
             preset=excluded.preset, max_tool_tokens=excluded.max_tool_tokens,
             allowlist_json=excluded.allowlist_json, updated_at=excluded.updated_at""",
        (slug, tools.get("preset"), tools.get("max_tool_tokens"), jdumps(tools.get("allowlist", [])), now, now),
    )
    conn.commit()
    _snapshot_version(conn, slug, display_name, definition, source="create")
    _ensure_weekly_reminder(conn, slug)
    return get(conn, slug)  # type: ignore[return-value]


# ---- 内置 onboarding 助理（Cindy）：人类助理，负责任务分析与工作流设计 -----------------
# owner 2026-10-19：多角色任务应先由「人类助理」分析任务、设计协作 workflow（谁先做什么、
# 依赖顺序、反馈/升级路径），人工批准后再交给参与角色执行——而不是无中心群聊。
ONBOARDING_SLUG = "cindy"

ONBOARDING_DEFINITION: dict = {
    "kind": "onboarding",
    "description": (
        "人类（owner）的工程规划助理：分析任务，为参与角色设计协作工作流（阶段顺序、分工、"
        "依赖、验收标准、反馈与升级路径），经人类批准后交由调度器执行。不亲自实现任务。"
    ),
    # 与其它角色一致：本地单用户环境用 bypassPermissions；否则 --print 模式下
    # 无法交互授权，会停在「等待授权」而不执行 anvil CLI。
    "runtime": {"id": "claude-code", "permission_mode": "bypassPermissions"},
    "workspace": {"repos": []},
    "tools": {},
    "concurrency": {"max_parallel": 1},
    "capabilities": {"planning": 0.95, "coordination": 0.9},
    "responsibilities": ["任务分析", "工作流设计", "角色协作编排", "升级路径定义"],
    "system_prompt": (
        "你是人类（owner）的工程规划助理 Cindy。你的职责不是实现任务，而是分析任务并为参与角色"
        "设计协作工作流。必须明确：谁主导、谁执行哪些子任务、阶段顺序与可并行关系（depends_on）、"
        "每个子任务的验收标准与文件范围（scope_paths）、出问题向谁反馈、何时升级给人类。"
        "典型形态：先由 PM/设计角色完成需求分解与接口基线，再由实现与测试并行推进"
        "（测试可提前编写用例，但执行测试依赖实现），最后集成验证。"
        "工作方式：用 anvil task comment 同步分析结论；用 anvil board draft 提交工作流草案；"
        "用 anvil task mention --to human 请人类批准；批准前不要实现任何子任务。"
    ),
}


def ensure_onboarding(conn: sqlite3.Connection) -> dict:
    """确保内置 onboarding 助理存在，并修复历史版本缺失的执行权限（幂等）。"""
    role = get(conn, ONBOARDING_SLUG)
    if role is None:
        create(conn, slug=ONBOARDING_SLUG, display_name="Cindy · 规划助理",
               definition=dict(ONBOARDING_DEFINITION))
        role = get(conn, ONBOARDING_SLUG)
    else:
        definition = jloads(role.get("definition_json") or "{}", {})
        runtime = definition.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {"id": runtime or "claude-code"}
        if runtime.get("permission_mode") != "bypassPermissions":
            runtime["permission_mode"] = "bypassPermissions"
            definition["runtime"] = runtime
            update(conn, ONBOARDING_SLUG, display_name=role["display_name"],
                   definition=definition)
            role = get(conn, ONBOARDING_SLUG)
    return role  # type: ignore[return-value]


def is_onboarding(conn: sqlite3.Connection, slug: str) -> bool:
    role = get(conn, slug)
    if role is None:
        return False
    return jloads(role["definition_json"], {}).get("kind") == "onboarding"


WEEKLY_REVIEW_PROMPT = (
    "回顾本周完成的任务与 notes，把散落的要点合并进 MEMORY.md，删掉已经过时的内容，"
    "把值得共享的用 `anvil lesson propose` 提出来。检查 MEMORY.md 是否超过 300 行，"
    "超了就精简。"
)


def _ensure_weekly_reminder(conn: sqlite3.Connection, slug: str) -> None:
    """design §8.5：每个角色默认附带一条周复盘 reminder（幂等）。"""
    existing = conn.execute(
        "SELECT 1 FROM reminders WHERE role_slug=? AND kind='cron' AND prompt=?",
        (slug, WEEKLY_REVIEW_PROMPT),
    ).fetchone()
    if existing:
        return
    from . import reminders as reminders_service  # 惰性导入避免循环
    try:
        reminders_service.create_reminder(
            conn, role_slug=slug, kind="cron", cron_expr="0 9 * * 1",
            prompt=WEEKLY_REVIEW_PROMPT,
        )
    except (ValueError, ConflictError):
        pass


def _snapshot_version(conn: sqlite3.Connection, slug: str, display_name: str,
                      definition: dict, source: str = "update") -> int:
    """每次配置变更留一版（design §10 roles/{id}/versions）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM role_versions WHERE slug=?", (slug,)
    ).fetchone()
    version = int(row["v"]) + 1
    conn.execute(
        "INSERT INTO role_versions (slug, version, display_name, definition_json, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (slug, version, display_name, jdumps(definition), source, now_iso()),
    )
    conn.commit()
    return version


def list_versions(conn: sqlite3.Connection, slug: str) -> list[dict]:
    rows = conn.execute(
        "SELECT version, display_name, source, created_at FROM role_versions "
        "WHERE slug=? ORDER BY version DESC", (slug,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn: sqlite3.Connection, slug: str, version: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM role_versions WHERE slug=? AND version=?", (slug, version)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["definition"] = jloads(d.pop("definition_json"), {})
    return d


def rollback(conn: sqlite3.Connection, slug: str, version: int) -> dict | None:
    """回滚到指定版本：恢复 definition + 重写 profile.yaml，并留一版回滚记录。"""
    target = get_version(conn, slug, version)
    if target is None:
        return None
    role = get(conn, slug)
    if role is None:
        return None
    update(conn, slug, display_name=target["display_name"], definition=target["definition"])
    _snapshot_version(conn, slug, target["display_name"], target["definition"],
                      source=f"rollback:{version}")
    return get(conn, slug)


def _ensure_home(slug: str, display_name: str, definition: dict) -> Path:
    home = config.AGENTS_DIR / slug
    home.mkdir(parents=True, exist_ok=True)
    (home / "notes").mkdir(exist_ok=True)
    profile = home / "profile.yaml"
    if not profile.exists():
        _write_profile(slug, display_name, definition)
    for name in ("instruction.md", "MEMORY.md"):
        p = home / name
        if not p.exists():
            p.write_text("", encoding="utf-8")
    return home


def _write_profile(slug: str, display_name: str, definition: dict) -> None:
    """写 profile.yaml（文件是真相）。create/update 共用。"""
    home = config.AGENTS_DIR / slug
    home.mkdir(parents=True, exist_ok=True)
    (home / "notes").mkdir(exist_ok=True)
    (home / "profile.yaml").write_text(
        yaml.safe_dump({"slug": slug, "display_name": display_name, **definition},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def get(conn: sqlite3.Connection, slug: str) -> dict | None:
    r = conn.execute("SELECT * FROM roles WHERE slug=?", (slug,)).fetchone()
    return _row(r) if r else None


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM roles ORDER BY slug")]


def update(conn: sqlite3.Connection, slug: str, **fields) -> dict | None:
    definition = fields.pop("definition", None)
    if definition is not None:
        fields["definition_json"] = jdumps(definition)
    if not fields:
        return get(conn, slug)
    fields = {**fields, "updated_at": now_iso()}
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE roles SET {cols} WHERE slug=?", (*fields.values(), slug))
    conn.commit()
    if definition is not None:
        role = get(conn, slug)
        if role:
            _write_profile(slug, role["display_name"], definition)
            _snapshot_version(conn, slug, role["display_name"], definition, source="update")
    return get(conn, slug)


def delete(conn: sqlite3.Connection, slug: str) -> bool:
    cur = conn.execute("DELETE FROM roles WHERE slug=?", (slug,))
    conn.commit()
    return cur.rowcount > 0


def home_tree(conn: sqlite3.Connection, slug: str) -> dict | None:
    role = get(conn, slug)
    if role is None:
        return None
    home = Path(role["home_path"])
    files = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file()) if home.exists() else []
    return {"slug": slug, "home_path": role["home_path"], "files": files}


def read_home_file(conn: sqlite3.Connection, slug: str, rel_path: str) -> dict | None:
    """只读展示角色 home 内文件（design §10 home/file）。拒绝路径穿越。"""
    role = get(conn, slug)
    if role is None:
        return None
    home = Path(role["home_path"]).resolve()
    target = (home / rel_path).resolve()
    try:
        target.relative_to(home)
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return {"slug": slug, "path": rel_path, "content": content}


def home_history(conn: sqlite3.Connection, slug: str, rel_path: str | None = None) -> list[str]:
    """角色 home 的本地 Git 历史（agents/ 可单独做仓库；无仓库时返回空）。"""
    role = get(conn, slug)
    if role is None:
        return []
    home = Path(role["home_path"])
    agent_root = home.parent
    if not (agent_root / ".git").exists():
        return []
    args = ["git", "-C", str(agent_root), "log", "--oneline", "--", f"{slug}/{rel_path}" if rel_path else slug]
    try:
        import subprocess
        out = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def get_tools(conn: sqlite3.Connection, slug: str) -> dict:
    r = conn.execute("SELECT * FROM role_tools WHERE role_slug=?", (slug,)).fetchone()
    if not r:
        return {"preset": None, "max_tool_tokens": None, "allowlist": [], "skills": [], "mcp": [], "updated_at": None}
    return {
        "preset": r["preset"],
        "max_tool_tokens": r["max_tool_tokens"],
        "allowlist": jloads(r["allowlist_json"], []),
        "skills": jloads(r["skills_json"] if "skills_json" in r.keys() else None, []),
        "mcp": jloads(r["mcp_json"] if "mcp_json" in r.keys() else None, []),
        "updated_at": r["updated_at"],
    }


def update_catalog(conn: sqlite3.Connection, slug: str, skills: list[str], mcp: list[str]) -> dict:
    """保存角色选择的 skill / MCP（全量替换）。"""
    if get(conn, slug) is None:
        return {}
    conn.execute(
        """INSERT INTO role_tools (role_slug, skills_json, mcp_json, created_at, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(role_slug) DO UPDATE SET
             skills_json=excluded.skills_json, mcp_json=excluded.mcp_json, updated_at=excluded.updated_at""",
        (slug, jdumps(skills), jdumps(mcp), now_iso(), now_iso()),
    )
    conn.commit()
    return get_tools(conn, slug)


def get_profile(conn: sqlite3.Connection, slug: str) -> dict:
    """读角色的 system prompt + 职责。"""
    role = get(conn, slug)
    if role is None:
        return {}
    definition = jloads(role["definition_json"], {})
    return {
        "system_prompt": definition.get("system_prompt") or "",
        "responsibilities": definition.get("responsibilities") or [],
    }


def update_profile(conn: sqlite3.Connection, slug: str, system_prompt: str, responsibilities: list[str]) -> dict:
    """更新角色的 system prompt + 职责。"""
    role = get(conn, slug)
    if role is None:
        return {}
    definition = jloads(role["definition_json"], {})
    definition["system_prompt"] = system_prompt
    definition["responsibilities"] = responsibilities
    conn.execute("UPDATE roles SET definition_json=?, updated_at=? WHERE slug=?", (jdumps(definition), now_iso(), slug))
    conn.commit()
    return get_profile(conn, slug)


def estimate_tokens(allowlist: list[dict]) -> int:
    """工具描述 token 估算：按条目 JSON 长度 / 3.5，低于固定下限时用固定值。

    比 M1 的「每个工具 500」更接近真实开销（design §2.4 的 len(json)/3.5），
    但仍是估算，不做精确 tokenizer。
    """
    if not allowlist:
        return 0
    total = 0
    for item in allowlist:
        tools = item.get("tools") or []
        if not tools:
            total += TOKENS_PER_TOOL
            continue
        for tool in tools:
            text = jdumps({"server": item.get("server") or item.get("preset"), "tool": tool})
            total += max(80, int(len(text) / 3.5))
    return total


def update_tools(conn: sqlite3.Connection, slug: str, preset: str | None,
                 allowlist: list[dict] | None, max_tool_tokens: int | None) -> tuple[dict, dict | None]:
    """Returns (tools, estimate_dict). estimate_dict non-None means 422."""
    role = get(conn, slug)
    if role is None:
        return {}, None
    current = get_tools(conn, slug)
    new_preset = preset if preset is not None else current["preset"]
    new_allowlist = allowlist if allowlist is not None else current["allowlist"]
    cap = max_tool_tokens if max_tool_tokens is not None else current["max_tool_tokens"]
    estimate = estimate_tokens(new_allowlist)
    if cap is not None and estimate > cap:
        return get_tools(conn, slug), {"estimate": estimate, "max_tool_tokens": cap}
    conn.execute(
        """INSERT INTO role_tools (role_slug, preset, max_tool_tokens, allowlist_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(role_slug) DO UPDATE SET
             preset=excluded.preset, max_tool_tokens=excluded.max_tool_tokens,
             allowlist_json=excluded.allowlist_json, updated_at=excluded.updated_at""",
        (slug, new_preset, cap, jdumps(new_allowlist), now_iso(), now_iso()),
    )
    conn.commit()
    return get_tools(conn, slug), None


def effective_config(conn: sqlite3.Connection, slug: str) -> dict | None:
    role = get(conn, slug)
    if role is None:
        return None
    definition = jloads(role["definition_json"], {})
    tools = get_tools(conn, slug)
    runtime = definition.get("runtime")
    runtime_id = runtime.get("id") if isinstance(runtime, dict) else runtime
    capabilities: dict = {}
    if runtime_id:
        row = conn.execute("SELECT capabilities FROM runtimes WHERE id=?", (runtime_id,)).fetchone()
        if row:
            capabilities = jloads(row["capabilities"], {})
    projection = {
        "runtime": runtime,
        "runtime_capabilities": capabilities,
        "workspace": definition.get("workspace"),
        "tools": tools,
        "concurrency": definition.get("concurrency"),
        "system_prompt": definition.get("system_prompt"),
        "responsibilities": definition.get("responsibilities") or [],
    }
    return {"projection": projection, "effective": projection}


def _row(r) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    d["definition"] = jloads(d.get("definition_json") or "{}", {})  # 供 API/前端读取角色画像
    return d
