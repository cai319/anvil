"""SQLite state layer.

Design stance (§1.5): files are the source of truth; SQLite stores state,
events, and the search index. Each app instance owns one connection (see
`main.create_app`), so tests get isolated per-`create_app` DBs.
"""
from __future__ import annotations

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
    nudge_count      INTEGER NOT NULL DEFAULT 0,    -- 停滞唤醒次数（超过上限判失败）
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

CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    role_slug    TEXT,
    task_id      TEXT,
    action       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

-- 角色常驻运行时会话（persistent daemon）：每角色一个长期 CLI 进程，
-- 空闲释放后保留 session_id，下次唤醒用 --resume 恢复同一上下文。
CREATE TABLE IF NOT EXISTS role_sessions (
    role_slug      TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    token          TEXT,            -- 角色级会话令牌（task_id=NULL）
    pid            INTEGER,
    status         TEXT NOT NULL DEFAULT 'idle',  -- starting|idle|busy|dead
    busy_since     TEXT,
    last_active_at TEXT,
    last_error     TEXT,
    updated_at     TEXT NOT NULL
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
    created_at TEXT NOT NULL
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
    for col in ("last_nudge_at TEXT", "nudge_count INTEGER NOT NULL DEFAULT 0"):
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
    for col in ("timeout_seconds INTEGER", "failure_class TEXT"):
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
    conn.commit()
