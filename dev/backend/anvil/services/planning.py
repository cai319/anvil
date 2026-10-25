"""Cindy / planner 的 Roster 构建与 planner run 启动。

角色可能在建任务时已由 human 绑定。Cindy 必须基于 Roster 评估角色是否足够：
- 足够 → 产出 workflow 草案；
- 不足 → 产出 role_suggestion 草案，等待 human 审批。

planner run 的 prompt 以 `[anvil:plan]` 开头；`graph/nodes.py::_prompt_for_run`
据此避免把「已有 workflow 任务上的重规划 run」误判为 REPLY_PROTOCOL。
"""
from __future__ import annotations

import json
import sqlite3

from .. import config
from ..util import now_iso
from . import repo_catalog
from . import roles as roles_service
from . import runs as runs_service
from . import runtime as runtime_service
from . import tasks as tasks_service

PLAN_MARKER = "[anvil:plan]"


def _assignees(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [x["role_slug"] for x in conn.execute(
        "SELECT role_slug FROM task_assignees WHERE task_id=? ORDER BY joined_at", (task_id,))]


def build_roster(conn: sqlite3.Connection, task_id: str,
                 assignees: list[str] | None = None) -> list[dict]:
    """构建规划用 Roster：能力、职责、运行时、负载、写权限、工具。"""
    if assignees is None:
        assignees = _assignees(conn, task_id)
    roster: list[dict] = []
    for slug in assignees:
        role = roles_service.get(conn, slug)
        if role is None:
            roster.append({"slug": slug, "display_name": slug, "missing": True})
            continue
        definition = role.get("definition") or {}
        runtime = definition.get("runtime") or {}
        runtime_id = runtime.get("id") if isinstance(runtime, dict) else runtime
        runtime_row = runtime_service.get(conn, runtime_id) if runtime_id else None
        concurrency = definition.get("concurrency") or {}
        max_parallel = concurrency.get("max_parallel") if isinstance(concurrency, dict) else None
        try:
            max_parallel = int(max_parallel) if max_parallel else None
        except (TypeError, ValueError):
            max_parallel = None
        active_runs = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE role_slug=? AND status IN (?,?,?,?,?,?)",
            (slug, *runs_service.ACTIVE_RUN_STATUSES),
        ).fetchone()["n"]
        workspace = definition.get("workspace") or {}
        repos = workspace.get("repos") if isinstance(workspace, dict) else []
        tools = roles_service.get_tools(conn, slug)
        roster.append({
            "slug": slug,
            "display_name": role.get("display_name") or slug,
            "description": definition.get("description") or "",
            "capabilities": definition.get("capabilities") or {},
            "working_mode": definition.get("working_mode") or "",
            "skill_description": definition.get("skill_description") or "",
            "responsibilities": definition.get("responsibilities") or [],
            "runtime": {
                "id": runtime_id or "",
                "status": (runtime_row or {}).get("status") or "unknown",
            } if runtime_id else {},
            "max_parallel": max_parallel,
            "active_runs": active_runs,
            "repos": list(repos or []),
            "skills": list(tools.get("skills") or []),
            "mcp": list(tools.get("mcp") or []),
        })
    return roster


def _repo_label(repo) -> str:
    """角色 workspace.repos 既支持 "repo-name"，也支持 {name, access} 对象。"""
    if isinstance(repo, dict):
        name = str(repo.get("name") or repo.get("repo") or repo.get("slug") or "").strip()
        access = str(repo.get("access") or "").strip()
        if name and access:
            return f"{name}({access})"
        if name:
            return name
        return json.dumps(repo, ensure_ascii=False)
    return str(repo)


def render_roster(conn: sqlite3.Connection, task_id: str, roster: list[dict]) -> str:
    if not roster:
        return "-（当前未绑定角色）"
    lines: list[str] = []
    for r in roster:
        if r.get("missing"):
            lines.append(f"- {r['slug']}（slug={r['slug']}；角色记录缺失）")
            continue
        parts: list[str] = []
        if r.get("description"):
            parts.append(f"定位：{r['description']}")
        if r.get("capabilities"):
            parts.append("能力：" + json.dumps(r["capabilities"], ensure_ascii=False))
        if r.get("responsibilities"):
            parts.append("职责：" + "、".join(str(x) for x in r["responsibilities"]))
        if r.get("runtime", {}).get("id"):
            parts.append(f"运行时：{r['runtime']['id']}({r['runtime'].get('status')})")
        if r.get("max_parallel"):
            parts.append(f"并发上限：{r['max_parallel']}")
        parts.append(f"当前活跃 run：{r.get('active_runs', 0)}")
        if r.get("repos"):
            parts.append("写权限仓库：" + "、".join(_repo_label(x) for x in r["repos"]))
        if r.get("skills"):
            parts.append("skills：" + "、".join(r["skills"]))
        if r.get("mcp"):
            parts.append("MCP：" + "、".join(r["mcp"]))
        detail = "；".join(p for p in parts if p)
        lines.append(
            f"- {r['display_name']}（slug={r['slug']}，@提及："
            f"anvil task mention {task_id} --to {r['slug']} \"消息\"）"
            + (f"：{detail}" if detail else "")
        )
    return "\n".join(lines)


