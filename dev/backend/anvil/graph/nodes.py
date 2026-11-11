"""Run-lifecycle 图节点（project / spawn / submit / await_review / review / process_exit）。

每个节点通过 `config["configurable"]["db_path"]` 开自己的连接、用 `thread_id`（= run_id）
读写 `runs` 行。节点**只 `UPDATE runs`**，绝不重复调用 `services.tasks.*`——任务 5 态由
端点负责变更（保住既有测试契约）。

interrupt 恢复会从被 interrupt 的节点顶部重跑，因此 `interrupt()` 必须放在幂等节点里
（`await_review_node`），非幂等副作用放在它前面的 `submit_node`。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langgraph.types import interrupt

from .. import config
from .. import config as app_config
from ..db import open_conn
from ..platform import factory
from ..services import (
    context_manifest as context_manifest_service,
    knowledge as knowledge_service,
    memory as memory_service,
    roles as roles_service,
    runs as runs_service,
    runtime as runtime_service,
    tasks as tasks_service,
    tooling as tooling_service,
)
from ..util import jloads, now_iso

logger = logging.getLogger("anvil.nodes")
from . import render


def _ctx(config) -> tuple[str, sqlite3.Connection]:
    """从 config 取 run_id（thread_id）与 db_path，开独立连接。"""
    cfg = config["configurable"]
    return cfg["thread_id"], open_conn(cfg["db_path"])


def _run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def _task(conn: sqlite3.Connection, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["scope_paths"] = jloads(d.pop("scope_paths_json"), [])
    d["acceptance"] = jloads(d.pop("acceptance_json"), [])
    d["tags"] = jloads(d.pop("tags_json", "[]"), [])
    return d


def _effective(conn: sqlite3.Connection, run: dict) -> dict:
    digest = run.get("profile_snapshot")
    if digest:
        row = conn.execute("SELECT payload_json FROM config_snapshots WHERE digest=?", (digest,)).fetchone()
        if row:
            return jloads(row["payload_json"], {})
    return roles_service.effective_config(conn, run["role_slug"]) or {}


def _runtime_id(effective: dict) -> str | None:
    rt = (effective.get("projection") or {}).get("runtime")
    if isinstance(rt, dict):
        return rt.get("id")
    return rt or None


def _supports_subagents(conn: sqlite3.Connection, run: dict,
                        effective: dict) -> bool:
    """运行时是否声明支持原生 subagent；能力来自 runtime YAML capabilities。"""
    try:
        runtime_id = _runtime_id(effective) or config.DEFAULT_RUNTIME
        row = runtime_service.get(conn, runtime_id) or {}
        caps = row.get("capabilities") or {}
        return bool(caps.get("supports_subagents"))
    except Exception:  # noqa: BLE001 —— 能力查询失败按不支持处理
        return False


def _persistent_session(conn: sqlite3.Connection, effective: dict) -> dict | None:
    """该角色运行时是否走常驻会话模式（claude-code stream-json 等）。"""
    from ..services import daemon as daemon_service

    mgr = daemon_service.get_manager()
    if not (mgr and mgr.enabled):
        return None
    runtime_id = _runtime_id(effective) or config.DEFAULT_RUNTIME
    definition = runtime_service.load_definitions().get(runtime_id) or {}
    sess = definition.get("session") or {}
    if sess.get("mode") != "persistent":
        return None
    return {"definition": definition, "session": sess, "runtime_id": runtime_id}


def _workspace_env(role_slug: str, workspace_path: str) -> dict:
    """运行时目录边界：任务 workspace（可写）/ 角色 home（私有）/ 知识库（只读）。"""
    return {
        "ANVIL_ROLE_HOME": str(config.AGENTS_DIR / role_slug),
        "ANVIL_TASK_WORKSPACE": str(workspace_path),
        "ANVIL_KNOWLEDGE_ROOT": str(config.KNOWLEDGE_REPOS_DIR),
        "ANVIL_CHANNEL_ROOT": str(config.CHANNELS_DIR),
    }


def _render_persistent(conn: sqlite3.Connection, run: dict, task: dict | None,
                       effective: dict, role: dict, workspace_path: str):
    """常驻模式的启动模板：args_new / args_resume 由 daemon 在 spawn 时按 session_id 渲染。"""
    runtime_id = _runtime_id(effective) or config.DEFAULT_RUNTIME
    definition = runtime_service.load_definitions().get(runtime_id) or {}
    rt_row = runtime_service.get(conn, runtime_id) or {}
    executable = (
        rt_row.get("executable_path")
        or definition.get("executable") or definition.get("executable_path")
        or runtime_id
    )
    executable = runtime_service.effective_executable(executable) or executable
    rt_cfg = (effective.get("projection") or {}).get("runtime") or {}
    if not isinstance(rt_cfg, dict):
        rt_cfg = {}
    permission_mode = rt_cfg.get("permission_mode") or config.DEFAULT_PERMISSION_MODE
    model_pool = rt_cfg.get("model_pool") or []
    context_window_floor = rt_cfg.get("context_window_floor")
    sess = definition.get("session") or {}
    inv = definition.get("invocation") or {}
    timeout = float(inv.get("timeout_seconds") or 3600)
    ctx = {
        "permission_mode": permission_mode,
        "workspace_path": workspace_path,
        "task_id": task["id"] if task else "",
        "agent_slug": run["role_slug"],
        "session_token": "",  # daemon 注入角色级令牌
    }
    cwd = render.render_template(str(inv.get("cwd") or workspace_path), ctx)
    env = {k: render.render_template(str(v), ctx) for k, v in (inv.get("env") or {}).items()}
    env.update(_workspace_env(run["role_slug"], workspace_path))
    return (
        executable,
        list(sess.get("args_new") or []),
        list(sess.get("args_resume") or []),
        cwd, env, timeout, ctx, model_pool, context_window_floor,
        runtime_id, workspace_path,
    )


def _task_workspace_path(conn: sqlite3.Connection, run: dict) -> str:
    """任务数据存储目录（不是知识库）；无 task_id 时返回空串。"""
    task_id = run.get("task_id")
    if not task_id:
        return ""
    from ..services import task_workspace as task_workspace_service
    # 派遣子任务与来源 task 共享 workspace/cwd，保证回切时 --resume 不因 cwd 变化轮换。
    return task_workspace_service.resolve_for_task(conn, task_id, create=True)


def _workspace_path(run: dict, role: dict, conn: sqlite3.Connection | None = None) -> str:
    """执行目录优先级：run worktree → task workspace → 角色 home。"""
    if conn is not None and run.get("task_id"):
        task = tasks_service.get(conn, run["task_id"])
        if task and task.get("type") in ("dispatch", "dispatched") and task.get("parent_id"):
            shared = _task_workspace_path(conn, run)
            if shared:
                return shared
    wt = run.get("worktree_path")
    if wt and Path(wt).exists():
        return wt
    if conn is not None:
        tw = _task_workspace_path(conn, run)
        if tw:
            return tw
    return role.get("home_path") or str(config.AGENTS_DIR / run["role_slug"])


def _run_workspace_path(conn: sqlite3.Connection, run: dict, role: dict,
                        effective: dict) -> str:
    """常驻运行：同一 (role, task) 复用第一次确定的 workspace/cwd。

    Claude Code 的会话按 cwd 分目录；如果每个 run 都建新 worktree，--resume 会
    找不到旧会话。优先级：
    1) 该任务槽已记录的 workspace（且不是旧的共享角色 home）；
    2) 本次 run 的 worktree（代码任务）；
    3) 任务 workspace（每任务独立数据目录）；
    4) 角色 home（回退/无任务运行）。
    旧槽指向角色 home 时会被忽略，首次唤醒触发一次 CIC 轮换。
    """
    role_home = str(role.get("home_path") or "")
    task_id = run.get("task_id")
    task = tasks_service.get(conn, task_id) if task_id else None
    if task and task.get("type") in ("dispatch", "dispatched") and task.get("parent_id"):
        # 派遣子任务必须与来源 task 使用同一 cwd；不能被旧槽/旧 worktree 覆盖。
        shared = _task_workspace_path(conn, run)
        if shared:
            return shared
    if _persistent_session(conn, effective) and run.get("task_id"):
        from ..services import daemon as daemon_service
        # 同一 task 的 workspace 在 slice 间共享；优先精确槽，再回退同 task 任意槽。
        target_key = daemon_service.context_key(run["task_id"], run.get("slice_key"))
        row = conn.execute(
            "SELECT workspace_path FROM role_task_sessions "
            "WHERE role_slug=? AND task_key=?",
            (run["role_slug"], target_key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT workspace_path FROM role_task_sessions "
                "WHERE role_slug=? AND task_id=? "
                "ORDER BY COALESCE(last_active_at, updated_at) DESC LIMIT 1",
                (run["role_slug"], run["task_id"]),
            ).fetchone()
        slot_ws = row["workspace_path"] if row is not None else None
        if slot_ws:
            p = Path(slot_ws)
            # 旧数据把角色 home 当作任务 workspace；迁移到 per-task dir。
            if p.exists() and str(p) != role_home:
                return str(p)
    wt = run.get("worktree_path")
    if wt and Path(wt).exists():
        return wt
    tw = _task_workspace_path(conn, run)
    if tw:
        return tw
    return role_home or str(config.AGENTS_DIR / run["role_slug"])


def _instruction_md(role: dict) -> str:
    home = role.get("home_path")
    if not home:
        return ""
    try:
        return (Path(home) / "instruction.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _clip_text(text: str, max_chars: int) -> str:
    """P0-3 注入预算：超出字符上限时保留尾部，并显式标注截断（避免 context rot）。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    # 尽量从行边界开始，避免半行
    nl = tail.find("\n")
    if 0 <= nl < 200:
        tail = tail[nl + 1:]
    return "…（更早的会话已省略）\n" + tail


