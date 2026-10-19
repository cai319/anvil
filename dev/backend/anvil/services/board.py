"""目标拆解（编排者，§2.7/§3.7）：输入目标 → 产出 draft 任务树 → 人类审阅 → 落板。

骨架版 decompose 用启发式模板产出「根任务 + 调研/实现/验证」子树；真实 LLM 拆解后续
接入编排者运行时。人类确认环节不可自动化：draft 必须 approve 才落板。
"""
from __future__ import annotations

import sqlite3

from ..util import jdumps, jloads, new_id, now_iso

# 工作流模板库（SOP 预设，MetaGPT 思路）：每条是一串「步骤」；步骤线性依赖上一步。
# 步骤字段：title（前缀）、type、role（角色 slug）。
WORKFLOW_TEMPLATES: dict[str, list[dict]] = {
    "design-dev-test": [
        {"title": "设计", "type": "design", "role": "designer"},
        {"title": "实现", "type": "implementation", "role": "developer"},
        {"title": "测试", "type": "verification", "role": "tester"},
    ],
    "dev-test": [
        {"title": "实现", "type": "implementation", "role": "developer"},
        {"title": "测试", "type": "verification", "role": "tester"},
    ],
    "research-dev": [
        {"title": "调研", "type": "research", "role": "designer"},
        {"title": "实现", "type": "implementation", "role": "developer"},
    ],
    "fix-test": [
        {"title": "修复", "type": "bugfix", "role": "developer"},
        {"title": "测试", "type": "verification", "role": "tester"},
    ],
}


def list_templates() -> list[dict]:
    """返回工作流模板清单（供前端拆解时选择）。"""
    return [
        {"name": name, "steps": [s["title"] for s in steps]}
        for name, steps in WORKFLOW_TEMPLATES.items()
    ]


def decompose(conn: sqlite3.Connection, goal: str, workspace_id: str | None = None,
              created_by: str = "orchestrator", template: str = "design-dev-test") -> dict:
    if not goal:
        raise ValueError("goal is required")
    steps = WORKFLOW_TEMPLATES.get(template, WORKFLOW_TEMPLATES["design-dev-test"])
    ids = [new_id() for _ in steps]
    children = []
    for i, (step, sid) in enumerate(zip(steps, ids)):
        deps = [ids[i - 1]] if i > 0 else []  # 线性依赖上一步
        children.append({
            "id": sid,
            "title": f"{step['title']} — {goal}",
            "type": step["type"],
            "role": step["role"],
            "depends_on": deps,
        })
    tree = {
        "id": new_id(),
        "title": goal,
        "type": "epic",
        "acceptance": [],
        "template": template,
        "children": children,
    }
    did = new_id()
    conn.execute(
        "INSERT INTO board_drafts (id, workspace_id, goal, tree_json, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (did, workspace_id, goal, jdumps(tree), "pending", created_by, now_iso()),
    )
    conn.commit()
    return get_draft(conn, did)  # type: ignore[return-value]


def create_draft(conn: sqlite3.Connection, goal: str, tree: dict,
                 created_by: str = "orchestrator", workspace_id: str | None = None,
                 source_task: str | None = None, kind: str = "tasks") -> dict:
    """agent（planner）或人创建工作流草案（P1-0 计划先行）。

    tree 节点字段：id/title/type/role/depends_on/acceptance/scope_paths/description；
    树级 leader 与 feedback_rules 落板时并入子任务描述。
    """
    did = new_id()
    conn.execute(
        "INSERT INTO board_drafts (id, workspace_id, goal, tree_json, status, created_by, created_at, source_task, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (did, workspace_id, goal, jdumps(tree), "pending", created_by, now_iso(), source_task, kind),
    )
    conn.commit()
    return get_draft(conn, did)  # type: ignore[return-value]


