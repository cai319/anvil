"""Anvil FastAPI application factory.

Exposes `create_app(config: dict) -> FastAPI`, the contract QA's test suite
(`dev/tests/conftest.py`) depends on:

    config = {"db_url": "sqlite+aiosqlite:///<tmp>/test.db", "test_auth": True}
    app = create_app(config)

编排控制面（LangGraph，design §6）：构建运行生命周期图 + SqliteSaver 检查点，挂到
`app.state`。后台调度器仅在 `scheduler_enabled=True`（`server.py` 生产入口）时启动，
默认关闭以保持测试隔离。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import config
from .api import (
    actions, agent_inbox, board, events, governance, inbox, knowledge_repos, memory, reminders, roles, runs, runtimes, tasks, templates, tooling, workflow, workspaces,
)
from .db import connect, open_conn
from .deps import token_is_valid
from .services import daemon as daemon_service
from .services import roles as roles_service
from .services import runs as runs_service
from .graph import build_graph, get_checkpointer
from .graph.orchestrator import Orchestrator
from .scheduler import Scheduler
from .version import VERSION


def _db_path(db_url: str | None) -> str:
    if not db_url:
        return str(config.SQLITE_PATH)
    if ":memory:" in db_url:
        return ":memory:"
    if ":///" in db_url:
        return db_url.split(":///", 1)[1]
    return db_url


def _graph_db_path(db_path: str) -> str:
    """检查点独立文件：测试按 app db 派生 `*-graph.db`，生产用 config.GRAPH_DB_PATH。"""
    if db_path == ":memory:":
        return str(config.GRAPH_DB_PATH)
    if db_path == str(config.SQLITE_PATH):
        return str(config.GRAPH_DB_PATH)
    p = Path(db_path)
    return str(p.with_name(p.stem + "-graph.db"))


def _handle_context_overflow(db_path: str, orchestrator, run_id: str, reason: str,
                             max_retries: int) -> str:
    """上下文溢出后的恢复：retry 时清会话并重新 wake 当前 run；超限则判失败。"""
    conn = open_conn(db_path)
    try:
        if reason == "threshold":
            # 主动水位轮换：正常上下文管理，不计入失败/溢出重试预算。
            outcome = runs_service.reset_run_for_redelivery(
                conn, run_id, reason=reason)
        else:
            outcome = runs_service.register_context_overflow(
                conn, run_id, max_retries=max_retries, reason=reason)
    finally:
        conn.close()
    if outcome == "retry" and orchestrator is not None:
        try:
            orchestrator.pump(db_path, run_id, "wake", {})
        except Exception:  # noqa: BLE001 —— 失败时 scheduler 会拾取 created run
            pass
    return outcome


def _cleanup_orphans(conn) -> None:
    """清理 task 已删除但遗留的 workflow/steps/runs/sessions。

    历史版本 delete_task 未级联 workflow；任务删除后调度器仍会推进孤儿步骤
    并创建 run。启动时做一次幂等清理，避免孤儿继续烧 token。
    """
    orphan_wfs = conn.execute(
        "SELECT id FROM workflows WHERE task_id NOT IN (SELECT id FROM tasks)"
    ).fetchall()
    for wf in orphan_wfs:
        conn.execute("DELETE FROM workflow_steps WHERE workflow_id=?", (wf["id"],))
        conn.execute("DELETE FROM workflows WHERE id=?", (wf["id"],))
    for table in ("task_deps", "task_assignees", "task_role_claims", "task_knowledge_repos"):
        conn.execute(f"DELETE FROM {table} WHERE task_id NOT IN (SELECT id FROM tasks)")
    conn.execute(
        "DELETE FROM task_deps WHERE depends_on NOT IN (SELECT id FROM tasks)")
    conn.execute(
        "DELETE FROM runs WHERE task_id IS NOT NULL "
        "AND task_id NOT IN (SELECT id FROM tasks)")
    conn.execute(
        "DELETE FROM sessions WHERE task_id IS NOT NULL "
        "AND task_id NOT IN (SELECT id FROM tasks)")
    conn.execute(
        "DELETE FROM activities WHERE task_id IS NOT NULL "
        "AND task_id NOT IN (SELECT id FROM tasks)")
    conn.execute(
        "DELETE FROM role_inbox_cursors WHERE task_id IS NOT NULL "
        "AND task_id NOT IN (SELECT id FROM tasks)")
    conn.execute(
        "DELETE FROM role_task_sessions WHERE task_key<>'__global__' "
        "AND task_key NOT IN (SELECT id FROM tasks)")
    conn.execute(
        "DELETE FROM reminders WHERE role_slug IS NOT NULL "
        "AND role_slug NOT IN (SELECT slug FROM roles)")
    conn.commit()


class _LocalTokenMiddleware(BaseHTTPMiddleware):
    """统一 API 鉴权：/api/v1 下所有请求都必须携带有效 token。

    token 来源：
    - `X-Anvil-Session`（默认）
    - `?token=` 仅用于 SSE `/api/v1/events`（EventSource 无法自定义 header）

    有效 token = 未过期的运行级 session，或人类共享 token（`ANVIL_AUTH_TOKEN`；
    未设置时只接受 `anvil` / `anvil-web`）。测试旁路 `ANVIL_TEST_AUTH=1` 下跳过。
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/api/v1"):
            test_auth = bool(getattr(request.app.state, "test_auth", False)) or os.environ.get("ANVIL_TEST_AUTH") == "1"
            if not test_auth:
                host = request.client.host if request.client else ""
                loopback = host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
                if not config.AUTH_TOKEN and not loopback:
                    return JSONResponse(
                        {"detail": "remote access requires ANVIL_AUTH_TOKEN"},
                        status_code=403,
                    )
                token = request.headers.get("X-Anvil-Session")
                if not token and path == "/api/v1/events":
                    token = request.query_params.get("token")
                if not token:
                    return JSONResponse({"detail": "missing X-Anvil-Session"}, status_code=401)
                if not token_is_valid(request, token):
                    return JSONResponse({"detail": "invalid X-Anvil-Session"}, status_code=401)
        return await call_next(request)


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or {}
    path = _db_path(cfg.get("db_url"))
    if path != ":memory:":
        data_dir = Path(path).parent
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            data_dir.chmod(0o700)
        except OSError:
            pass
    _init = connect(path)
    # 历史删除遗留的孤儿 workflow/run：启动时清理，避免调度器继续推进已删除任务。
    _cleanup_orphans(_init)
    # 跨平台/迁移修复：数据库里的角色 home_path 可能来自旧系统（如 macOS → Windows）。
    roles_service.repair_home_paths(_init)
    # 旧角色可能早于每日复盘功能创建；启动时为所有启用角色补齐/迁移 reminder。
    roles_service.ensure_daily_review_reminders(_init)
    _init.close()  # 建表后即关，之后每请求独立连接

    # 编排控制面：同步 LangGraph + SqliteSaver（长驻 app 生命周期）。
    graph_db_path = _graph_db_path(path)
    checkpointer = get_checkpointer(graph_db_path)
    graph = build_graph(checkpointer)
    orchestrator = Orchestrator(graph, checkpointer)

    scheduler_enabled = bool(cfg.get("scheduler_enabled"))
    test_auth = bool(cfg.get("test_auth")) or os.environ.get("ANVIL_TEST_AUTH") == "1"
    if test_auth and scheduler_enabled:
        raise RuntimeError(
            "ANVIL_TEST_AUTH is test-only and must not be enabled in the production server")
    scheduler = Scheduler(path, orchestrator) if scheduler_enabled else None

    # 常驻角色运行时（owner 2026-10-19）：生产入口（scheduler_enabled）默认启用；
    # 测试/纯 API 模式默认关闭（保持 one-shot 假进程可测），可用 ANVIL_PERSISTENT_RUNTIME 覆盖。
    persistent_enabled = config.PERSISTENT_RUNTIME == "1" or (
        config.PERSISTENT_RUNTIME == "auto" and scheduler_enabled)
    role_daemon = daemon_service.init_manager(path, persistent_enabled)
    if persistent_enabled:
        def _on_role_exit(role: str, code: int) -> None:
            """常驻进程意外退出：把该角色所有活跃 run 泵进 process_exit。"""
            c = open_conn(path)
            try:
                rows = c.execute(
                    "SELECT id FROM runs WHERE role_slug=? AND status IN ('created','projecting','running')",
                    (role,),
                ).fetchall()
            finally:
                c.close()
            for r in rows:
                try:
                    orchestrator.pump(path, r["id"], "process_exit", {"exit_code": code})
                except Exception:  # noqa: BLE001
                    pass
        role_daemon.on_exit = _on_role_exit

        def _on_role_context_overflow(role: str, run_id: str, reason: str) -> None:
            """上下文溢出：清会话后用 CIC 新会话自动重投当前 run；超限则判失败。

            daemon 只负责发现并终止会话；run 的恢复动作放在这里，避免反向依赖编排器。
            """
            _handle_context_overflow(path, orchestrator, run_id, reason,
                                     config.CONTEXT_RESET_MAX_RETRIES)
        role_daemon.on_context_overflow = _on_role_context_overflow

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动自愈（P0-3）：上一实例崩溃遗留的 running/projecting run → suspended（可 @mention 续跑）
        _conn = open_conn(path)
        try:
            runs_service.heal_stale_runs(_conn)
        finally:
            _conn.close()
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            scheduler.stop()
        # 常驻角色进程先关闭（各任务上下文槽保留 session，下次按任务 --resume）
        role_daemon.shutdown()
        # 优雅停机（P0-3）：终止活跃子进程（防孤儿烧钱），run 转 suspended（非 failed，可续跑）
        _conn = open_conn(path)
        try:
            runs_service.suspend_active(_conn)
        finally:
            _conn.close()

    app = FastAPI(title="Anvil", version=VERSION, lifespan=lifespan)
    app.state.db_path = path
    app.state.test_auth = test_auth
    app.state.graph_db_path = graph_db_path
    app.state.checkpointer = checkpointer
    app.state.graph = graph
    app.state.orchestrator = orchestrator
    app.state.scheduler = scheduler
    app.state.scheduler_enabled = scheduler_enabled

    app.add_middleware(_LocalTokenMiddleware)

    app.include_router(runtimes.router, prefix="/api/v1/runtimes", tags=["runtimes"])
    app.include_router(roles.router, prefix="/api/v1", tags=["roles"])
    app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["tasks"])
    app.include_router(inbox.router, prefix="/api/v1", tags=["inbox"])
    app.include_router(agent_inbox.router, prefix="/api/v1", tags=["agent-inbox"])
    app.include_router(actions.router, prefix="/api/v1", tags=["actions"])
    app.include_router(workflow.router, prefix="/api/v1", tags=["workflow"])
    app.include_router(memory.router, prefix="/api/v1", tags=["memory"])
    app.include_router(knowledge_repos.router, prefix="/api/v1/knowledge", tags=["knowledge-repos"])
    app.include_router(workspaces.router, prefix="/api/v1", tags=["workspaces"])
    app.include_router(governance.router, prefix="/api/v1", tags=["governance"])
    app.include_router(runs.router, prefix="/api/v1", tags=["runs"])
    app.include_router(board.router, prefix="/api/v1", tags=["board"])
    app.include_router(templates.router, prefix="/api/v1", tags=["workflow-templates"])
    app.include_router(reminders.router, prefix="/api/v1", tags=["reminders"])
    app.include_router(tooling.router, prefix="/api/v1", tags=["tooling"])
    app.include_router(events.router, prefix="/api/v1", tags=["events"])

    # 前端静态（打包后随 PyInstaller 数据目录，dev 用 frontend/dist build 产物）。
    if config.FRONTEND_DIST.exists():
        assets = config.FRONTEND_DIST / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404)
            root = config.FRONTEND_DIST.resolve()
            candidate = (root / full_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                # 路径穿越：绝不把 dist 之外的文件当静态资源返回
                raise HTTPException(status_code=404)
            if candidate.is_file():
                return FileResponse(candidate)
            index = root / "index.html"
            if index.exists():
                return FileResponse(index, headers={"Cache-Control": "no-cache"})
            raise HTTPException(status_code=404)

    return app


# Module-level app for `uvicorn anvil.main:app`.
app = create_app()