def _timeline_in_scope(entry: dict, slice_key: str | None,
                       task_wide: bool = False) -> bool:
    if task_wide:
        return True
    value = entry.get("slice_key")
    if slice_key:
        return value in (None, slice_key)
    return value is None


def _conversation_transcript(conn: sqlite3.Connection, task_id: str | None,
                             max_chars: int = 4000,
                             slice_key: str | None = None,
                             task_wide: bool = False) -> str:
    """把任务会话时间线（评论 + @提及 + 认领/提交/评审）拼成对话记录，作为续跑上下文。"""
    if not task_id:
        return ""
    tl = tasks_service.timeline(conn, task_id)
    lines: list[str] = []
    for e in tl:
        if not _timeline_in_scope(e, slice_key, task_wide):
            continue
        role = e["role"] or "system"
        p = e["payload"] or {}
        if e["kind"] == "comment":
            lines.append(f"[{role}] {p.get('body', '')}")
        elif e["kind"] == "mention":
            lines.append(f"[{role}] @{p.get('target', '')}: {p.get('message', '')}")
        elif e["kind"] == "claim":
            lines.append(f"[{role}] 认领了任务")
        elif e["kind"] == "submit":
            lines.append(f"[{role}] 提交了评审")
        elif e["kind"] == "review":
            lines.append(f"[{role}] 评审 {'通过' if p.get('approve') else '驳回'}")
    return _clip_text("\n".join(lines), max_chars)


