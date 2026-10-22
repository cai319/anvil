"""Role endpoints (§10 roles 段 + SE §3)."""
from __future__ import annotations

import json
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import RoleIn, RoleOut, ToolsIn
from ..services import utility
from ..util import ConflictError

router = APIRouter()
logger = logging.getLogger("anvil.roles")


def _extract_json(text: str) -> dict | None:
    """从运行时输出中取第一个完整 JSON 对象（兼容前后夹杂说明文字）。"""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


@router.post("/roles/auto", status_code=201, response_model=RoleOut)
def auto_create_role(body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    """从自然人一句描述自动生成角色（能力/定位/职责/系统提示）并创建。

    owner 2026-10-19：定义角色时可以「一句话生成」其能力与定位作为基础背景。
    """
    import uuid

    brief = str(body.get("brief") or "").strip()
    if not brief:
        raise HTTPException(status_code=422, detail="brief is required")
    slug = str(body.get("slug") or "").strip()
    if not slug:
        ascii_part = re.sub(r"[^a-z0-9]+", "-", brief.lower()).strip("-")[:20]
        slug = (ascii_part or "role") + "-" + uuid.uuid4().hex[:5]
    if services.roles.get(conn, slug) is not None:
        raise HTTPException(status_code=409, detail=f"role {slug} already exists")
    prompt = (
        "根据以下对 AI 角色的简短描述，生成该角色的完整画像，只返回 JSON（不要 markdown 代码块）：\n"
        f"角色描述：{brief}\n\n"
        "JSON 字段：\n"
        '  "display_name": 简短中文名（如「后端工程师」）\n'
        '  "description": 一句人类可读的角色定位（不进提示词）\n'
        '  "working_mode": 工作模式（自主性/协作方式/关注点）\n'
        '  "skill_description": 职业技能描述\n'
        '  "capabilities": 领域能力权重对象（如 {"python":0.9,"sql":0.85}）\n'
        '  "responsibilities": 职责列表（3-5 条）\n'
        '  "system_prompt": 系统提示（角色身份与行为准则；须写出：在任务会话中主动用 '
        'anvil task comment 同步进展、用 anvil task mention @协作角色；需要人类决策时 @human）\n'
    )
    runtime_id = str(body.get("runtime_id") or "").strip() or None
    try:
        resolved_runtime_id = utility.resolve_runtime_id(conn, runtime_id)
        text = utility.generate(conn, prompt, runtime_id=runtime_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"生成画像失败: {exc}")
    data = _extract_json(text) or {}
    definition = {
        "kind": "member",
        "description": data.get("description") or brief,
        "runtime": {"id": resolved_runtime_id},
        "workspace": {"repos": []},
        "tools": {},
        "concurrency": {"max_parallel": 2},
        "capabilities": data.get("capabilities") or {},
        "working_mode": data.get("working_mode"),
        "skill_description": data.get("skill_description"),
        "system_prompt": data.get("system_prompt") or "",
        "responsibilities": data.get("responsibilities") or [],
    }
    display_name = str(data.get("display_name") or body.get("display_name") or slug)
    try:
        return services.roles.create(conn, slug=slug, display_name=display_name,
                                     definition=definition)
    except ConflictError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/roles/expand", status_code=201, response_model=RoleOut)
def expand_role(body: dict, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """Agent 自行扩展团队（Raft 风格）：创建新 agent 并继承创建者的 runtime。

    约束：仅 Agent 会话可调用；每日至多 N 个（默认 3）；总角色数上限；
    不继承/不允许设置 tools、permission_mode 以外的高权限字段。
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    from .. import config
    from ..util import now_iso

    if not auth.role or auth.role == "human":
        raise HTTPException(status_code=403, detail="only an agent session can expand the team")
    creator = services.roles.get(conn, auth.role)
    if creator is None:
        raise HTTPException(status_code=404, detail="creator role not found")
    brief = str(body.get("brief") or "").strip()
    if not brief:
        raise HTTPException(status_code=422, detail="brief is required")

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    used = conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE role_slug=? AND action='role_expand' AND created_at>=?",
        (auth.role, since)).fetchone()["n"]
    if used >= config.AGENT_ROLE_EXPAND_DAILY:
        raise HTTPException(status_code=429, detail="daily role expansion quota exhausted")
    total = conn.execute("SELECT COUNT(*) AS n FROM roles").fetchone()["n"]
    if total >= config.MAX_ROLES:
        raise HTTPException(status_code=409, detail=f"role limit {config.MAX_ROLES} reached")

    creator_def = creator.get("definition") or {}
    creator_runtime = creator_def.get("runtime")
    if not isinstance(creator_runtime, dict):
        creator_runtime = {"id": creator_runtime or "claude-code"}
    runtime = {"id": creator_runtime.get("id") or "claude-code"}
    for k in ("permission_mode", "model"):
        if creator_runtime.get(k):
            runtime[k] = creator_runtime[k]

    prompt = (
        "你是团队里的 agent，正在扩展一个新 agent。根据下面的 brief 生成完整画像，"
        "只返回 JSON（不要 markdown 代码块）：\n"
        f"brief：{brief}\n\n"
        '字段：display_name、description、capabilities（对象）、responsibilities（3-5 条）、'
        "system_prompt（身份与行为准则；要求主动用 anvil task comment 同步进展、"
        "用 anvil task mention 协作、需要人类决策时 @human）。"
    )
    data: dict = {}
    try:
        text = utility.generate(conn, prompt, runtime_id=runtime["id"])
        data = _extract_json(text) or {}
    except Exception:  # noqa: BLE001 —— 运行时不可用时退化为简单画像
        data = {}

    slug = str(body.get("slug") or "").strip()
    if not slug:
        slug = "agent-" + uuid.uuid4().hex[:6]
    if services.roles.get(conn, slug) is not None:
        raise HTTPException(status_code=409, detail=f"role {slug} already exists")
    display_name = str(data.get("display_name") or brief[:20] or slug)
    definition = {
        "kind": "member",
        "created_by": f"agent:{auth.role}",
        "description": data.get("description") or brief,
        "runtime": runtime,
        "workspace": {"repos": []},
        "tools": {},
        "concurrency": {"max_parallel": 1},
        "capabilities": data.get("capabilities") or {},
        "responsibilities": data.get("responsibilities") or ["按分配的任务 brief 执行"],
        "system_prompt": data.get("system_prompt") or (
            f"你是 {display_name}，由 {auth.role} 创建。按任务 brief 执行，"
            "主动用 anvil task comment 同步进展、用 anvil task mention 协作，"
            "需要人类决策时 @human。"),
    }
    try:
        role = services.roles.create(conn, slug=slug, display_name=display_name,
                                     definition=definition)
    except ConflictError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (auth.role, None, "role_expand",
         __import__("json").dumps({"slug": slug, "brief": brief}, ensure_ascii=False),
         now_iso()))
    task_id = body.get("task_id")
    if task_id and services.tasks.get(conn, task_id) is not None:
        services.tasks.comment(conn, task_id, auth.role,
                               f"创建了新 agent：{display_name}（{slug}）\n{brief}")
        services.tasks.mention(conn, task_id, auth.role, "human",
                               f"我创建了新 agent {display_name}（{slug}）：{brief}")
    conn.commit()
    return role


@router.post("/roles", status_code=201, response_model=RoleOut)
def create_role(body: RoleIn, conn=Depends(get_db)):
    definition = body.model_dump(exclude_none=True)
    definition.pop("slug", None)
    definition.pop("display_name", None)
    if definition.get("kind") == "onboarding":
        raise HTTPException(status_code=422, detail="cannot create onboarding role via API")
    definition.setdefault("kind", "member")
    try:
        return services.roles.create(conn, slug=body.slug, display_name=body.display_name,
                                     definition=definition)
    except ConflictError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/roles", response_model=list[RoleOut])
def list_roles(conn=Depends(get_db)):
    return services.roles.list_all(conn)


@router.get("/roles/{slug}", response_model=RoleOut)
def get_role(slug: str, conn=Depends(get_db)):
    role = services.roles.get(conn, slug)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    return role


@router.put("/roles/{slug}", response_model=RoleOut)
def update_role(slug: str, body: RoleIn, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    role = services.roles.get(conn, slug)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    definition = body.model_dump(exclude_none=True)
    definition.pop("slug", None)
    definition.pop("display_name", None)
    if definition.get("kind") == "onboarding":
        raise HTTPException(status_code=422, detail="cannot convert role to onboarding via API")
    services.roles.update(conn, slug, display_name=body.display_name, definition=definition)
    return services.roles.get(conn, slug)  # type: ignore[return-value]


@router.put("/roles/{slug}/runtime", response_model=RoleOut)
def update_role_runtime(slug: str, body: dict, conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    """把角色绑定到指定运行时；保留其它角色配置（system_prompt 等）。"""
    role = services.roles.get(conn, slug)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    rid = str(body.get("id") or "").strip()
    if not rid:
        raise HTTPException(status_code=422, detail="id is required")
    if services.runtime.get(conn, rid) is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    definition = role.get("definition") or {}
    definition["runtime"] = {"id": rid}
    services.roles.update(conn, slug, display_name=role["display_name"], definition=definition)
    return services.roles.get(conn, slug)  # type: ignore[return-value]


@router.post("/roles/{slug}/session-reset")
def reset_role_session(slug: str, conn=Depends(get_db),
                       auth: Auth = Depends(require_human)):
    """重置角色常驻会话上下文；保留 MEMORY.md / notes。"""
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    from ..services import daemon as daemon_service

    mgr = daemon_service.get_manager()
    if mgr and mgr.enabled:
        mgr.reset_session(slug)
    else:
        daemon_service.clear_session(conn, slug, "session reset")
    return {"slug": slug, "reset": "session"}


@router.delete("/roles/{slug}", status_code=204)
def delete_role(slug: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        deleted = services.roles.delete(conn, slug)
    except Exception as exc:  # ConflictError
        raise HTTPException(status_code=409, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="role not found")
    return None


@router.get("/roles/{slug}/home")
def role_home(slug: str, conn=Depends(get_db)):
    tree = services.roles.home_tree(conn, slug)
    if tree is None:
        raise HTTPException(status_code=404, detail="role not found")
    return tree


@router.get("/roles/{slug}/home/file")
def role_home_file(slug: str, path: str, conn=Depends(get_db)):
    """只读展示角色 home 内文件（design §10；默认只读，编辑请用「给角色发指令」）。"""
    file = services.roles.read_home_file(conn, slug, path)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found or path out of home")
    return file


@router.get("/roles/{slug}/home/history")
def role_home_history(slug: str, path: str | None = None, conn=Depends(get_db)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    return {"slug": slug, "path": path, "commits": services.roles.home_history(conn, slug, path)}


@router.get("/roles/{slug}/versions")
def role_versions(slug: str, conn=Depends(get_db)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    return services.roles.list_versions(conn, slug)


@router.post("/roles/{slug}/rollback/{version}")
def rollback_role(slug: str, version: int, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    role = services.roles.rollback(conn, slug, version)
    if role is None:
        raise HTTPException(status_code=404, detail="role or version not found")
    return role


@router.post("/roles/{slug}/message")
def message_role(slug: str, body: dict, request: Request, conn=Depends(get_db)):
    """向角色直接发指令（design §10 唤醒源之一）：创建无任务 run 并立即派发。"""
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")
    run = services.runs.create_run(conn, slug, None, prompt=message)
    orch = getattr(request.app.state, "orchestrator", None)
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    return run


@router.get("/roles/{slug}/metrics")
def role_metrics(slug: str, conn=Depends(get_db)):
    """角色执行指标（design §10 / §6.5）：run 分布 + 成本 + 任务吞吐。"""
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    runs = conn.execute(
        "SELECT status, COUNT(*) AS n FROM runs WHERE role_slug=? GROUP BY status", (slug,)
    ).fetchall()
    runs_by_status = {r["status"]: r["n"] for r in runs}
    cost = conn.execute(
        "SELECT COALESCE(SUM(usd),0) AS s FROM cost_ledger WHERE role_slug=?", (slug,)
    ).fetchone()["s"]
    done = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE assignee=? AND status IN ('done','closed')", (slug,)
    ).fetchone()["n"]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE assignee=?", (slug,)
    ).fetchone()["n"]
    return {
        "role": slug,
        "runs": sum(runs_by_status.values()),
        "runs_by_status": runs_by_status,
        "tasks_total": total,
        "tasks_done": done,
        "cost_used_usd": cost,
    }


@router.get("/roles/{slug}/tools")
def role_tools(slug: str, conn=Depends(get_db)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    return services.roles.get_tools(conn, slug)


@router.put("/roles/{slug}/tools")
def update_role_tools(slug: str, body: ToolsIn, conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    tools, estimate = services.roles.update_tools(
        conn, slug, body.preset, body.allowlist, body.max_tool_tokens,
    )
    if estimate is not None:
        raise HTTPException(status_code=422, detail=estimate)
    return tools


@router.get("/roles/{slug}/effective-config")
def effective_config(slug: str, conn=Depends(get_db)):
    cfg = services.roles.effective_config(conn, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="role not found")
    return cfg


@router.get("/roles/{slug}/catalog")
def role_catalog(slug: str, conn=Depends(get_db)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    tools = services.roles.get_tools(conn, slug)
    return {"skills": tools.get("skills", []), "mcp": tools.get("mcp", [])}


@router.put("/roles/{slug}/catalog")
def update_role_catalog(slug: str, body: dict, conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    skills = [str(s) for s in (body.get("skills") or [])]
    mcp = [str(m) for m in (body.get("mcp") or [])]
    return services.roles.update_catalog(conn, slug, skills, mcp)


@router.get("/roles/{slug}/profile")
def role_profile(slug: str, conn=Depends(get_db)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    return services.roles.get_profile(conn, slug)


@router.put("/roles/{slug}/profile")
def update_role_profile(slug: str, body: dict, conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    if services.roles.get(conn, slug) is None:
        raise HTTPException(status_code=404, detail="role not found")
    system_prompt = str(body.get("system_prompt") or "")
    responsibilities = [str(r) for r in (body.get("responsibilities") or [])]
    return services.roles.update_profile(conn, slug, system_prompt, responsibilities)


@router.post("/roles/{slug}/generate")
def generate_role(slug: str, body: dict, conn=Depends(get_db)):
    """用角色自己的运行时（未配置时用默认运行时）生成工作模式/技能描述/职责/系统提示。"""
    role = services.roles.get(conn, slug)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    brief = str(body.get("brief") or "").strip()
    if not brief:
        raise HTTPException(status_code=422, detail="brief is required")
    prompt = (
        "根据以下对 AI 角色的简短描述，生成该角色的完整画像，只返回 JSON（不要 markdown 代码块）：\n"
        f"角色描述：{brief}\n\n"
        'JSON 字段：\n'
        '  "working_mode": 工作模式（一句话：自主性/协作方式/关注点）\n'
        '  "skill_description": 职业技能描述（一句话，擅长什么）\n'
        '  "responsibilities": 职责列表（3-5 条字符串）\n'
        '  "system_prompt": 系统提示（一段话：角色身份与行为准则；须明确写出该角色在团队协作时'
        '会主动在任务会话用 anvil task comment 同步思考进展、用 anvil task mention @提及'
        '需要协作的其它角色；若该角色负责规划/设计，须先产出方案/设计并 @human 请确认、'
        '收到 human 确认后再实施）\n'
    )
    definition = role.get("definition") or {}
    runtime = definition.get("runtime")
    runtime_id = runtime.get("id") if isinstance(runtime, dict) else (str(runtime) if runtime else None)
    started = time.monotonic()
    try:
        text = utility.generate(conn, prompt, runtime_id=runtime_id)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        raise HTTPException(status_code=502, detail=f"生成画像失败（{elapsed:.1f}s）: {exc}")

    data = _extract_json(text)
    if data is None:
        elapsed = time.monotonic() - started
        snippet = (text or "")[:500]
        logger.warning(
            "role profile generation: no JSON slug=%s runtime=%s elapsed=%.1fs output=%r",
            slug, runtime_id or "default", elapsed, (text or "")[:2000],
        )
        raise HTTPException(
            status_code=502,
            detail=f"运行时未返回有效的 JSON 画像（{elapsed:.1f}s）。原始输出片段: {snippet}",
        )

    missing: list[str] = []
    if not isinstance(data.get("working_mode"), str) or not data["working_mode"].strip():
        missing.append("working_mode")
    if not isinstance(data.get("skill_description"), str) or not data["skill_description"].strip():
        missing.append("skill_description")
    if not isinstance(data.get("system_prompt"), str) or not data["system_prompt"].strip():
        missing.append("system_prompt")
    responsibilities = data.get("responsibilities")
    if not isinstance(responsibilities, list) or not any(
        isinstance(x, str) and x.strip() for x in responsibilities
    ):
        missing.append("responsibilities")

    elapsed = time.monotonic() - started
    if missing:
        snippet = (text or "")[:500]
        logger.warning(
            "role profile generation incomplete slug=%s runtime=%s elapsed=%.1fs missing=%s output=%r",
            slug, runtime_id or "default", elapsed, missing, (text or "")[:2000],
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"运行时返回的画像不完整（{elapsed:.1f}s；缺少/为空：{', '.join(missing)}）。"
                f"原始输出片段: {snippet}"
            ),
        )

    logger.info(
        "role profile generated slug=%s runtime=%s elapsed=%.1fs",
        slug, runtime_id or "default", elapsed,
    )
    return data


@router.post("/roles/{slug}/clone", status_code=201, response_model=RoleOut)
def clone_role(slug: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    src = services.roles.get(conn, slug)
    if src is None:
        raise HTTPException(status_code=404, detail="role not found")
    from ..util import jloads
    definition = jloads(src["definition_json"], {})
    new_slug = f"{slug}-clone"
    return services.roles.create(conn, slug=new_slug, display_name=f"{src['display_name']} (clone)", definition=definition)


@router.post("/tools/estimate")
def estimate_tools(body: dict, conn=Depends(get_db)):
    allowlist = body.get("allowlist", [])
    return {"estimate": services.roles.estimate_tokens(allowlist)}
