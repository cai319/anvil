"""目标拆解（编排者，§2.7/§3.7）：输入目标 → 产出 draft 任务树 → 人类审阅 → 落板。

骨架版 decompose 用启发式模板产出「根任务 + 调研/实现/验证」子树；真实 LLM 拆解后续
接入编排者运行时。人类确认环节不可自动化：draft 必须 approve 才落板。
"""
from __future__ import annotations

import sqlite3

from ..util import ConflictError, jdumps, jloads, new_id, now_iso

DRAFT_KINDS = {"tasks", "workflow", "role_suggestion"}

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
                 source_task: str | None = None, kind: str = "tasks",
                 enforce_role_coverage: bool = True) -> dict:
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
    from . import knowledge_binding
    # agent 草案严格校验 repo 名（防止 Cindy 编造）；人工草案允许安全的新名字。
    knowledge_binding.normalize_draft_tree(
        conn, tree, strict=not str(created_by).startswith("human"))
    if kind == "workflow":
        # 把 Cindy 的 slices 确定性展开成每片闭环，并在草案阶段就拦下
        # 「大型多角色但无决策点」的串行工作流，让 planner 当场修正。
        from . import workflow as workflow_service
        tree = workflow_service.normalize_definition(conn, tree)
        workflow_service.validate_definition(conn, tree)
        if enforce_role_coverage:
            workflow_service.validate_role_coverage(conn, source_task, tree)
        # 把任务的规划种子附着到草案，批准后写进 workflow provenance。
        if source_task:
            try:
                seed_row = conn.execute(
                    "SELECT plan_seed_json FROM tasks WHERE id=?", (source_task,)).fetchone()
                seed = jloads(seed_row["plan_seed_json"]) if seed_row else None
                if isinstance(seed, dict) and seed.get("template_id") and "template_seed" not in tree:
                    tree = dict(tree)
                    tree["template_seed"] = seed
            except (sqlite3.Error, TypeError):
                pass
    did = new_id()
    conn.execute(
        "INSERT INTO board_drafts (id, workspace_id, goal, tree_json, status, created_by, created_at, source_task, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (did, workspace_id, goal, jdumps(tree), "pending", created_by, now_iso(), source_task, kind),
    )
    conn.commit()
    return get_draft(conn, did)  # type: ignore[return-value]


def update_draft(conn: sqlite3.Connection, did: str, tree: dict,
                 goal: str | None = None, workspace_id: str | None = None,
                 by: str = "human") -> dict:
    """人工编辑 pending workflow 草案；仍然保持 pending，等待原 HITL 审批。"""
    draft = get_draft(conn, did)
    if draft is None:
        raise LookupError(did)
    if draft.get("status") != "pending":
        raise ConflictError(f"draft {did} is {draft.get('status')}, expected pending")
    if (draft.get("kind") or "tasks") != "workflow":
        raise ConflictError("only pending workflow drafts can be edited")
    source_task = draft.get("source_task")
    if not source_task:
        raise ConflictError("workflow draft missing source_task")
    from . import workflow as workflow_service
    tree = workflow_service.normalize_definition(conn, tree)
    workflow_service.validate_definition(conn, tree)
    workflow_service.validate_role_coverage(conn, source_task, tree)
    conn.execute(
        "UPDATE board_drafts SET goal=COALESCE(?, goal), workspace_id=COALESCE(?, workspace_id), "
        "tree_json=? WHERE id=?",
        (goal, workspace_id, jdumps(tree), did),
    )
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (source_task, by, "comment",
         jdumps({"body": "人工在模板工作台更新了工作流草案，等待审批。"}), now_iso()),
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


def _authorized_slugs(tree: dict) -> list[str]:
    raw = tree.get("requested_assignees")
    if not isinstance(raw, list):
        raw = tree.get("suggested_roles") or []
    out: list[str] = []
    for item in raw:
        slug = str(item.get("slug") or "").strip() if isinstance(item, dict) else str(item).strip()
        if slug:
            out.append(slug)
    return list(dict.fromkeys(out))


