"""Run 端点（M2/M4）：创建 / 列表 / 查询 / 三档重置 / 释放进程。"""
from __future__ import annotations

import shutil
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import config, services
from ..deps import Auth, get_auth, get_db, require_human
from ..platform import factory
from ..util import now_iso

router = APIRouter()


@router.post("/runs", status_code=201)
def start_run(body: dict, request: Request, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    role = body.get("role_slug") or auth.role or "qa-role"
    task_id = body.get("task_id")
    # 幂等：同任务同角色已有活跃 run 时直接返回它（不二次 spawn、也不报冲突；多角色可各有一 run）
    if task_id and role:
        existing = services.runs.find_active_run(conn, task_id, role)
        if existing:
            return existing
    run = services.runs.create_run(conn, role, task_id)
    orch = getattr(request.app.state, "orchestrator", None)
    if orch:
        try:
            orch.pump(request.app.state.db_path, run["id"], "wake", {})
        except Exception:  # noqa: BLE001
            pass
    return run


@router.get("/runs")
def list_runs(role_slug: str | None = None, task_id: str | None = None,
              active: bool = False, conn=Depends(get_db)):
    """active=true 只返回活跃会话（含挂起等待），供主页实时看板使用。"""
    statuses = list(services.runs.ACTIVE_RUN_STATUSES) if active else None
    return services.runs.list_runs(conn, role_slug=role_slug, task_id=task_id, statuses=statuses)


@router.get("/runs/{rid}")
def get_run(rid: str, conn=Depends(get_db)):
    run = services.runs.get_run(conn, rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/runs/{rid}/timeline")
def run_timeline(rid: str, conn=Depends(get_db)):
    """该 run 的生命周期迁移事件（运行会话追溯）。"""
    if services.runs.get_run(conn, rid) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return services.runs.run_timeline(conn, rid)


@router.get("/runs/{rid}/log")
def run_log(rid: str, conn=Depends(get_db),
            auth: Auth = Depends(require_human)):
    """该 run 的 stdout/stderr 日志。"""
    if services.runs.get_run(conn, rid) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return services.runs.get_run_log(conn, rid)


@router.get("/runs/{rid}/snapshot")
def run_snapshot(rid: str, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    """该 run 冻结的 profile 快照（config snapshot）。"""
    if services.runs.get_run(conn, rid) is None:
        raise HTTPException(status_code=404, detail="run not found")
    snap = services.runs.get_snapshot(conn, rid)
    return snap or {}


def _terminate(run: dict) -> None:
    sid = run.get("native_session_id")
    if not sid:
        return
    try:
        factory.get_platform()[0].terminate(sid)
    except Exception:  # noqa: BLE001
        pass


def _backup_home(role_slug: str) -> str | None:
    home = config.AGENTS_DIR / role_slug
    if not home.exists():
        return None
    backups = config.DATA_DIR / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups / f"{role_slug}-{stamp}"
    try:
        return shutil.make_archive(str(target), "gztar", root_dir=str(home))
    except Exception:  # noqa: BLE001
        return None


def _clear_memory(role_slug: str) -> None:
    home = config.AGENTS_DIR / role_slug
    if not home.exists():
        return
    (home / "MEMORY.md").write_text("", encoding="utf-8")
    for sub in ("notes", "scratch"):
        d = home / sub
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file():
                    p.unlink()


def _reset(run_id: str, mode: str, request: Request, conn) -> dict:
    run = services.runs.get_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    from ..services import daemon as daemon_service

    mgr = daemon_service.get_manager()
    _terminate(run)
    if mode == "session":
        # 丢弃任务上下文前先固化知识快照（full reset 会连 notes 一起清，不需要）。
        try:
            services.memory.consolidate_context(
                conn, run["role_slug"], run.get("task_id"),
                "run-session-reset", run_id)
        except Exception:  # noqa: BLE001 —— 快照失败不阻断 reset
            pass

    # 常驻模式必须同时处理角色进程和 role_sessions：
    # - restart：杀进程但保留 session_id，下一次 --resume 同一上下文；
    # - session/full：杀进程并清空 session_id，下一次用 args_new 新建上下文。
    if mgr and mgr.enabled:
        if mode == "restart":
            mgr.restart_process(run["role_slug"])
        elif mode == "session":
            # 只清该 run 所属任务的上下文；角色在其它任务上的会话保留。
            mgr.reset_session(run["role_slug"], task_id=run.get("task_id"))
        else:
            mgr.reset_session(run["role_slug"])
    elif mode == "session":
        # one-shot / daemon 未启用：只清该 run 所属任务槽。
        daemon_service.clear_session(
            conn, run["role_slug"], "session reset",
            task_key_value=daemon_service.task_key(run.get("task_id")))
    elif mode == "full":
        daemon_service.clear_session(conn, run["role_slug"], "full reset")

    services.runs.set_native_session(conn, run_id, None)

    backup = None
    if mode == "full":
        backup = _backup_home(run["role_slug"])
        _clear_memory(run["role_slug"])
        # 记忆索引同步清空（文件是真相，索引不能留下幽灵条目）
        conn.execute("DELETE FROM memories WHERE role_slug=?", (run["role_slug"],))
        conn.execute("DELETE FROM notes WHERE role_slug=?", (run["role_slug"],))
        try:
            conn.execute("DELETE FROM memory_fts WHERE role_slug=?", (run["role_slug"],))
        except Exception:  # noqa: BLE001
            pass
        conn.commit()

    # 三档重置都回到 created，由调度器重新 project + spawn：
    # restart 保留 session_id；session/full 已清空 session_id。
    services.runs.set_status(conn, run_id, "created")
    orch = getattr(request.app.state, "orchestrator", None)
    if orch:
        try:
            orch.pump(request.app.state.db_path, run_id, "wake", {})
        except Exception:  # noqa: BLE001
            pass
    result = services.runs.get_run(conn, run_id) or {}
    return {**result, "reset": mode, "backup": backup}


@router.post("/runs/{rid}/restart")
def restart_run(rid: str, request: Request, conn=Depends(get_db),
                auth: Auth = Depends(require_human)):
    """restart：结束进程、保留会话记忆，重新拉起（design §6.3）。"""
    return _reset(rid, "restart", request, conn)


@router.post("/runs/{rid}/session-reset")
def session_reset(rid: str, request: Request, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    """session reset：丢弃会话上下文，保留 MEMORY.md 与 notes。"""
    return _reset(rid, "session", request, conn)


@router.post("/runs/{rid}/full-reset")
def full_reset(rid: str, request: Request, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    """full reset：会话与记忆全清，仅保留 profile/instruction；先自动备份 home。"""
    return _reset(rid, "full", request, conn)


@router.delete("/runs/{rid}", status_code=204)
def release_run(rid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    """释放进程（design §10 DELETE /runs/{id}）。"""
    run = services.runs.get_run(conn, rid)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    _terminate(run)
    services.runs.set_native_session(conn, rid, None)
    services.runs.set_status(conn, rid, "abandoned", ended_at=now_iso())
    return None
