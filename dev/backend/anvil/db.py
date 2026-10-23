"""SQLite state layer.

Design stance (§1.5): files are the source of truth; SQLite stores state,
events, and the search index. Each app instance owns one connection (see
`main.create_app`), so tests get isolated per-`create_app` DBs.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runtimes (
    id                TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL,
    executable_path   TEXT NOT NULL,
    detected_version  TEXT,
    status            TEXT NOT NULL DEFAULT 'unverified',  -- ready|unverified|unhealthy|missing
    capabilities      TEXT NOT NULL DEFAULT '{}',
    definition_hash   TEXT,
    last_probe_at     TEXT,
    last_probe_log    TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS roles (
    slug            TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    avatar_color    TEXT,
    avatar          TEXT,
    home_path       TEXT NOT NULL,
    definition_json TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    type             TEXT,
    status           TEXT NOT NULL DEFAULT 'todo',  -- todo|in_progress|in_review|done|closed
    priority         TEXT,
    tags_json        TEXT NOT NULL DEFAULT '[]',
    repo             TEXT,
    base_branch      TEXT,
    scope_paths_json TEXT NOT NULL DEFAULT '[]',
    description      TEXT,
    acceptance_json  TEXT NOT NULL DEFAULT '[]',
    assignee         TEXT,
    lease_expires_at TEXT,
    claimed_at       TEXT,
    created_by       TEXT,
    parent_id        TEXT,
    -- DEPRECATED: 旧版美元成本字段，仅保留兼容；不再写入/展示。Token 见 token_usage。
    cost_budget_usd  REAL,
    cost_used_usd    REAL NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id    TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS task_assignees (
    task_id   TEXT NOT NULL,
    role_slug TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (task_id, role_slug)
);

-- 工作流（Cindy 生成，属于一个高层 Task）：步骤状态机，步骤不是独立 task 行。
-- 跨角色可并行；同一角色的多个步骤由引擎串行调度。
CREATE TABLE IF NOT EXISTS workflows (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',  -- running|done|failed
    created_by      TEXT,
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    workflow_id      TEXT NOT NULL,
    step_id          TEXT NOT NULL,
    title            TEXT NOT NULL,
    role_slug        TEXT NOT NULL,
    instructions     TEXT,
    depends_on_json  TEXT NOT NULL DEFAULT '[]',
    acceptance_json  TEXT NOT NULL DEFAULT '[]',
    gate             INTEGER NOT NULL DEFAULT 0,   -- 1 = 提交后需人工审核
    reviewer         TEXT,                          -- 验证者角色（必须不同于执行角色）
    contract         TEXT,                          -- 契约/不变量（contract-first）
    failure_reason   TEXT,                          -- 失败归因（spec/contract/test/env/agent）
    last_nudge_at    TEXT,                          -- 停滞看门狗：上次唤醒时间
    nudge_count      INTEGER NOT NULL DEFAULT 0,    -- 停滞唤醒次数（超过上限交给自动恢复）
    recovery_count   INTEGER NOT NULL DEFAULT 0,    -- 自动恢复重试次数（跨 run 累计）
    next_retry_at    TEXT,                          -- 自动恢复的下次可调度时间（指数退避）
    kind             TEXT NOT NULL DEFAULT 'task',   -- task | decision | plan_slices
    on_reject_json   TEXT NOT NULL DEFAULT '[]',     -- decision 驳回时需重做的步骤
    max_rework       INTEGER NOT NULL DEFAULT 2,
    rework_count     INTEGER NOT NULL DEFAULT 0,
    feedback         TEXT,                           -- 最近决策/评审反馈
    source_step_id   TEXT,                           -- 动态切片：生成该步骤的 plan_slices 步骤
    slice_key        TEXT,                           -- 动态切片：所属切片 id
    plan_json        TEXT,                           -- plan_slices：切片计划原文
    status           TEXT NOT NULL DEFAULT 'pending',
    -- pending|running|submitted|approved|rejected|failed|skipped
    run_id           TEXT,
    summary          TEXT,
    started_at       TEXT,
    ended_at         TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (workflow_id, step_id)
);

-- 任务内每个参与角色的独立工作状态（多角色并行协作）。
-- status: pending|working|submitted|released|approved
CREATE TABLE IF NOT EXISTS task_role_claims (
    task_id          TEXT NOT NULL,
    role_slug        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    summary          TEXT,
    run_id           TEXT,
    claimed_at       TEXT,
    lease_expires_at TEXT,
    submitted_at     TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (task_id, role_slug)
);

CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    role_slug         TEXT NOT NULL,
    task_id           TEXT,
    status            TEXT NOT NULL DEFAULT 'created',
    -- 8-state lifecycle: created|projecting|running|submitted|reviewing|completed|failed|abandoned
    profile_snapshot  TEXT,
    worktree_path     TEXT,
    native_session_id TEXT,
    log_path          TEXT,
    prompt            TEXT,   -- 无任务 run（reminder / 直接消息）的唤醒提示
    started_at        TEXT,
    ended_at          TEXT
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    digest       TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 角色配置版本（每次 create/update/rollback 留一版，供回滚与 diff）
CREATE TABLE IF NOT EXISTS role_versions (
    slug            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    display_name    TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'update',
    created_at      TEXT NOT NULL,
    PRIMARY KEY (slug, version)
);

CREATE TABLE IF NOT EXISTS role_tools (
    role_slug       TEXT PRIMARY KEY,
    preset          TEXT,
    max_tool_tokens INTEGER,
    allowlist_json  TEXT NOT NULL DEFAULT '[]',
    skills_json     TEXT NOT NULL DEFAULT '[]',
    mcp_json        TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id             TEXT PRIMARY KEY,
    role_slug      TEXT NOT NULL,
    kind           TEXT NOT NULL,   -- cron|at|when
    cron_expr      TEXT,
    fire_at        TEXT,
    when_condition TEXT,
    prompt         TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    next_fire_at   TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    tags_json   TEXT NOT NULL DEFAULT '[]',
    source_task TEXT,
    repo        TEXT NOT NULL DEFAULT 'shared',  -- 归属共享仓库（shared = 全局经验）
    last_hit_at TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    created_at  TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id            TEXT PRIMARY KEY,
    role_slug     TEXT NOT NULL,
    field         TEXT NOT NULL,
    current_json  TEXT,
    proposed_json TEXT,
    rationale     TEXT,
    risk          TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    created_at    TEXT NOT NULL,
    decided_at    TEXT
);

-- Token 消耗账本（run 的 result.usage；input 含 cache read/creation，output 为生成）
CREATE TABLE IF NOT EXISTS token_usage (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    role_slug             TEXT,
    task_id               TEXT,
    run_id                TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    recorded_at           TEXT NOT NULL
);

-- DEPRECATED: 旧版美元成本账本；不再写入/展示。Token 统计见 token_usage。
CREATE TABLE IF NOT EXISTS cost_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role_slug   TEXT,
    task_id     TEXT,
    run_id      TEXT,
    usd         REAL NOT NULL,
    source      TEXT NOT NULL,  -- reported|estimated
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT,
    role_slug    TEXT,
    run_id       TEXT,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activities_task_id ON activities(task_id, id);

CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    role_slug    TEXT,
    task_id      TEXT,
    action       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

-- 角色常驻运行时（persistent daemon）：每角色最多一个活跃 CLI 进程。
-- session_id 是「当前加载的任务上下文槽」的镜像；真正的会话按
-- (role_slug, task_key) 保存在 role_task_sessions，切换任务时恢复对应槽。
CREATE TABLE IF NOT EXISTS role_sessions (
    role_slug      TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    token          TEXT,            -- 角色级会话令牌（task_id=NULL）
    model_id       TEXT,            -- 最近一次 init 事件报告的模型 id
    pid            INTEGER,
    status         TEXT NOT NULL DEFAULT 'idle',  -- starting|idle|busy|dead
    busy_since     TEXT,
    last_active_at TEXT,
    last_error     TEXT,
    updated_at     TEXT NOT NULL
);

-- 角色 × 任务上下文槽：一个角色可以在多个任务上工作，但同一时刻只执行一个。
-- 每个槽保存该任务独立的 CLI 会话、cwd、工作区和上下文水位；切回任务时
-- 用 --resume 恢复，首次进入则新建会话并注入 CIC。
CREATE TABLE IF NOT EXISTS role_task_sessions (
    role_slug      TEXT NOT NULL,
    task_key       TEXT NOT NULL,               -- task id 或 __global__
    session_id     TEXT NOT NULL DEFAULT '',
    runtime_id     TEXT,
    cwd            TEXT,                        -- CLI 会话所属目录（--resume 必须一致）
    workspace_path TEXT,                        -- 投影/工作区路径（同任务复用）
    model_id       TEXT,
    status         TEXT NOT NULL DEFAULT 'idle', -- idle|busy|dead
    context_tokens INTEGER,
    last_seen_activity_id INTEGER NOT NULL DEFAULT 0,
    last_active_at TEXT,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (role_slug, task_key)
);

-- Action Cards：Agent 只能 prepare，自然人批准后以人的身份执行（HITL 一等原语）
CREATE TABLE IF NOT EXISTS action_cards (
    id          TEXT PRIMARY KEY,
    role_slug   TEXT NOT NULL,
    task_id     TEXT,
    kind        TEXT NOT NULL,                 -- task.cancel | task.reopen
    payload_json TEXT NOT NULL DEFAULT '{}',
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|rejected|executed|failed
    result_json TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

-- Agent Inbox 拉模式 cursor（role × task；两阶段：check 读、commit 提交）
CREATE TABLE IF NOT EXISTS role_inbox_cursors (
    role_slug        TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    last_activity_id INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (role_slug, task_id)
);

-- Agent 会话令牌（design §10 末段：X-Anvil-Session 校验该会话能否操作目标任务）
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    role_slug  TEXT NOT NULL,
    run_id     TEXT,
    task_id    TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

-- 治理/运行时键值（全场冻结等全局开关）
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 目标拆解 draft（编排者产出任务树，人类审阅后落板）
CREATE TABLE IF NOT EXISTS board_drafts (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT,
    goal         TEXT NOT NULL,
    tree_json    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);

-- 频道（工作区即频道，§B）
CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    repo        TEXT,
    description TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 频道成员（角色↔频道多对多，频道侧权威）
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL,
    role_slug    TEXT NOT NULL,
    access       TEXT NOT NULL,  -- read|write|admin
    joined_at    TEXT NOT NULL,
    PRIMARY KEY (workspace_id, role_slug)
);

-- Skill 注册表（M3）：磁盘目录仍是真相；DB 用于 CRUD、版本与跨运行时分发。
CREATE TABLE IF NOT EXISTS skills (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    content     TEXT NOT NULL DEFAULT '',
    runtime     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, version)
);

-- MCP server 注册表（M3）：支持 stdio / http；tools_json 为最近一次握商缓存。
CREATE TABLE IF NOT EXISTS mcp_servers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    transport  TEXT NOT NULL DEFAULT 'stdio',  -- stdio|http|sse
    command    TEXT,
    args_json  TEXT NOT NULL DEFAULT '[]',
    env_json   TEXT NOT NULL DEFAULT '{}',
    url        TEXT,
    tools_json TEXT NOT NULL DEFAULT '[]',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Memory/notes index layer (files are truth; these back search/append).
CREATE TABLE IF NOT EXISTS memories (
    id         TEXT PRIMARY KEY,
    role_slug  TEXT NOT NULL,
    content    TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT 'self',  -- self|shared
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id         TEXT PRIMARY KEY,
    role_slug  TEXT NOT NULL,
    path       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- 知识元数据：scope=task 仅同任务可见；scope=role 可跨任务复用；
    -- legacy/未知笔记保持 unclassified，默认不自动注入。
    kind          TEXT NOT NULL DEFAULT 'note',
    scope         TEXT NOT NULL DEFAULT 'unclassified',
    task_id       TEXT,
    source_task   TEXT,
    tags_json     TEXT NOT NULL DEFAULT '[]',
    repo          TEXT,
    module        TEXT,
    confidence    REAL NOT NULL DEFAULT 0.5,
    updated_at    TEXT,
    source        TEXT NOT NULL DEFAULT 'agent',
    content_hash  TEXT
);
"""