def authorized_assignee_additions(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """返回 human 已批准、但由 planner 自行创建/绑定的角色白名单。"""
    out: set[str] = set()
    rows = conn.execute(
        "SELECT tree_json FROM board_drafts WHERE source_task=? "
        "AND kind='role_suggestion' AND status='approved'", (task_id,)).fetchall()
    for row in rows:
        tree = jloads(row["tree_json"], {})
        if str(tree.get("apply_mode") or "") == "authorize":
            out.update(_authorized_slugs(tree))
    return out


def propose_assignee_additions(conn: sqlite3.Connection, task_id: str, agent: str,
                               roles: list[str], reason: str | None = None) -> dict:
    """任务已有 Roster 时的角色追加提案：复用 role_suggestion，等待 human 审批。

    批准只授权 planner 自行创建/绑定，不替 planner 创建角色（apply_mode=authorize）。
    同一任务已有 pending 提案时合并角色并提示 human，避免重复草案。
    """
    from . import roles as roles_service
    from . import tasks as tasks_service

    requested = list(dict.fromkeys(str(r).strip() for r in roles if str(r).strip()))
    entries = []
    for slug in requested:
        role = roles_service.get(conn, slug) or {}
        entries.append({"slug": slug,
                        "display_name": role.get("display_name") or slug,
                        "reason": reason or "planner 请求追加任务角色"})
    reason = reason or f"追加任务角色：{', '.join(requested)}"

    pending = None
    for row in conn.execute(
        "SELECT id, tree_json FROM board_drafts WHERE source_task=? "
        "AND kind='role_suggestion' AND status='pending' ORDER BY created_at DESC",
        (task_id,)
    ).fetchall():
        tree = jloads(row["tree_json"], {})
        if str(tree.get("apply_mode") or "") == "authorize":
            pending = (row["id"], tree)
            break

    if pending:
        draft_id, tree = pending
        merged = list(dict.fromkeys([*_authorized_slugs(tree), *requested]))
        prior = {str(x.get("slug") or "").strip(): x
                 for x in (tree.get("suggested_roles") or []) if isinstance(x, dict)}
        merged_entries = []
        for slug in merged:
            role = roles_service.get(conn, slug) or {}
            entry = {**(prior.get(slug) or {}),
                     **next((e for e in entries if e["slug"] == slug), {})}
            entry["slug"] = slug
            entry["display_name"] = (role.get("display_name")
                                     or entry.get("display_name") or slug)
            entry["reason"] = (entry.get("reason") or reason
                               or "planner 请求追加任务角色")
            merged_entries.append(entry)
        tree = {**tree, "apply_mode": "authorize", "rationale": reason,
                "requested_assignees": merged, "suggested_roles": merged_entries}
        conn.execute("UPDATE board_drafts SET tree_json=? WHERE id=?",
                     (jdumps(tree), draft_id))
        revised = True
    else:
        tree = {"apply_mode": "authorize", "rationale": reason,
                "requested_assignees": requested, "suggested_roles": entries}
        draft = create_draft(conn, goal=reason, tree=tree,
                             created_by=f"agent:{agent}", source_task=task_id,
                             kind="role_suggestion")
        draft_id = draft["id"]
        revised = False

    sender = roles_service.notification_sender(conn, task_id, f"agent:{agent}")
    tasks_service.mention(
        conn, task_id, sender, "human",
        f"角色追加提案{'已更新' if revised else '已提交'}：{', '.join(requested)}。"
        "请审批或反馈；批准后再执行 task assign。")
    return get_draft(conn, draft_id)  # type: ignore[return-value]


def _approve_assignee_authorization(conn: sqlite3.Connection, draft: dict,
                                    task: dict, tree: dict) -> tuple[dict | None, str | None]:
    """批准「角色追加授权」：不创建/不绑定，只授权 planner 后续自行 assign。"""
    from . import roles as roles_service
    from . import tasks as tasks_service

    requested = _authorized_slugs(tree)
    if not requested:
        raise ConflictError("role addition proposal has no requested roles")
    entries = {str(item.get("slug") or "").strip(): item
               for item in (tree.get("suggested_roles") or [])
               if isinstance(item, dict)}
    for slug in requested:
        role = roles_service.get(conn, slug)
        if role is None:
            # 允许“先提案、批准后再创建”：提案必须带足够的角色画像供 human 审阅。
            entry = entries.get(slug) or {}
            if not (entry.get("display_name") or entry.get("description")
                    or entry.get("responsibilities")):
                raise ConflictError(
                    f"role {slug} does not exist; proposal needs display_name/description")
            continue
        if not role.get("enabled", True):
            raise ConflictError(f"role {slug} is disabled")

    now = now_iso()
    summary = "、".join(requested)
    conn.execute("UPDATE board_drafts SET status='approved', decided_at=? WHERE id=?",
                 (now, draft["id"]))
    task_id = task["id"]
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (task_id, "human", "comment",
         jdumps({"body": f"已批准角色追加：{summary}。由 planner 创建/绑定。"}), now),
    )
    creator = str(draft.get("created_by") or "").strip()
    if creator.startswith("agent:"):
        creator = creator[6:]
    target = creator or str(task.get("planner") or "")
    if target:
        tasks_service.mention(
            conn, task_id, "human", target,
            f"角色追加提案已批准：{summary}。请执行 role expand（如需）并 "
            f"`anvil task assign {task_id} --assignees {','.join(requested)}`。")
    return None, f"authorized only; planner must create/bind: {summary}"