def _prompt(task: dict | None, transcript: str = "", extra: str = "",
            protocol: str | None = render.PROTOCOL) -> str:
    # extra（规划协议 / reminder 提示等）始终注入：即使有任务正文也不能被挤掉。
    # planner 自带完整协议，不能再拼通用 PROTOCOL（否则「必须 claim」会覆盖「不要 claim」）。
    head = "\n".join(p for p in (task.get("title"), task.get("description")) if p) if task else ""
    parts = [p for p in (head, extra, transcript, protocol) if p]
    return "\n\n".join(p for p in parts if p)


def _recent_transcript(conn: sqlite3.Connection, task_id: str | None, limit: int = 24,
                       max_chars: int = 1800, slice_key: str | None = None,
                       task_wide: bool = False) -> str:
    """最近 N 条任务会话（评论/@提及/提交/评审），用于步骤/回复上下文注入。"""
    if not task_id:
        return ""
    tl = [e for e in tasks_service.timeline(conn, task_id)
          if _timeline_in_scope(e, slice_key, task_wide)]
    lines: list[str] = []
    for e in tl[-limit:]:
        role = e["role"] or "system"
        p = e["payload"] or {}
        if e["kind"] == "comment":
            lines.append(f"[{role}] {p.get('body', '')}")
        elif e["kind"] == "mention":
            lines.append(f"[{role}] @{p.get('target', '')}: {p.get('message', '')}")
        elif e["kind"] == "submit":
            lines.append(f"[{role}] 提交了评审")
        elif e["kind"] == "review":
            lines.append(f"[{role}] 评审 {'通过' if p.get('approve') else '驳回'}")
    return _clip_text("\n".join(lines), max_chars)


def _knowledge_hint(conn: sqlite3.Connection, run: dict,
                    task: dict | None) -> str:
    """紧凑知识提示：warm session 不走 CIC，也能看到知识库入口。"""
    try:
        task_id = run.get("task_id") or (task or {}).get("id")
        is_step = bool(run.get("step_id"))
        return knowledge_service.step_knowledge_hint(
            conn, run["role_slug"], task_id, task,
            max_chars=1200 if is_step else 320,
            compact=not is_step)
    except Exception:  # noqa: BLE001 —— 知识提示失败不阻断唤醒
        return ""


def _is_finding_reviewer(conn: sqlite3.Connection, task: dict | None,
                         role: str | None) -> bool:
    if not task or not task.get("issue_board_enabled") or not role:
        return False
    if role in (task.get("reviewer_roles") or []):
        return True
    tid = task.get("id")
    if not tid:
        return False
    return conn.execute(
        "SELECT 1 FROM task_reviewers WHERE task_id=? AND role_slug=? LIMIT 1",
        (tid, role),
    ).fetchone() is not None


def _with_wake_note(run: dict, text: str) -> str:
    """自动回切的一次性 continuation 放在 prompt 顶部，不改变原协议识别。"""
    note = str(run.get("wake_note") or "").strip()
    if not note:
        return text
    return ("## 回到原切片上下文\n" + note + "\n\n" + text).strip()