def get_draft(conn: sqlite3.Connection, did: str) -> dict | None:
    r = conn.execute("SELECT * FROM board_drafts WHERE id=?", (did,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["tree"] = jloads(d.get("tree_json"), {})  # 保留 tree_json（契约），另附解析后 tree
    return d


def list_drafts(conn: sqlite3.Connection, workspace_id: str | None = None,
                source_task: str | None = None) -> list[dict]:
    q = "SELECT * FROM board_drafts"
    clauses, params = [], []
    if workspace_id:
        clauses.append("workspace_id=?")
        params.append(workspace_id)
    if source_task:
        clauses.append("source_task=?")
        params.append(source_task)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC"
    out = []
    for r in conn.execute(q, params):
        d = dict(r)
        d["tree"] = jloads(d.get("tree_json"), {})
        out.append(d)
    return out


def approve(conn: sqlite3.Connection, draft_ids: list[str], by: str = "human") -> dict:
    landed: list[str] = []
    for did in draft_ids:
        draft = get_draft(conn, did)
        if draft is None or draft["status"] != "pending":
            continue
        if draft.get("kind") == "workflow" and draft.get("source_task"):
            # Workflow 草案：创建步骤状态机（步骤不是 task 行）
            from . import workflow as workflow_service
            workflow_service.create_workflow(
                conn, draft["source_task"], draft["tree"], created_by=draft["created_by"])
            n = len((draft.get("tree") or {}).get("steps") or []) or 1
            conn.execute(
                "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (draft["source_task"], "human", "comment",
                 jdumps({"body": f"已批准工作流（{n} 个步骤），执行引擎开始推进。"}),
                 now_iso()),
            )
            conn.execute("UPDATE board_drafts SET status='approved', decided_at=? WHERE id=?",
                         (now_iso(), did))
            landed.append(did)
            continue
        # 兼容旧 tasks 草案：子任务落在其父任务之下
        _land_tree(conn, draft["tree"], parent_id=draft.get("source_task"),
                   workspace_id=draft.get("workspace_id"))
        if draft.get("source_task"):
            # 工作流已批准：父任务作为 epic 进入「进行中」，由子任务执行承载实际工作
            conn.execute(
                "UPDATE tasks SET status='in_progress', updated_at=? WHERE id=? AND status='todo'",
                (now_iso(), draft["source_task"]),
            )
            # 记一条 human 评论：消解收件箱里「请批准」的提及，并在时间线留下决策记录
            n = len((draft.get("tree") or {}).get("children") or []) or 1
            conn.execute(
                "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (draft["source_task"], "human", "comment",
                 jdumps({"body": f"已批准工作流草案（{n} 个子任务），子任务已落板并按依赖自动执行。"}),
                 now_iso()),
            )
        conn.execute("UPDATE board_drafts SET status='approved', decided_at=? WHERE id=?", (now_iso(), did))
        landed.append(did)
    conn.commit()
    return {"landed": landed}


def reject(conn: sqlite3.Connection, draft_id: str, by: str = "human") -> dict:
    conn.execute("UPDATE board_drafts SET status='rejected', decided_at=? WHERE id=?", (now_iso(), draft_id))
    conn.commit()
    return get_draft(conn, draft_id)  # type: ignore[return-value]


def _land_tree(conn: sqlite3.Connection, node: dict, parent_id: str | None,
               workspace_id: str | None, feedback_rules: list[str] | None = None) -> None:
    now = now_iso()
    if feedback_rules is None:
        feedback_rules = node.get("feedback_rules") or []
    children = node.get("children") or []
    # 有 children 的节点是容器（epic），只落地 children（流水线步骤）；叶子节点落地自身。
    for child in (children if children else [node]):
        tid = child.get("id") or new_id()
        desc = child.get("description") or ""
        if feedback_rules:
            desc = (desc + "\n\n" if desc else "") + "反馈路径：\n" + "\n".join(f"- {r}" for r in feedback_rules)
        conn.execute(
            "INSERT INTO tasks (id, title, type, status, parent_id, workspace_id, description, "
            "scope_paths_json, acceptance_json, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, child.get("title", ""), child.get("type"), "todo", parent_id, workspace_id,
             desc or None, jdumps(child.get("scope_paths") or []), jdumps(child.get("acceptance") or []),
             "orchestrator", now, now),
        )
        for dep in child.get("depends_on", []):
            conn.execute("INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?,?)", (tid, dep))
        if child.get("role"):
            conn.execute(
                "INSERT OR IGNORE INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)",
                (tid, child["role"], now),
            )
        for grandchild in child.get("children", []):
            _land_tree(conn, grandchild, parent_id=tid, workspace_id=workspace_id,
                       feedback_rules=feedback_rules)
