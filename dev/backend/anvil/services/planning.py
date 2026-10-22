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

from ..util import now_iso
from . import roles as roles_service
from . import runs as runs_service
from . import runtime as runtime_service

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
            parts.append("写权限仓库：" + "、".join(r["repos"]))
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
    if extra_constraint.strip():
        prompt += "\n\n## 规划约束（必须遵守）\n" + extra_constraint.strip()
    run = runs_service.create_run(conn, planner, task_id, prompt=prompt)
    if orchestrator:
        try:
            orchestrator.pump(db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    return run