def _prompt_for_run(conn: sqlite3.Connection, run: dict, task: dict | None) -> str:
    """按 run 类型选择协议：
    - 工作流步骤：步骤说明 + 最近会话（含 @ 反馈）+ STEP_PROTOCOL
    - 问题板复评：FINDING_PROTOCOL（可写 finding，不做普通 task submit）
    - 被 @ 的回复：任务背景 + 消息 + 最近会话 + REPLY_PROTOCOL（只讨论、不 claim/submit）
    - 普通任务：任务协议
    """
    prompt = run.get("prompt") or ""
    if prompt.startswith("[anvil:findings]"):
        body = prompt[len("[anvil:findings]"):].strip()
        head = "\n".join(p for p in ((task or {}).get("title"),
                                     (task or {}).get("description")) if p)
        return _with_wake_note(
            run, "\n\n".join(p for p in (head, body, render.FINDING_PROTOCOL) if p))
    if run.get("step_id"):
        from ..services import agent_inbox
        task_id = run.get("task_id")
        parts = [prompt]
        if task_id:
            unread = agent_inbox.unread_count(
                conn, run["role_slug"], task_id, slice_key=run.get("slice_key"))
            if unread:
                items = agent_inbox.items(
                    conn, run["role_slug"], task_id, limit=3,
                    slice_key=run.get("slice_key"))
                lines = "\n".join(f"- [{i['role']}] {i['text'][:300]}" for i in items)
                parts.append(
                    f"## 未读会话（{unread} 条，最近 3 条）\n{lines}\n\n"
                    f"处理完反馈后执行 `anvil inbox commit {task_id} --up-to <最大消息 id>`；"
                    f"完整未读用 `anvil inbox check {task_id}` 按需拉取（拉模式）。")
        hint = _knowledge_hint(conn, run, task)
        if hint:
            parts.append(hint)
        parts.append(render.STEP_PROTOCOL)
        if _is_finding_reviewer(conn, task, run.get("role_slug")):
            parts.append(render.STEP_FINDING_PROTOCOL)
        return _with_wake_note(run, "\n\n".join(p for p in parts if p))
    # 规划 run（Cindy / planner）：不能因为任务已有 workflow 就被误判为回复。
    from ..services import planning as planning_service
    if prompt.startswith(planning_service.PLAN_MARKER):
        body = prompt[len(planning_service.PLAN_MARKER):].strip()
        base = _prompt(task, _conversation_transcript(
            conn, run.get("task_id"), task_wide=True), body, protocol=None)
        hint = _knowledge_hint(conn, run, task)
        return _with_wake_note(run, "\n\n".join(p for p in (base, hint) if p))
    # 工作流任务上的非步骤 run = 被 @ 的会话回复（包括历史遗留无标记的 run），
    # 一律用 REPLY_PROTOCOL，避免误 claim/submit 任务。
    is_reply = prompt.startswith("[anvil:reply]")
    if not is_reply and run.get("task_id"):
        from ..services import workflow as workflow_service
        try:
            is_reply = workflow_service.get_for_task(conn, run["task_id"]) is not None
        except Exception:  # noqa: BLE001
            is_reply = False
    if is_reply:
        body = prompt[len("[anvil:reply]"):].strip() if prompt.startswith("[anvil:reply]") else prompt
        recent = _recent_transcript(
            conn, run.get("task_id"), slice_key=run.get("slice_key"))
        head = "\n".join(p for p in ((task or {}).get("title"), (task or {}).get("description")) if p)
        parts = [head, body or "你在任务会话中被 @，请阅读最近会话并回复。",
                 ("## 最近会话\n" + recent) if recent else "",
                 _knowledge_hint(conn, run, task), render.REPLY_PROTOCOL]
        return _with_wake_note(run, "\n\n".join(p for p in parts if p))
    hint = _knowledge_hint(conn, run, task)
    body = prompt + ("\n\n" + hint if hint else "")
    return _with_wake_note(
        run, _prompt(task, _conversation_transcript(
            conn, run.get("task_id"), slice_key=run.get("slice_key")), body))


def _protocol_for_run(conn: sqlite3.Connection, run: dict) -> str:
    """投影到 CLAUDE.md 的协议必须与 run 类型一致（步骤/问题板/规划/回复/普通任务）。"""
    prompt = run.get("prompt") or ""
    task = tasks_service.get(conn, run["task_id"]) if run.get("task_id") else None
    if prompt.startswith("[anvil:findings]"):
        return render.FINDING_PROTOCOL
    if run.get("step_id"):
        if _is_finding_reviewer(conn, task, run.get("role_slug")):
            return render.STEP_PROTOCOL + "\n\n" + render.STEP_FINDING_PROTOCOL
        return render.STEP_PROTOCOL
    from ..services import planning as planning_service
    if prompt.startswith(planning_service.PLAN_MARKER):
        return render.PLANNING_PROTOCOL + "\n\n" + render.REVISION_PROTOCOL
    is_reply = prompt.startswith("[anvil:reply]")
    if not is_reply and run.get("task_id"):
        from ..services import workflow as workflow_service
        try:
            is_reply = workflow_service.get_for_task(conn, run["task_id"]) is not None
        except Exception:  # noqa: BLE001
            is_reply = False
    return render.REPLY_PROTOCOL if is_reply else render.PROTOCOL


def _render_invocation(conn: sqlite3.Connection, run: dict, task: dict | None,
                       effective: dict, role: dict, workspace_path: str) -> tuple[str, list[str], str, dict]:
    runtime_id = _runtime_id(effective) or config.DEFAULT_RUNTIME
    definitions = runtime_service.load_definitions()
    definition = definitions.get(runtime_id) or {}
    rt_row = runtime_service.get(conn, runtime_id) or {}
    executable = (
        rt_row.get("executable_path")
        or definition.get("executable") or definition.get("executable_path")
        or runtime_id
    )
    executable = runtime_service.effective_executable(executable) or executable
    rt_cfg = (effective.get("projection") or {}).get("runtime") or {}
    permission_mode = (rt_cfg.get("permission_mode") if isinstance(rt_cfg, dict) else None) or config.DEFAULT_PERMISSION_MODE

    # 运行级会话令牌（design §10 末段）：绑定 role + task，Agent 无法自填 X-Anvil-Role 冒充。
    token = secrets.token_urlsafe(24)
    timeout = float((definition.get("invocation") or {}).get("timeout_seconds") or 3600)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=timeout + 300)).isoformat()
    conn.execute("DELETE FROM sessions WHERE run_id=?", (run["id"],))
    conn.execute(
        "INSERT INTO sessions (token, role_slug, run_id, task_id, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        (token, run["role_slug"], run["id"], run.get("task_id"), now_iso(), expires),
    )
    conn.commit()

    ctx = {
        "permission_mode": permission_mode,
        "prompt": _prompt_for_run(conn, run, task),
        "workspace_path": workspace_path,
        "task_id": task["id"] if task else "",
        "agent_slug": run["role_slug"],
        "session_token": token,
    }
    inv = definition.get("invocation") or {}
    timeout = float(inv.get("timeout_seconds") or 3600)  # run 级超时冻结值（P0-2 单一真相）
    args = [render.render_template(str(a), ctx) for a in inv.get("args", [])]
    cwd = render.render_template(str(inv.get("cwd") or workspace_path), ctx)
    env = {k: render.render_template(str(v), ctx) for k, v in (inv.get("env") or {}).items()}
    env.update(_workspace_env(run["role_slug"], workspace_path))
    return executable, args, cwd, env, timeout