def _approve_role_suggestion(conn: sqlite3.Connection, draft: dict,
                             by: str) -> tuple[dict | None, str | None]:
    from .. import config
    from . import roles as roles_service
    from . import tasks as tasks_service
    from . import utility as utility_service

    task_id = draft.get("source_task")
    task = tasks_service.get(conn, task_id) if task_id else None
    if task is None:
        raise ConflictError("role_suggestion source_task not found")

    tree = draft.get("tree") or {}
    if str(tree.get("apply_mode") or "") == "authorize":
        return _approve_assignee_authorization(conn, draft, task, tree)
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
        slug = roles_service.validate_slug(str(item.get("slug") or ""))
        display = str(item.get("display_name") or "").strip()
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
         jdumps({"body": f"已批准角色建议，创建/绑定：{summary}。"}), now),
    )
    conn.execute("UPDATE board_drafts SET status='approved', decided_at=? WHERE id=?",
                 (now, draft["id"]))
    # 批准本身只落角色，不替自然人 @ 任何角色、不启动重规划 run。
    # 自然人后续在任务会话里自己 @ 规划者/执行角色；mention 端点负责唤醒对应角色。
    warning = None if to_create else f"all suggested roles already exist; bound only: {summary}"
    return None, warning


def approve(conn: sqlite3.Connection, draft_ids: list[str], by: str = "human",
            *, restart_running: bool = False, restart_reason: str | None = None,
            restart_note: str | None = None) -> dict:
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
            running = conn.execute(
                "SELECT id FROM workflows WHERE task_id=? AND status='running' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (draft["source_task"],),
            ).fetchone()
            if running and not restart_running:
                raise ConflictError(
                    "task already has a running workflow; workflow replacement is not allowed. "
                    "Confirm restart_running to terminate the current execution and restart.")
            from . import knowledge_binding
            from . import workflow as workflow_service
            try:
                knowledge_binding.apply_cindy_draft(conn, draft["source_task"], draft["tree"])
            except Exception as exc:  # noqa: BLE001 —— 绑定失败不阻断审批
                warnings.append(f"{did}: knowledge repos not applied ({exc})")
            workflow_service.create_workflow(
                conn, draft["source_task"], draft["tree"], created_by=draft["created_by"],
                restart_running=restart_running, restart_reason=restart_reason,
                restart_note=restart_note)
            n = len((draft.get("tree") or {}).get("steps") or []) or 1
            action = "终止旧执行并重新开始" if running else "开始执行"
            conn.execute(
                "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (draft["source_task"], "human", "comment",
                 jdumps({"body": f"已批准工作流（{n} 个步骤），{action}。"}),
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
        root = draft.get("tree") or {}
        _land_tree(conn, root, parent_id=draft.get("source_task"),
                   workspace_id=draft.get("workspace_id"), defaults=root)
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
    try:
        from . import repo_catalog
        repo_catalog.invalidate_catalog()
    except Exception:  # noqa: BLE001
        pass
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
                sender = roles_service.notification_sender(
                    conn, task_id, draft.get("created_by"))
                conn.execute(
                    "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, sender, "mention", jdumps({
                        "target": "human",
                        "message": "角色建议已被拒绝，但任务当前没有可用角色；"
                                   "请先创建/绑定至少一个角色后重新派发。",
                        "event": "role_suggestion_rejected",
                    }), now),
                )
        if feedback:
            conn.execute(
                "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, "human", "comment",
                 jdumps({"body": f"角色建议被驳回：{feedback}"}), now),
            )
        tree = draft.get("tree") or {}
        if str(tree.get("apply_mode") or "") == "authorize":
            creator = str(draft.get("created_by") or "").strip()
            if creator.startswith("agent:"):
                creator = creator[6:]
            target = creator or str((task or {}).get("planner") or "")
            if target:
                tasks_service.mention(
                    conn, task_id, "human", target,
                    f"角色追加提案被驳回：{feedback or '请调整后重新提交'}。"
                    "修改后重新提交提案（role propose 或 task assign）即可。")
    conn.commit()
    return {"draft": get_draft(conn, draft_id), "replan": replan, "blocked": blocked}