def _task_size_block(task: dict) -> str:
    """给 planner 的规模提示：帮助它判断是否需要按切片闭环组织。"""
    parts: list[str] = []
    if task.get("type"):
        parts.append(f"- type: {task['type']}")
    if task.get("tags"):
        parts.append("- tags: " + "、".join(str(x) for x in task["tags"]))
    if task.get("repo"):
        parts.append(f"- repo: {task['repo']}")
    if task.get("scope_paths"):
        parts.append("- scope_paths: " + "、".join(str(x) for x in task["scope_paths"]))
    if task.get("acceptance"):
        parts.append(f"- 验收标准数量: {len(task['acceptance'])}")
    hint = " ".join(str(x) for x in (
        task.get("title"), task.get("description"), task.get("type"),
        *(task.get("tags") or []),
    ) if x).lower()
    wide_scope = len(task.get("scope_paths") or []) >= 3
    audit_like = any(k in hint for k in (
        "审计", "安全", "渗透", "代码审查", "review", "audit", "security"))
    if wide_scope or audit_like:
        parts.append(
            "- 规模提示：scope 较宽或审计/审查类任务，必须用 plan_slices + loop 组织："
            "由执行角色在运行时盘点项目后切片，服务端按 loop 模板为每片生成"
            "execute → verify → fix → verify2 → decide 小闭环。不要由规划者预先切分。")
    return "\n".join(parts)


def _task_knowledge_block(conn: sqlite3.Connection, task: dict) -> str:
    parts: list[str] = []
    if task.get("repo"):
        parts.append(f"- 执行 repo：{task['repo']}")
    if task.get("workspace_id"):
        try:
            ws = conn.execute(
                "SELECT name, repo, description FROM workspaces WHERE id=?",
                (task["workspace_id"],)).fetchone()
        except sqlite3.Error:
            ws = None
        if ws is not None:
            parts.append(f"- 频道：{ws['name'] or task['workspace_id']}"
                         + (f"（repo={ws['repo']}）" if ws["repo"] else ""))
            if ws["description"]:
                parts.append(f"- 频道描述：{str(ws['description'])[:300]}")
    if task.get("base_branch"):
        parts.append(f"- base_branch：{task['base_branch']}")
    if task.get("tags"):
        parts.append("- 当前 tags：" + "、".join(str(x) for x in task["tags"]))
    else:
        parts.append("- 当前 tags：（空，请根据任务内容生成 2-6 个）")
    bindings = task.get("knowledge_repos") or []
    if bindings:
        applied = [b["repo"] for b in bindings if b.get("status") == "applied"]
        suggested = [b["repo"] for b in bindings if b.get("status") == "suggested"]
        if applied:
            parts.append("- 已自动绑定知识仓库：" + "、".join(applied))
        if suggested:
            parts.append("- 待确认建议：" + "、".join(suggested))
    if task.get("parent_id"):
        parent = tasks_service.get(conn, task["parent_id"]) or {}
        parts.append(f"- 父任务：{parent.get('title') or task['parent_id']}"
                     + (f"（repo={parent['repo']}）" if parent.get("repo") else ""))
    if task.get("description"):
        parts.append("- 任务描述（截断）：" + str(task["description"])[:1200])
    try:
        from . import task_knowledge as task_knowledge_service
        from . import workspace_knowledge as workspace_knowledge_service
        channel_knowledge = workspace_knowledge_service.render_for_task(
            conn, task, budget_chars=config.CHANNEL_KNOWLEDGE_PLANNING_CHARS)
        if channel_knowledge:
            parts.append("\n" + channel_knowledge)
        if task.get("id"):
            task_shared = task_knowledge_service.render_for_task(
                conn, task["id"], budget_chars=1200)
            if task_shared:
                parts.append("\n" + task_shared)
    except Exception:  # noqa: BLE001 —— 知识注入失败不阻断规划
        pass
    catalog = repo_catalog.build_catalog(conn, exclude_task_id=task.get("id"))
    rendered = repo_catalog.render_catalog_for_prompt(catalog)
    if rendered:
        parts.append("\n知识仓库目录（只能从这里选择；shared 不是候选）：\n" + rendered)
    return "\n".join(parts)