def _project_catalog(conn: sqlite3.Connection, effective: dict, workspace_path: str, role_slug: str) -> None:
    """投影角色勾选的 skill/MCP + 工具 allowlist（design §3.5 步骤 3–4）。

    优先用 run 冻结快照中的 tools（配置快照语义：中途改配置不影响已开始的运行），
    快照缺失时才回退实时配置。
    """
    tools = (effective.get("projection") or {}).get("tools") or roles_service.get_tools(conn, role_slug)
    selected_skills = tools.get("skills", [])
    selected_mcp = tools.get("mcp", [])
    runtime_id = _runtime_id(effective) or config.DEFAULT_RUNTIME
    definition = runtime_service.load_definitions().get(runtime_id) or {}
    cat = definition.get("catalog") or {}

    proj = effective.get("projection") or {}
    skills_target_tpl = ((proj.get("skills") or {}).get("target")
                         or "{{workspace_path}}/.claude/skills/")
    skills_target = Path(render.render_template(str(skills_target_tpl), {"workspace_path": workspace_path}))

    # skills：运行时自身目录优先；DB 注册的 skill 作为兜底写入 SKILL.md
    skills_dir = os.path.expanduser(str(cat.get("skills_dir", "")))
    if selected_skills:
        target = skills_target
        target.mkdir(parents=True, exist_ok=True)
        for name in selected_skills:
            dst = target / name
            src = Path(skills_dir) / name if skills_dir else None
            if dst.exists():
                continue
            if src and src.is_dir():
                try:
                    os.symlink(src, dst)
                except OSError:
                    shutil.copytree(src, dst)
                continue
            skill = tooling_service.get_skill(conn, name)
            if skill:
                try:
                    dst.mkdir(parents=True, exist_ok=True)
                    (dst / "SKILL.md").write_text(skill.get("content") or "", encoding="utf-8")
                except OSError:
                    pass

    # mcp：运行时自身配置 + Anvil 注册表合并
    mcp_config = os.path.expanduser(str(cat.get("mcp_config", "")))
    mcp_pointer = str(cat.get("mcp_pointer", "mcpServers"))
    mcp_target_tpl = ((proj.get("mcp") or {}).get("target")
                      or "{{workspace_path}}/.claude/settings.json")
    mcp_target = Path(render.render_template(str(mcp_target_tpl), {"workspace_path": workspace_path}))
    chosen: dict = {}
    if mcp_config and os.path.isfile(mcp_config):
        try:
            with open(mcp_config, encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get(mcp_pointer, {}) or {}
            chosen.update({k: servers[k] for k in selected_mcp if k in servers})
        except Exception:  # noqa: BLE001
            pass
    chosen.update(tooling_service.mcp_config_for(conn, selected_mcp, runtime_id))
    if selected_mcp and chosen:
        try:
            settings = mcp_target
            settings.parent.mkdir(parents=True, exist_ok=True)
            existing = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
            existing["mcpServers"] = chosen
            settings.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001 —— 投影失败不阻断运行
            pass

    # 工具 allowlist → Claude Code permissions.allow（M3 工具裁剪投影）
    allow = _allow_entries(tools.get("allowlist") or [])
    if allow:
        try:
            settings = mcp_target
            settings.parent.mkdir(parents=True, exist_ok=True)
            existing = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
            permissions = existing.get("permissions") or {}
            permissions["allow"] = allow
            existing["permissions"] = permissions
            settings.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _allow_entries(allowlist: list[dict]) -> list[str]:
    """把 `[{server, tools:[...]}]` 转成 Claude Code 的 mcp__server__tool 形式。"""
    entries: list[str] = []
    for item in allowlist:
        if not isinstance(item, dict):
            continue
        server = item.get("server") or item.get("name")
        tools = item.get("tools") or []
        if server and tools:
            entries.extend(f"mcp__{server}__{tool}" for tool in tools)
        elif server:
            entries.append(str(server))
        else:
            entries.extend(str(t) for t in tools)
    return entries


def _project_run(conn: sqlite3.Connection, run: dict, *,
                 mark_status: bool = True) -> None:
    """执行一次投影（CLAUDE.md/AGENTS.md + skills/MCP）；幂等。"""
    run_id = run["id"]
    role = roles_service.get(conn, run["role_slug"]) or {}
    task = _task(conn, run.get("task_id"))
    effective = _effective(conn, run)
    workspace_path = _run_workspace_path(conn, run, role, effective)
    proj = effective.get("projection") or {}
    ctx = memory_service.context_for_task(conn, run["role_slug"], task)
    # projection.instruction.target 由运行时 YAML 决定（Claude→CLAUDE.md，Codex→AGENTS.md…）
    target_tpl = ((proj.get("instruction") or {}).get("target")
                  or "{{workspace_path}}/CLAUDE.md")
    target = Path(render.render_template(str(target_tpl), {"workspace_path": workspace_path}))
    render.project_instruction(
        target, _instruction_md(role), task or {},
        system_prompt=proj.get("system_prompt") or "",
        responsibilities=proj.get("responsibilities") or [],
        capabilities=ctx.get("capabilities", ""),
        memory=ctx["memory"], snapshot=ctx.get("snapshot", ""),
        notes=ctx["notes"], lessons=ctx["lessons"],
        channel_knowledge=ctx.get("channel_knowledge", ""),
        task_knowledge=ctx.get("task_knowledge", ""),
        topic_knowledge=ctx.get("topic_knowledge", ""),
        knowledge_repos=ctx.get("knowledge_repos"),
        prompt=(((str(run.get("wake_note") or "").strip() + "\n\n")
                 if run.get("wake_note") else "") + (run.get("prompt") or "")
                + ("\n\n" + render.SUBAGENT_PROTOCOL
                   if _supports_subagents(conn, run, effective) else "")),
        protocol=_protocol_for_run(conn, run),
    )
    _project_catalog(conn, effective, workspace_path, run["role_slug"])
    if app_config.CONTEXT_MANIFEST_ENABLED:
        memory = str(ctx.get("memory") or "")
        notes = "\n".join(str(x) for x in (ctx.get("notes") or []))
        lessons = "\n".join(str(x) for x in (ctx.get("lessons") or []))
        manifest = [
            {"name": "identity", "layer": "L0", "kind": "identity",
             "chars": len(str(proj.get("system_prompt") or "")) + len(_instruction_md(role))},
            {"name": "capabilities", "layer": "L0", "kind": "capabilities",
             "scope": "role", "chars": len(str(ctx.get("capabilities") or ""))},
            {"name": "role_principles", "layer": "L3", "kind": "principles",
             "scope": "role", "source_ids": [run["role_slug"]], "chars": len(memory)},
            {"name": "related_knowledge", "layer": "L2", "kind": "knowledge",
             "scope": "role_or_task", "chars": len(notes)},
            {"name": "topic_knowledge", "layer": "L2", "kind": "topic_knowledge",
             "scope": "topic",
             "chars": len(str(ctx.get("topic_knowledge") or ""))},
            {"name": "shared_lessons", "layer": "L4", "kind": "lesson",
             "scope": "shared", "chars": len(lessons)},
            {"name": "channel_knowledge", "layer": "L1", "kind": "channel_knowledge",
             "scope": "channel", "chars": len(str(ctx.get("channel_knowledge") or ""))},
            {"name": "task_knowledge", "layer": "L1", "kind": "task_knowledge",
             "scope": "task", "chars": len(str(ctx.get("task_knowledge") or ""))},
            {"name": "task", "layer": "L1", "kind": "task",
             "scope": "task", "source_ids": [task.get("id")] if task else [],
             "chars": len(str((task or {}).get("description") or ""))},
            {"name": "protocol", "layer": "L0", "kind": "protocol",
             "chars": len(_protocol_for_run(conn, run))},
        ]
        try:
            context_manifest_service.record(
                conn, run_id=run_id, role_slug=run["role_slug"],
                task_id=run.get("task_id"), slice_key=run.get("slice_key"),
                kind="projection",
                budget_chars=0, total_chars=sum(int(b["chars"]) for b in manifest),
                blocks=manifest)
            conn.commit()
        except Exception:  # noqa: BLE001 —— manifest 失败不影响投影
            pass
    if mark_status:
        runs_service.set_status(conn, run_id, "projecting")


def project_run_by_path(db_path: str, run_id: str) -> bool:
    """供 daemon 在实际投递前调用：打开独立连接投影一次，幂等。"""
    conn = open_conn(db_path)
    try:
        run = _run(conn, run_id)
        if not run or run.get("native_session_id"):
            return False
        _project_run(conn, run, mark_status=False)
        return True
    finally:
        conn.close()


def project_node(state: dict, config) -> dict:
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run or run.get("native_session_id"):
            return {}  # 已 spawn 过（重入守卫）或 run 不存在
        effective = _effective(conn, run)
        # 持久会话：投影推迟到 daemon 实际投递前，避免排队任务覆盖正在执行任务的
        # 共享 CLAUDE.md/.claude。one-shot 仍在此投影。
        if app_config.DEFERRED_PROJECTION and _persistent_session(conn, effective):
            runs_service.set_status(conn, run_id, "projecting")
            return {}
        _project_run(conn, run)
        return {}
    finally:
        conn.close()


def _delivery_priority(conn: sqlite3.Connection, run: dict) -> int:
    """队列优先级：回切 > 步骤 > findings > mention > 默认。"""
    run_id = run.get("id")
    if run.get("wake_note"):
        if conn.execute(
                "SELECT 1 FROM task_dispatches WHERE return_run_id=? "
                "AND status IN ('open','return_ready','returning') LIMIT 1",
                (run_id,)).fetchone():
            return 10
        if conn.execute(
                "SELECT 1 FROM runs WHERE context_return_run_id=? "
                "AND context_return_after_turn=1 LIMIT 1",
                (run_id,)).fetchone():
            return 10
    if run.get("step_id") or run.get("origin") == "step":
        return 20
    prompt = str(run.get("prompt") or "")
    if prompt.startswith("[anvil:findings]") or run.get("origin") == "findings":
        return 40
    if run.get("origin") in ("mention", "dispatch") or run.get("dispatch_id"):
        return 50
    if run.get("wake_note"):
        return 50
    return 100


def spawn_node(state: dict, config) -> dict:
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run or run.get("native_session_id"):
            return {}  # 已 spawn（重入守卫）
        role = roles_service.get(conn, run["role_slug"]) or {}
        task = _task(conn, run.get("task_id"))
        effective = _effective(conn, run)
        workspace_path = _run_workspace_path(conn, run, role, effective)
        # 常驻会话模式：唤醒 = 向角色进程 stdin 投递消息（进程存活则复用，不新建）
        persistent = _persistent_session(conn, effective)
        if persistent:
            from ..services import daemon as daemon_service
            try:
                (exe, args_new, args_resume, cwd, env, timeout, pctx, model_pool,
                 window_floor, runtime_id, ws_path) = _render_persistent(
                    conn, run, task, effective, role, workspace_path)
                prompt = _prompt_for_run(conn, run, task)
                if _supports_subagents(conn, run, effective):
                    prompt = prompt + "\n\n" + render.SUBAGENT_PROTOCOL
                delivery = daemon_service.get_manager().ensure_and_deliver(
                    conn, run["role_slug"], executable=exe, args_new=args_new,
                    args_resume=args_resume, ctx=pctx, cwd=cwd, env=env, prompt=prompt,
                    run_id=run_id, task_id=run.get("task_id"),
                    slice_key=run.get("slice_key"),
                    priority=_delivery_priority(conn, run),
                    model_pool=model_pool, context_window_floor=window_floor,
                    runtime_id=runtime_id, workspace_path=ws_path)
            except Exception:  # noqa: BLE001 —— 无法拉起/投递即失败
                logger.exception("persistent spawn failed for run %s", run_id)
                runs_service.set_status(conn, run_id, "failed", ended_at=now_iso(),
                                        failure_class="spawn_error")
                return {}
            if delivery.get("error"):
                logger.warning("persistent delivery failed for run %s: %s",
                               run_id, delivery.get("error"))
                return {}
            runs_service.mark_spawned(conn, run_id, f"rs:{run['role_slug']}",
                                      timeout_seconds=timeout)
            runs_service.set_status(conn, run_id, "running")
            return {}
        try:
            executable, args, cwd, env, timeout = _render_invocation(
                conn, run, task, effective, role, workspace_path)
            pm = factory.get_platform()[0]
            log_path = run.get("log_path")
            if log_path:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "ab") as logf:
                    result = pm.spawn(executable, args, cwd, env, stdout=logf, stderr=subprocess.STDOUT)
            else:
                result = pm.spawn(executable, args, cwd, env)
        except Exception:  # noqa: BLE001 —— 无法拉起子进程即失败，不做静默降级
            logger.exception("spawn failed for run %s", run_id)
            runs_service.set_status(conn, run_id, "failed", ended_at=now_iso(),
                                    failure_class="spawn_error")
            return {}
        # P0-1/P0-2：写会话+冻结超时+重置 started_at（续跑重新计时），再置 running
        runs_service.mark_spawned(conn, run_id, result.native_session_id, timeout_seconds=timeout)
        runs_service.set_status(conn, run_id, "running")
        conn.execute("UPDATE runs SET wake_note=NULL WHERE id=?", (run_id,))
        conn.commit()
        return {}
    finally:
        conn.close()