# FTS5 is M4 (memory search); create it now if the build supports it, otherwise
# search degrades to LIKE.
FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
    "USING fts5(role_slug, content, tokenize='porter')"
)


def _raw_connect(path: str) -> sqlite3.Connection:
    # 每请求独立连接，但 FastAPI 同步依赖的 setup 与端点可能跑在不同线程池 worker，
    # 同一请求内跨线程顺序使用（无并发），需 check_same_thread=False 避免误报。
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def connect(path: str) -> sqlite3.Connection:
    """建库（含建表）。仅在 create_app 时调用一次，建表后即关。"""
    conn = _raw_connect(path)
    init_schema(conn)
    if path != ":memory:":
        try:
            from pathlib import Path
            Path(path).chmod(0o600)
        except OSError:
            pass
    return conn


def open_conn(path: str) -> sqlite3.Connection:
    """每请求独立连接（不重建表）。避免跨线程共享同一个 connection。"""
    return _raw_connect(path)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    try:
        conn.execute(FTS_SCHEMA)
    except sqlite3.OperationalError:
        # FTS5 not compiled into this Python build — memory search falls back to LIKE.
        pass
    # 迁移：既有 tasks 表补 workspace_id 列（B 多频道）
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN workspace_id TEXT")
    except sqlite3.OperationalError:
        pass
    # 迁移：roles 补 avatar 列（角色头像预设；NULL = 按 slug 自动匹配）
    try:
        conn.execute("ALTER TABLE roles ADD COLUMN avatar TEXT")
    except sqlite3.OperationalError:
        pass
    # 迁移：role_tools 补 skill/mcp 选择列（M3 工具治理）
    for col in ("skills_json", "mcp_json"):
        try:
            conn.execute(f"ALTER TABLE role_tools ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
    # 迁移：runs 补 prompt 列（reminder / 直接消息 run）
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN prompt TEXT")
    except sqlite3.OperationalError:
        pass
    # 迁移：lessons 补 repo / approved_at 列（M4 升格审批）
    for col in ("repo TEXT NOT NULL DEFAULT 'shared'", "approved_at TEXT"):
        try:
            conn.execute(f"ALTER TABLE lessons ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # 迁移：清理历史空字符串 assignee（早期 PATCH 空值 bug 遗留），否则 claim 匹配不上
    try:
        conn.execute("UPDATE tasks SET assignee=NULL WHERE assignee=''")
    except sqlite3.OperationalError:
        pass
    # 迁移：为已有团队任务补角色认领状态（从最新的 run 反推，避免历史任务显示为空白）
    try:
        missing = conn.execute(
            "SELECT a.task_id, a.role_slug FROM task_assignees a "
            "LEFT JOIN task_role_claims c ON c.task_id=a.task_id AND c.role_slug=a.role_slug "
            "WHERE c.task_id IS NULL"
        ).fetchall()
        for row in missing:
            run = conn.execute(
                "SELECT id, status, started_at FROM runs WHERE task_id=? AND role_slug=? "
                "ORDER BY started_at DESC LIMIT 1", (row["task_id"], row["role_slug"])
            ).fetchone()
            status = "pending"
            run_id = None
            claimed_at = None
            if run:
                run_id = run["id"]
                claimed_at = run["started_at"]
                status = {
                    "completed": "submitted", "reviewing": "submitted", "submitted": "submitted",
                    "running": "working", "created": "working", "projecting": "working",
                    "failed": "released", "abandoned": "released",
                }.get(run["status"], "pending")
            conn.execute(
                "INSERT INTO task_role_claims (task_id, role_slug, status, run_id, claimed_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (row["task_id"], row["role_slug"], status, run_id, claimed_at,
                 datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.OperationalError:
        pass
    # 迁移：为已有角色补一份 v1 配置版本（新表 role_versions 的引导）
    now = datetime.now(timezone.utc).isoformat()
    try:
        rows = conn.execute("SELECT slug, display_name, definition_json FROM roles").fetchall()
        for r in rows:
            exists = conn.execute(
                "SELECT 1 FROM role_versions WHERE slug=? LIMIT 1", (r["slug"],)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO role_versions (slug, version, display_name, definition_json, source, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (r["slug"], 1, r["display_name"], r["definition_json"], "bootstrap", now),
                )
    except sqlite3.OperationalError:
        pass
    # 迁移：workflow——run 绑定步骤；草案区分 tasks / workflow
    for col in ("step_id TEXT", "workflow_id TEXT"):
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE board_drafts ADD COLUMN kind TEXT NOT NULL DEFAULT 'tasks'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE workflow_steps ADD COLUMN reviewer TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE role_sessions ADD COLUMN last_context_tokens INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE role_sessions ADD COLUMN model_id TEXT")
    except sqlite3.OperationalError:
        pass
    # 迁移：多任务角色调度——把当前任务/run/步骤持久化，供 UI 与调度器观测。
    for col in ("current_task_id TEXT", "current_run_id TEXT", "current_step_id TEXT"):
        try:
            conn.execute(f"ALTER TABLE role_sessions ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # 旧版只有一个角色级 session：保守地放入 __global__ 槽，不冒充任何任务上下文。
    try:
        legacy = conn.execute(
            "SELECT role_slug, session_id, model_id, last_context_tokens FROM role_sessions "
            "WHERE COALESCE(session_id,'')<>''"
        ).fetchall()
        for row in legacy:
            has_slot = conn.execute(
                "SELECT 1 FROM role_task_sessions WHERE role_slug=? LIMIT 1",
                (row["role_slug"],)).fetchone()
            if has_slot:
                continue  # 已经是多槽版本的数据，不再猜测旧 session 属于哪个任务
            conn.execute(
                "INSERT INTO role_task_sessions "
                "(role_slug, task_key, session_id, model_id, context_tokens, status, "
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (row["role_slug"], "__global__", row["session_id"], row["model_id"],
                 row["last_context_tokens"], "idle", now, now),
            )
    except sqlite3.OperationalError:
        pass
    for col in ("last_nudge_at TEXT", "nudge_count INTEGER NOT NULL DEFAULT 0",
                "recovery_count INTEGER NOT NULL DEFAULT 0", "next_retry_at TEXT"):
        try:
            conn.execute(f"ALTER TABLE workflow_steps ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("contract TEXT", "failure_reason TEXT"):
        try:
            conn.execute(f"ALTER TABLE workflow_steps ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in (
        "kind TEXT NOT NULL DEFAULT 'task'",
        "on_reject_json TEXT NOT NULL DEFAULT '[]'",
        "max_rework INTEGER NOT NULL DEFAULT 2",
        "rework_count INTEGER NOT NULL DEFAULT 0",
        "feedback TEXT",
        "source_step_id TEXT",
        "slice_key TEXT",
        "plan_json TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE workflow_steps ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # 迁移：P0 可靠性——run 级超时 / 失败分类；P1-0——任务规划字段 / 草案关联任务
    for col in ("timeout_seconds INTEGER", "failure_class TEXT",
                "context_reset_count INTEGER NOT NULL DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("plan_first INTEGER NOT NULL DEFAULT 0", "planner TEXT"):
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE board_drafts ADD COLUMN source_task TEXT")
    except sqlite3.OperationalError:
        pass
    # 迁移：知识库元数据（scope/task/kind/tags/repo/confidence/updated_at/source/hash）
    for col in (
        "kind TEXT NOT NULL DEFAULT 'note'",
        "scope TEXT NOT NULL DEFAULT 'unclassified'",
        "task_id TEXT",
        "source_task TEXT",
        "tags_json TEXT NOT NULL DEFAULT '[]'",
        "repo TEXT",
        "module TEXT",
        "confidence REAL NOT NULL DEFAULT 0.5",
        "updated_at TEXT",
        "source TEXT NOT NULL DEFAULT 'agent'",
        "content_hash TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_notes_role_task_kind "
        "ON notes(role_slug, task_id, kind, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notes_role_scope_updated "
        "ON notes(role_slug, scope, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notes_role_repo ON notes(role_slug, repo)",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    _ensure_notes_fts(conn)
    _migrate_notes_metadata(conn)
    _migrate_legacy_note_scopes(conn)
    _ensure_notes_path_unique(conn)
    _rebuild_memory_fts_once(conn)
    _reset_legacy_token_usage_once(conn)
    conn.commit()


def _setting(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    except sqlite3.OperationalError:
        return None


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, datetime.now(timezone.utc).isoformat()),
    )


def _safe_task_segment(task_id: str) -> str:
    """与 services.knowledge.task_path_segment 保持一致的目录段算法。"""
    raw = str(task_id or "")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:64] or "task"
    if safe != raw:
        safe += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return safe


def _ensure_notes_fts(conn: sqlite3.Connection) -> bool:
    """创建独立 notes_fts（trigram 优先）+ 同步触发器；失败则回退 LIKE。"""
    table = (
        "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
        "note_id UNINDEXED, role_slug UNINDEXED, kind UNINDEXED, tags, content, "
        "tokenize='trigram')"
    )
    try:
        conn.execute(table)
    except sqlite3.OperationalError:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
                "note_id UNINDEXED, role_slug UNINDEXED, kind UNINDEXED, tags, content, "
                "tokenize='unicode61')")
        except sqlite3.OperationalError:
            for trigger in ("notes_fts_ai", "notes_fts_ad", "notes_fts_au"):
                try:
                    conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                except sqlite3.OperationalError:
                    pass
            return False
    for stmt in (
        "CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN "
        "INSERT INTO notes_fts(rowid, note_id, role_slug, kind, tags, content) "
        "VALUES (new.rowid, new.id, new.role_slug, new.kind, new.tags_json, new.content); END",
        "CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN "
        "DELETE FROM notes_fts WHERE rowid = old.rowid; END",
        "CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE ON notes BEGIN "
        "DELETE FROM notes_fts WHERE rowid = old.rowid; "
        "INSERT INTO notes_fts(rowid, note_id, role_slug, kind, tags, content) "
        "VALUES (new.rowid, new.id, new.role_slug, new.kind, new.tags_json, new.content); END",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return True


def _migrate_notes_metadata(conn: sqlite3.Connection) -> None:
    """一次性回填旧 notes 的 scope/task/kind/updated_at/hash，并重建 notes_fts。"""
    marker = _setting(conn, "schema.notes_metadata_v1")
    if marker:
        return
    tasks = conn.execute("SELECT id, tags_json, repo FROM tasks").fetchall()
    seg_to_task: dict[str, str | None] = {}
    task_meta: dict[str, dict] = {}
    for task in tasks:
        tid = str(task["id"])
        seg = _safe_task_segment(tid)
        if seg in seg_to_task and seg_to_task[seg] != tid:
            seg_to_task[seg] = None  # 目录名冲突：无法唯一恢复
        else:
            seg_to_task[seg] = tid
        task_meta[tid] = {"tags": task["tags_json"] or "[]", "repo": task["repo"]}
    rows = conn.execute(
        "SELECT rowid, path, content, created_at FROM notes").fetchall()
    for row in rows:
        path = str(row["path"] or "")
        content = str(row["content"] or "")
        values = {
            "updated_at": row["created_at"],
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        update = (
            "UPDATE notes SET updated_at=COALESCE(updated_at, ?), "
            "content_hash=COALESCE(content_hash, ?)"
        )
        params: list = [values["updated_at"], values["content_hash"]]
        if path.startswith("knowledge/"):
            parts = path.split("/")
            seg = parts[1] if len(parts) > 1 else ""
            tid = seg_to_task.get(seg)
            update += ", kind='task_snapshot', scope=?"
            params.append("task" if tid else "unclassified")
            # 目录名就是旧快照唯一可用的任务标识：匹配不到现存任务时把目录名
            # 保留在 source_task（scope 仍为 unclassified，不会自动注入）。
            update += ", task_id=?"
            params.append(tid)
            if tid:
                update += ", source_task=?, tags_json=?, repo=?"
                params.extend([tid,
                               task_meta.get(tid, {}).get("tags", "[]"),
                               task_meta.get(tid, {}).get("repo")])
            elif seg:
                update += ", source_task=?"
                params.append(seg)
        update += " WHERE rowid=?"
        params.append(row["rowid"])
        conn.execute(update, params)
    # 重建 notes_fts（触发器可能已同步过，最终以 notes 为准）
    try:
        conn.execute("DELETE FROM notes_fts")
        conn.execute(
            "INSERT INTO notes_fts(rowid, note_id, role_slug, kind, tags, content) "
            "SELECT rowid, id, role_slug, kind, tags_json, content FROM notes")
    except sqlite3.OperationalError:
        pass
    _set_setting(conn, "schema.notes_metadata_v1", "1")
    conn.commit()


def _migrate_legacy_note_scopes(conn: sqlite3.Connection) -> None:
    """旧 notes/ 笔记回填为 role scope（可跨任务复用），knowledge/ 快照保持原判。

    v1 迁移把未知 scope 的旧行留在 unclassified；那会让角色已有的普通笔记
    从检索/CIC 里消失。普通 notes 位于角色 home 内，默认就是角色可复用知识。
    """
    marker = _setting(conn, "schema.notes_metadata_v2")
    if marker:
        return
    conn.execute(
        "UPDATE notes SET scope='role' "
        "WHERE scope='unclassified' AND path NOT LIKE 'knowledge/%'")
    _set_setting(conn, "schema.notes_metadata_v2", "1")
    conn.commit()


def _ensure_notes_path_unique(conn: sqlite3.Connection) -> None:
    """确保 `(role_slug, path)` 唯一：一个笔记文件对应一行索引。

    - 旧库可能因 append 多次写入留下同 path 多行：按文件是真相合并内容，
      保留最新一行的元数据/id，再删除其余行；
    - 唯一索引是 write_note upsert 的冲突目标；
    - 旧的普通索引被唯一索引取代。
    """
    marker = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_notes_role_path_unique'").fetchone()
    unique_ok = marker is not None
    if not unique_ok:
        groups = conn.execute(
            "SELECT role_slug, path FROM notes GROUP BY role_slug, path "
            "HAVING COUNT(*) > 1").fetchall()
        for group in groups:
            rows = conn.execute(
                "SELECT rowid, content, created_at, updated_at FROM notes "
                "WHERE role_slug=? AND path=? "
                "ORDER BY COALESCE(created_at, ''), rowid",
                (group["role_slug"], group["path"])).fetchall()
            if not rows:
                continue
            keep = max(
                rows,
                key=lambda r: (str(r["updated_at"] or r["created_at"] or ""), r["rowid"]))
            parts: list[str] = []
            seen: set[str] = set()
            for r in rows:
                text = str(r["content"] or "")
                if not text:
                    continue
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                parts.append(text)
            merged = "\n".join(parts)
            conn.execute(
                "UPDATE notes SET content=?, content_hash=?, "
                "updated_at=COALESCE(updated_at, ?) WHERE rowid=?",
                (merged, hashlib.sha256(merged.encode("utf-8")).hexdigest(),
                 keep["updated_at"] or keep["created_at"], keep["rowid"]))
            conn.execute(
                "DELETE FROM notes WHERE role_slug=? AND path=? AND rowid<>?",
                (group["role_slug"], group["path"], keep["rowid"]))
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_role_path_unique "
                "ON notes(role_slug, path)")
            unique_ok = True
        except sqlite3.OperationalError:
            # 极端异常数据下保留非唯一索引；write_note 会退化为 DELETE+INSERT。
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notes_role_path "
                "ON notes(role_slug, path)")
    if unique_ok:
        try:
            conn.execute("DROP INDEX IF EXISTS idx_notes_role_path")
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _reset_legacy_token_usage_once(conn: sqlite3.Connection) -> None:
    """一次性清空旧版 token 记账。

    旧逻辑在每条 assistant 事件上记账：同一轮会重复累计完整 input/cache，且
    assistant.usage.output_tokens 恒为 0（真实输出在 result 事件里）。这些行
    无法恢复真实输出，清空后由 result 事件重新开始准确统计。
    """
    if _setting(conn, "schema.token_usage_reset_v1"):
        return
    conn.execute("DELETE FROM token_usage")
    _set_setting(conn, "schema.token_usage_reset_v1", "1")
    conn.commit()


def _notes_path_is_unique(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_notes_role_path_unique'").fetchone() is not None


def _rebuild_memory_fts_once(conn: sqlite3.Connection) -> None:
    """旧版 memory_fts 同时索引 notes；重建为仅 memories，避免跨任务泄漏。"""
    if _setting(conn, "schema.memory_fts_memories_only_v1"):
        return
    try:
        conn.execute("DROP TABLE IF EXISTS memory_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE memory_fts USING fts5("
            "role_slug, content, tokenize='porter')")
        conn.execute(
            "INSERT INTO memory_fts(role_slug, content) "
            "SELECT role_slug, content FROM memories")
        _set_setting(conn, "schema.memory_fts_memories_only_v1", "1")
    except sqlite3.OperationalError:
        # FTS5 不可用：保持 LIKE 回退，不阻断启动。
        pass
    conn.commit()
