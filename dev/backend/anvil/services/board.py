"""目标拆解（编排者，§2.7/§3.7）：输入目标 → 产出 draft 任务树 → 人类审阅 → 落板。

骨架版 decompose 用启发式模板产出「根任务 + 调研/实现/验证」子树；真实 LLM 拆解后续
接入编排者运行时。人类确认环节不可自动化：draft 必须 approve 才落板。
"""
from __future__ import annotations

import re
import sqlite3

from ..util import ConflictError, jdumps, jloads, new_id, now_iso

DRAFT_KINDS = {"tasks", "workflow", "role_suggestion"}
ROLE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

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
    kind: tasks | workflow | role_suggestion。
    """
    kind = str(kind or "tasks")
    if kind not in DRAFT_KINDS:
        raise ConflictError(f"unsupported draft kind: {kind}")
    if kind in ("workflow", "role_suggestion") and not source_task:
        raise ConflictError(f"{kind} draft requires source_task")
    if kind == "workflow":
        # 把 Cindy 的 slices 确定性展开成每片闭环，并在草案阶段就拦下
        # 「大型多角色但无决策点」的串行工作流，让 planner 当场修正。
        from . import workflow as workflow_service
        tree = workflow_service.normalize_definition(conn, tree)
        workflow_service.validate_definition(conn, tree)
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


def _planner_for_task(task: dict, roles_service) -> str:
    if task.get("plan_first") and task.get("planner"):
        return str(task["planner"])
    return roles_service.ONBOARDING_SLUG


def _approve_role_suggestion(conn: sqlite3.Connection, draft: dict, by: str) -> tuple[dict | None, str | None]:
    from .. import config
    from . import roles as roles_service
    from . import tasks as tasks_service
    from . import utility as utility_service

    task_id = draft.get("source_task")
    task = tasks_service.get(conn, task_id) if task_id else None
    if task is None:
        raise ConflictError("role_suggestion source_task not found")

    tree = draft.get("tree") or {}
    suggested = tree.get("suggested_roles") or tree.get("roles") or []
    if not isinstance(suggested, list) or not suggested:
        raise ConflictError("role_suggestion has no suggested_roles")
    if len(suggested) > 10:
        raise ConflictError("too many suggested roles (max 10)")

    existing_slugs: list[str] = []
    to_create: list[tuple[str, str, dict]] = []
    seen: set[str] = set()
    for item in suggested:
        if not isinstance(item, dict):
            raise ConflictError("suggested role must be an object")
        slug = str(item.get("slug") or "").strip()
        display = str(item.get("display_name") or "").strip()
        if not ROLE_SLUG_RE.match(slug):
            raise ConflictError(f"invalid role slug: {slug!r}")
        if not display:
            raise ConflictError(f"role {slug}: display_name required")
        if slug == roles_service.ONBOARDING_SLUG or item.get("kind") == "onboarding":
            raise ConflictError(f"role {slug}: onboarding assistant cannot be suggested")
        if slug in seen:
            continue
        seen.add(slug)
        if roles_service.get(conn, slug) is not None:
            existing_slugs.append(slug)
            continue
        requested_runtime = item.get("runtime")
        if isinstance(requested_runtime, dict):
            requested_runtime = requested_runtime.get("id")
        requested_runtime = str(requested_runtime or "").strip() or None
        try:
            runtime_id = utility_service.resolve_runtime_id(conn, requested_runtime)
        except Exception as exc:  # noqa: BLE001
            raise ConflictError(f"role {slug}: {exc}") from exc
        definition = {
            k: item[k] for k in (
                "description", "workspace", "tools", "concurrency", "capabilities",
                "working_mode", "skill_description", "responsibilities", "system_prompt",
            ) if item.get(k) is not None
        }
        definition["kind"] = "member"
        definition["runtime"] = {"id": runtime_id}
        definition.setdefault("workspace", {"repos": []})
        definition.setdefault("tools", {})
        definition.setdefault("concurrency", {"max_parallel": 2})
        to_create.append((slug, display, definition))

    current_roles = conn.execute("SELECT COUNT(*) AS n FROM roles").fetchone()["n"]
    if current_roles + len(to_create) > config.MAX_ROLES:
        raise ConflictError(
            f"role limit {config.MAX_ROLES} would be exceeded "
            f"({current_roles} + {len(to_create)})")

    created: list[str] = []
    try:
        for slug, display, definition in to_create:
            roles_service.create(conn, slug=slug, display_name=display, definition=definition)
            created.append(slug)
    except Exception as exc:  # noqa: BLE001
        roles_service.rollback_created(conn, created)
        raise ConflictError(f"failed to create suggested roles: {exc}") from exc

    bind = list(dict.fromkeys(existing_slugs + [s for s, _, _ in to_create]))
    try:
        tasks_service.add_assignees(conn, task_id, bind)
    except Exception as exc:  # noqa: BLE001
        roles_service.rollback_created(conn, created)
        raise ConflictError(f"failed to bind suggested roles: {exc}") from exc

    now = now_iso()
    summary = "、".join(bind)
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (task_id, "human", "comment",
         jdumps({"body": f"已批准角色建议，创建/绑定：{summary}；将基于新 Roster 重新规划。"}),
         now),
    )
    conn.execute("UPDATE board_drafts SET status='approved', decided_at=? WHERE id=?",
                 (now, draft["id"]))
    fresh = tasks_service.get(conn, task_id) or {}
    planner = _planner_for_task(fresh, roles_service)
    replan = {
        "task_id": task_id,
        "planner": planner,
        "assignees": fresh.get("assignees") or bind,
        "extra_constraint": "human 已批准新增角色并绑定到任务；请基于新 Roster 输出 workflow 草案。",
        "replace_active": True,
    }
    warning = None if to_create else f"all suggested roles already exist; bound only: {summary}"
    return replan, warning


def approve(conn: sqlite3.Connection, draft_ids: list[str], by: str = "human") -> dict:
    landed: list[str] = []
    replans: list[dict] = []
    warnings: list[str] = []
    for did in draft_ids:
        draft = get_draft(conn, did)
        if draft is None or draft["status"] != "pending":
            continue
        kind = draft.get("kind") or "tasks"
        if kind == "workflow":
            if not draft.get("source_task"):
                raise ConflictError("workflow draft missing source_task")
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
        if kind == "role_suggestion":
            replan, warning = _approve_role_suggestion(conn, draft, by)
            landed.append(did)
            if replan:
                replans.append(replan)
            if warning:
                warnings.append(warning)
            continue
        if kind != "tasks":
            raise ConflictError(f"unsupported draft kind: {kind}")
        # 兼容旧 tasks 草案：子任务落在其父任务之下
        _land_tree(conn, draft["tree"], parent_id=draft.get("source_task"),
                   workspace_id=draft.get("workspace_id"))
        if draft.get("source_task"):
            conn.execute(
                "UPDATE tasks SET status='in_progress', updated_at=? WHERE id=? AND status='todo'",
                (now_iso(), draft["source_task"]),
            )
            n = len((draft.get("tree") or {}).get("children") or []) or 1
            conn.execute(
                "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (draft["source_task"], "human", "comment",
                 jdumps({"body": f"已批准工作流草案（{n} 个子任务），子任务已落板并按依赖自动执行。"}),
                 now_iso()),
            )
        conn.execute("UPDATE board_drafts SET status='approved', decided_at=? WHERE id=?",
                     (now_iso(), did))
        landed.append(did)
    conn.commit()
    return {"landed": landed, "replans": replans, "warnings": warnings}


def reject(conn: sqlite3.Connection, draft_id: str, by: str = "human",
           feedback: str | None = None) -> dict:
    draft = get_draft(conn, draft_id)
    if draft is None:
        raise LookupError(draft_id)
    if draft["status"] != "pending":
        raise ConflictError(f"draft {draft_id} is {draft['status']}, expected pending")
    now = now_iso()
    conn.execute("UPDATE board_drafts SET status='rejected', decided_at=? WHERE id=?",
                 (now, draft_id))
    replan = None
    blocked = None
    if draft.get("kind") == "role_suggestion" and draft.get("source_task"):
        from . import roles as roles_service
        from . import tasks as tasks_service

        task_id = draft["source_task"]
        task = tasks_service.get(conn, task_id)
        if task is None:
            blocked = "task_missing"
        else:
            assignees = task.get("assignees") or []
            if not assignees:
                blocked = "no_roles"
                conn.execute(
                    "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, "workflow", "mention", jdumps({
                        "target": "human",
                        "message": "角色建议已被拒绝，但任务当前没有可用角色；"
                                   "请先创建/绑定至少一个角色后重新派发。",
                        "event": "role_suggestion_rejected",
                    }), now),
                )
            else:
                replan = {
                    "task_id": task_id,
                    "planner": _planner_for_task(task, roles_service),
                    "assignees": assignees,
                    "extra_constraint": (
                        "human 已拒绝新增角色；只能使用现有 Roster 中的角色规划；"
                        "若能力不足，在草案中标注风险，不得虚构角色。"),
                    "replace_active": True,
                }
        if feedback:
            conn.execute(
                "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, "human", "comment",
                 jdumps({"body": f"角色建议被驳回：{feedback}"}), now),
            )
    conn.commit()
    return {"draft": get_draft(conn, draft_id), "replan": replan, "blocked": blocked}


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