def submit_node(state: dict, config) -> dict:
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run:
            return {}
        if run["status"] == "running":
            runs_service.set_status(conn, run_id, "submitted")
        return {}
    finally:
        conn.close()


def await_review_node(state: dict, config) -> dict:
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run:
            return {}
        if run["status"] == "submitted":
            runs_service.set_status(conn, run_id, "reviewing")
            # HITL：通知自然人来评审，把 @human 提及打进任务会话时间线
            if run.get("task_id"):
                tasks_service.mention(
                    conn, run["task_id"], run["role_slug"], "human",
                    f"{run['role_slug']} 已提交任务，请评审（通过/驳回）",
                )
        decision = interrupt({"kind": "review", "run_id": run_id, "task_id": run.get("task_id")})
        return {"review_decision": decision}
    finally:
        conn.close()


def review_node(state: dict, config) -> dict:
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run:
            return {}
        decision = state.get("review_decision") or {}
        approve = decision.get("approve", False) if isinstance(decision, dict) else bool(decision)
        if approve:
            if runs_service.set_status(conn, run_id, "completed", ended_at=now_iso()):
                _clear_terminal_role_state(run, run_id)
        else:
            # 驳回：终止旧会话、清 native_session_id → 回 project 重新投影 + 重新 spawn
            if run.get("native_session_id"):
                try:
                    factory.get_platform()[0].terminate(run["native_session_id"])
                except Exception:  # noqa: BLE001
                    pass
            runs_service.set_native_session(conn, run_id, None)
            runs_service.set_status(conn, run_id, "projecting")
        return {}
    finally:
        conn.close()


