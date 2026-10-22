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
    actions, agent_inbox, board, events, governance, inbox, memory, reminders, roles, runs, runtimes, tasks, tooling, workflow, workspaces,
)
from .db import connect, open_conn
from .services import daemon as daemon_service
from .services import roles as roles_service
from .services import runs as runs_service
from .graph import build_graph, get_checkpointer
from .graph.orchestrator import Orchestrator
from .scheduler import Scheduler


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


class _LocalTokenMiddleware(BaseHTTPMiddleware):
    """本地 token 轻校验（design §1.4 / §10）：所有写操作必须带 X-Anvil-Session。

    只做「存在性」校验；具体 token 是否有效由 `deps.get_auth`（共享 token 或运行级
    会话令牌）判断。测试旁路 `ANVIL_TEST_AUTH=1` 下跳过。
    """

    async def dispatch(self, request, call_next):
        if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                and request.url.path.startswith("/api/v1")):
            test_auth = bool(getattr(request.app.state, "test_auth", False)) or os.environ.get("ANVIL_TEST_AUTH") == "1"
            if not test_auth and not request.headers.get("X-Anvil-Session"):
                return JSONResponse({"detail": "missing X-Anvil-Session"}, status_code=401)
        return await call_next(request)


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or {}
    path = _db_path(cfg.get("db_url"))
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    _init = connect(path)
    # 跨平台/迁移修复：数据库里的角色 home_path 可能来自旧系统（如 macOS → Windows）。
    roles_service.repair_home_paths(_init)
    _init.close()  # 建表后即关，之后每请求独立连接

    # 编排控制面：同步 LangGraph + SqliteSaver（长驻 app 生命周期）。
    graph_db_path = _graph_db_path(path)
    checkpointer = get_checkpointer(graph_db_path)
    graph = build_graph(checkpointer)
    orchestrator = Orchestrator(graph, checkpointer)

    scheduler_enabled = bool(cfg.get("scheduler_enabled"))
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
        # 常驻角色进程先关闭（session_id 保留在 role_sessions，下次 --resume 恢复上下文）
        role_daemon.shutdown()
        # 优雅停机（P0-3）：终止活跃子进程（防孤儿烧钱），run 转 suspended（非 failed，可续跑）
        _conn = open_conn(path)
        try:
            runs_service.suspend_active(_conn)
        finally:
            _conn.close()

    app = FastAPI(title="Anvil", version="0.1.0", lifespan=lifespan)
    app.state.db_path = path
    app.state.test_auth = bool(cfg.get("test_auth")) or os.environ.get("ANVIL_TEST_AUTH") == "1"
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
    app.include_router(workspaces.router, prefix="/api/v1", tags=["workspaces"])
    app.include_router(governance.router, prefix="/api/v1", tags=["governance"])
    app.include_router(runs.router, prefix="/api/v1", tags=["runs"])
    app.include_router(board.router, prefix="/api/v1", tags=["board"])
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
            candidate = config.FRONTEND_DIST / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            index = config.FRONTEND_DIST / "index.html"
            if index.exists():
                return FileResponse(index, headers={"Cache-Control": "no-cache"})
            raise HTTPException(status_code=404)

    return app


# Module-level app for `uvicorn anvil.main:app`.
app = create_app()