def _land_tree(conn: sqlite3.Connection, node: dict, parent_id: str | None,
               workspace_id: str | None, feedback_rules: list[str] | None = None,
               defaults: dict | None = None) -> None:
    from . import knowledge as knowledge_service
    from . import knowledge_binding

    now = now_iso()
    defaults = defaults if defaults is not None else node
    if feedback_rules is None:
        feedback_rules = node.get("feedback_rules") or []
    parent = conn.execute(
        "SELECT tags_json, repo, base_branch FROM tasks WHERE id=?",
        (parent_id,)).fetchone() if parent_id else None
    parent_tags = jloads(parent["tags_json"], []) if parent else []
    parent_repo = parent["repo"] if parent else None
    parent_base = parent["base_branch"] if parent else None
    children = node.get("children") or []
    # 有 children 的节点是容器（epic），只落地 children（流水线步骤）；叶子节点落地自身。
    for child in (children if children else [node]):
        tid = child.get("id") or new_id()
        desc = child.get("description") or ""
        if feedback_rules:
            desc = (desc + "\n\n" if desc else "") + "反馈路径：\n" + "\n".join(f"- {r}" for r in feedback_rules)
        raw_tags = child["tags"] if "tags" in child else defaults.get("tags")
        if raw_tags is None:
            raw_tags = parent_tags
        tags = knowledge_service.normalize_tags(raw_tags or [])
        repo = child.get("repo") or defaults.get("repo") or parent_repo
        base_branch = child.get("base_branch") or defaults.get("base_branch") or parent_base
        conn.execute(
            "INSERT INTO tasks (id, title, type, status, parent_id, workspace_id, description, "
            "scope_paths_json, acceptance_json, tags_json, repo, base_branch, created_by, "
            "created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, child.get("title", ""), child.get("type"), "todo", parent_id, workspace_id,
             desc or None, jdumps(child.get("scope_paths") or []), jdumps(child.get("acceptance") or []),
             jdumps(tags), repo, base_branch, "orchestrator", now, now),
        )
        for dep in child.get("depends_on", []):
            conn.execute("INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?,?)", (tid, dep))
        if child.get("role"):
            conn.execute(
                "INSERT OR IGNORE INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)",
                (tid, child["role"], now),
            )
        try:
            if "knowledge_repos" in child or "knowledge_repos" in defaults:
                selection = child["knowledge_repos"] if "knowledge_repos" in child else defaults.get("knowledge_repos")
                knowledge_binding.apply_cindy_draft(conn, tid, {
                    "tags": tags,
                    "knowledge_repos": selection or [],
                }, commit=False)
            else:
                knowledge_binding.auto_bind(conn, {
                    "id": tid, "title": child.get("title", ""), "description": desc,
                    "tags": tags, "repo": repo, "workspace_id": workspace_id,
                    "parent_id": parent_id,
                }, commit=False)
        except Exception:  # noqa: BLE001 —— 绑定失败不能阻断落板
            pass
        child_defaults = dict(defaults)
        for key in ("tags", "knowledge_repos", "repo", "base_branch"):
            if key in child:
                child_defaults[key] = child[key]
        for grandchild in child.get("children", []):
            _land_tree(conn, grandchild, parent_id=tid, workspace_id=workspace_id,
                       feedback_rules=feedback_rules, defaults=child_defaults)