def _clear_terminal_role_state(run: dict, run_id: str) -> None:
    """run 终态后同步清掉 daemon 上对应的 busy/current 状态。"""
    try:
        from ..services import daemon as daemon_service
        mgr = daemon_service.get_manager()
        if mgr is not None:
            mgr.clear_run(run["role_slug"], run_id)
    except Exception:  # noqa: BLE001 —— 清理失败不影响 run 状态
        logger.exception("clear terminal role state failed run=%s", run_id)


def process_exit_node(state: dict, config) -> dict:
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run:
            return {}
        payload = state.get("payload") or {}
        # 陈旧探测让路：scheduler.monitor 先探测到进程已死，随后用户重启并重新 spawn
        # （mark_spawned 会重置 started_at）。此时旧探测泵入的 process_exit 不能把新
        # 进程判死，否则重启会表现为「失败 → 运行中 → 失败」。
        observed_started = payload.get("observed_started_at")
        if observed_started is not None and run.get("started_at") != observed_started:
            return {}
        # 依据 run 自身状态，而非 task 状态：已终态/已提交（评审流接管）/挂起（suspended）则不动。
        if run["status"] in ("completed", "failed", "abandoned", "submitted", "reviewing", "suspended"):
            if run["status"] in ("failed", "abandoned"):
                _clear_terminal_role_state(run, run_id)
            elif run["status"] in ("submitted", "reviewing", "suspended"):
                # 进程在提交/评审/挂起期间退出：角色不该继续显示这个 run 为 current。
                try:
                    from ..services import daemon as daemon_service
                    mgr = daemon_service.get_manager()
                    if mgr is not None:
                        mgr.clear_current(run["role_slug"], run_id)
                except Exception:  # noqa: BLE001
                    pass
            return {}
        # 上下文溢出由 daemon 回调自动换会话重投；monitor 可能在同一时刻泵 process_exit，
        # 这里必须让路，否则会把待重投的 run 判死（或与重投竞态）。
        rs = conn.execute(
            "SELECT last_error FROM role_sessions WHERE role_slug=?",
            (run["role_slug"],)).fetchone()
        last_error = str((rs["last_error"] if rs else "") or "")
        # 新溢出标记绑定具体 run；旧格式（无 run=）保留兼容，避免升级窗口误判。
        if (last_error.startswith("context overflow")
                and ("run=" not in last_error or f"run={run_id}" in last_error)):
            return {}
        # 挂起：若该角色最后 @提及了某个角色（等待响应），则保持 running，等 @回来再 resume。
        if _waiting_for_reply(
                conn, run.get("task_id"), run["role_slug"],
                slice_key=run.get("slice_key")):
            return {}
        exit_code = payload.get("exit_code")
        # §7.4：退出但未 submit 永不 completed；非零→failed，否则 abandoned。
        # 失败分类（P0-4）：优先用泵入的分类（如 platform_timeout），否则按退出码归因。
        if exit_code not in (None, 0):
            failure_class = payload.get("failure_class") or f"agent_exit_{exit_code}"
            runs_service.set_status(conn, run_id, "failed", ended_at=now_iso(),
                                    failure_class=failure_class)
        else:
            runs_service.set_status(conn, run_id, "abandoned", ended_at=now_iso())
        _clear_terminal_role_state(run, run_id)
        return {}
    finally:
        conn.close()