def _workflow_summary(wf: dict) -> str:
    """给重规划 planner 的当前 workflow 摘要（不逐条展开已生成的切片步骤）。"""
    lines = [f"- workflow id: {wf.get('id')}", f"- status: {wf.get('status')}"]
    definition = wf.get("definition") or {}
    loop = definition.get("loop")
    if isinstance(loop, dict):
        phases = []
        for phase in ("scope", "execute", "verify", "fix", "verify2", "decide"):
            spec = loop.get(phase)
            if isinstance(spec, dict):
                phases.append(f"{phase}={spec.get('role') or '-'}")
        if phases:
            lines.append("- loop 模板：" + "；".join(phases))
    steps = wf.get("steps") or []
    static = [s for s in steps if not s.get("slice_key")]
    for s in static[:40]:
        lines.append(
            f"- {s.get('step_id')} [{s.get('kind') or 'task'}] {s.get('title')} "
            f"→ {s.get('role_slug')} ({s.get('status')})")
    if len(static) > 40:
        lines.append(f"- 其余 {len(static) - 40} 个静态步骤省略")
    slice_steps = len(steps) - len(static)
    if slice_steps:
        lines.append(f"- 已展开切片步骤：{slice_steps} 个（按 loop 模板生成，未逐条列出）")
    lines.append(
        "- 如需调整，请提交新的 workflow 草案；人工批准后会终止当前执行并重新开始，"
        "不会中途替换正在运行的 workflow。")
    return "\n".join(lines)


def start_planner_run(conn: sqlite3.Connection, db_path: str, orchestrator,
                      task_id: str, planner: str, assignees: list[str] | None = None,
                      extra_constraint: str = "",
                      replace_active: bool = False) -> dict:
    """启动 planner run（Cindy / 任务 planner）。

    replace_active=False：已有活跃 planner run 直接复用（首次派发幂等）。
    replace_active=True：先 abandon 旧 planner run，再新建（角色建议审批后的重规划）。
    """
    from ..graph.render import PLANNING_PROTOCOL

    if planner == roles_service.ONBOARDING_SLUG:
        roles_service.ensure_onboarding(conn)
    if assignees is None:
        assignees = _assignees(conn, task_id)

    if replace_active:
        for row in conn.execute(
            "SELECT id FROM runs WHERE task_id=? AND role_slug=? "
            "AND status NOT IN ('completed','failed','abandoned')",
            (task_id, planner),
        ).fetchall():
            runs_service.set_status(conn, row["id"], "abandoned", ended_at=now_iso())
    else:
        existing = runs_service.find_active_run(conn, task_id, planner)
        if existing:
            return existing

    roster = build_roster(conn, task_id, assignees)
    prompt = (
        PLAN_MARKER + "\n"
        + PLANNING_PROTOCOL
        + "\n\n## 参与角色（Roster）\n"
        + render_roster(conn, task_id, roster)
    )
    task = tasks_service.get(conn, task_id) or {}
    size_block = _task_size_block(task)
    if size_block:
        prompt += "\n\n## 任务规模信息（用于判断是否需要切片）\n" + size_block
    knowledge_block = _task_knowledge_block(conn, task)
    if knowledge_block:
        prompt += "\n\n## 任务知识与仓库上下文\n" + knowledge_block
    try:
        from . import templates as template_service
        seed_block = template_service.render_seed_for_prompt(conn, task)
    except Exception:  # noqa: BLE001 —— 模板种子失败不应阻断规划
        seed_block = ""
    if seed_block:
        prompt += "\n\n## 工作流模板种子（建议骨架，需结合任务调整）\n" + seed_block
    if extra_constraint.strip():
        prompt += "\n\n## 规划约束（必须遵守）\n" + extra_constraint.strip()
    try:
        from . import workflow as workflow_service
        wf = workflow_service.get_for_task(conn, task_id)
    except Exception:  # noqa: BLE001 —— 摘要失败不影响规划
        wf = None
    if wf:
        prompt += "\n\n## 当前 workflow（只读参考；如需调整请提交新草案）\n" + _workflow_summary(wf)
    run = runs_service.create_run(conn, planner, task_id, prompt=prompt)
    if orchestrator:
        try:
            orchestrator.pump(db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    return run