def resume_node(state: dict, config) -> dict:
    """resume：清空 run 的会话标记，让图重入 spawn 投递新一轮消息。

    - 常驻模式：进程本身长期存活（同一上下文），**不得终止**——spawn 会复用进程并投递消息。
    - one-shot 模式：终止旧子进程（若仍存活）再重新 spawn。
      防进程泄漏：@mention 唤醒时旧进程可能仍在运行；只清 native_session_id 会让旧进程
      变成无人跟踪的孤儿（实测同一角色短时间内叠出 3-5 个 claude 进程）。
    """
    run_id, conn = _ctx(config)
    try:
        run = _run(conn, run_id)
        if not run:
            return {}
        persistent = _persistent_session(conn, _effective(conn, run))
        if not persistent and run.get("native_session_id"):
            try:
                factory.get_platform()[0].terminate(run["native_session_id"])
            except Exception:  # noqa: BLE001 —— 尽力终止，不阻塞续跑
                pass
        runs_service.set_native_session(conn, run_id, None)
        return {}
    finally:
        conn.close()


def _waiting_for_reply(conn: sqlite3.Connection, task_id: str | None,
                       role_slug: str, slice_key: str | None = None) -> bool:
    """该角色最后一条 @提及是否在等待响应（角色或 human）。

    等待 human 确认也是合法的挂起态（HITL / plan_confirm_execute），不能判为 abandoned；
    人类通过 `POST /tasks/{id}/mention` @回该角色时，mention 端点会 resume 这个 run。
    """
    if not task_id:
        return False
    tl = [e for e in tasks_service.timeline(conn, task_id)
          if _timeline_in_scope(e, slice_key)]
    # 只看该角色自己发出的最后一条 @提及；系统/工作流通知（role=workflow）不算等待响应。
    own = [i for i, e in enumerate(tl)
           if e["kind"] == "mention" and e["role"] == role_slug]
    if not own:
        return False
    # 对方已经 @ 回来 = 不再等待。只看「最后一次自己 @」之后的回复；
    # 历史上有过 @ 不代表现在仍在等（否则任何一次 mention 都会让 run 永久 running）。
    last = own[-1]
    return not any(
        e["kind"] == "mention" and e["role"] != role_slug
        and (e.get("payload") or {}).get("target") == role_slug
        for e in tl[last + 1:])
