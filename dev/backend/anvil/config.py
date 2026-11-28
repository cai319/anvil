"""Anvil backend configuration.

Values come from environment variables with sensible defaults. Data directories
(agents/repos/runtimes) are kept separate from the source tree per design —
"files are the source of truth, SQLite holds state".
"""
from __future__ import annotations

import json
import os
import platform as _platform
import sys
from pathlib import Path

# Source tree root: .../dev (backend/cli/frontend live here).
ROOT_DIR = Path(__file__).resolve().parents[2]


def _frontend_dist() -> Path:
    # 打包后前端静态随 PyInstaller 数据目录分发；dev 时用源码树 build 产物。
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "frontend"
    return ROOT_DIR / "frontend" / "dist"


FRONTEND_DIST = _frontend_dist()


def _default_log_dir() -> Path:
    """源码：<项目根>/log；打包：<anvil-server.exe 所在目录>/log。"""
    if getattr(sys, "_MEIPASS", None) or getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "log"
    return ROOT_DIR.parent / "log"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Runtime data root. Defaults to <aliceAgent>/data (sibling of dev/ and design.md).
DATA_DIR = Path(_env("ANVIL_DATA_DIR", str(ROOT_DIR.parent / "data"))).expanduser()

SQLITE_PATH = Path(_env("ANVIL_SQLITE_PATH", str(DATA_DIR / "anvil.db"))).expanduser()
# 运行日志：默认项目根 log/，Windows 源码模式即 <项目根>\log\anvil.log。
LOG_DIR = Path(_env("ANVIL_LOG_DIR", str(_default_log_dir()))).expanduser()
LOG_LEVEL = _env("ANVIL_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(_env("ANVIL_LOG_MAX_BYTES", str(20 * 1024 * 1024)))
LOG_BACKUPS = int(_env("ANVIL_LOG_BACKUPS", "10"))
AGENTS_DIR = Path(_env("ANVIL_AGENTS_DIR", str(DATA_DIR / "agents"))).expanduser()
REPOS_DIR = Path(_env("ANVIL_REPOS_DIR", str(DATA_DIR / "repos"))).expanduser()
# 任务数据存储（与知识库完全分开）。task_workspaces 表 + 每任务目录。
TASK_WORKSPACES_DIR = Path(
    _env("ANVIL_TASK_WORKSPACES_DIR", str(DATA_DIR / "task-workspaces"))).expanduser()
# 主题知识库（跨任务）与 Channel 知识库的文件根目录。
KNOWLEDGE_REPOS_DIR = Path(
    _env("ANVIL_KNOWLEDGE_REPOS_DIR", str(DATA_DIR / "knowledge-repos"))).expanduser()
CHANNELS_DIR = Path(_env("ANVIL_CHANNELS_DIR", str(DATA_DIR / "channels"))).expanduser()
# v1.50：隐藏回放集 / 运行时提案 / 导入隔离目录。
ROLE_EVAL_DIR = Path(_env("ANVIL_ROLE_EVAL_DIR", str(DATA_DIR / "eval"))).expanduser()
RUNTIME_PROPOSALS_DIR = Path(
    _env("ANVIL_RUNTIME_PROPOSALS_DIR", str(DATA_DIR / "runtime-proposals"))).expanduser()
ROLE_IMPORTS_DIR = Path(
    _env("ANVIL_ROLE_IMPORTS_DIR", str(DATA_DIR / "imports"))).expanduser()

# LangGraph 检查点独立文件（与 anvil.db 隔离，见 graph/__init__.py）。
GRAPH_DB_PATH = Path(_env("ANVIL_GRAPH_DB_PATH", str(DATA_DIR / "anvil-graph.db"))).expanduser()

# 后台调度器间隔（秒，design §6.1「每 5 秒扫描」）。
SCHEDULER_INTERVAL = float(_env("ANVIL_SCHEDULER_INTERVAL", "5.0"))

# 运行超时（秒）：monitor 对超过此时长仍 running 的 run 终止并标 failed（防 agent 卡死）。
RUN_TIMEOUT_SECONDS = float(_env("ANVIL_RUN_TIMEOUT", "600"))
# 资源租约：默认与 run 超时对齐；持有者需在到期前续租。
LEASE_TTL_SECONDS = float(_env("ANVIL_LEASE_TTL", str(RUN_TIMEOUT_SECONDS)))

# workflow 生命周期锁：task 级互斥，覆盖 advance/apply/finalize/create 等跨进程写路径。
WORKFLOW_LOCK_TTL_SECONDS = float(
    _env("ANVIL_WORKFLOW_LOCK_TTL", "120"))
# 修订草案无主/过期清理（小时）；0 表示只清理 base workflow 失效的草案。
REVISION_DRAFT_TTL_HOURS = float(
    _env("ANVIL_REVISION_DRAFT_TTL_HOURS", "168"))

# Utility LLM 调用（AI 生成画像/角色）经默认 CLI 运行时执行；同步请求的超时（秒）。
UTILITY_TIMEOUT_SECONDS = float(_env("ANVIL_UTILITY_TIMEOUT", "120"))

# 问题板 → 外部问题跟踪服务器。测试只发空 list，给较短超时；正式提交可能批量较大。
# connect 单独限制：DNS/TCP 建连慢时不必耗掉整个读超时，报错也能区分阶段。
ISSUE_TRACKER_TIMEOUT_SECONDS = float(_env("ANVIL_ISSUE_TRACKER_TIMEOUT", "30"))
ISSUE_TRACKER_TEST_TIMEOUT_SECONDS = float(
    _env("ANVIL_ISSUE_TRACKER_TEST_TIMEOUT", "10"))
ISSUE_TRACKER_CONNECT_TIMEOUT_SECONDS = float(
    _env("ANVIL_ISSUE_TRACKER_CONNECT_TIMEOUT", "5"))
# 外部字段（除 description_md）压缩：超过 512 字符时用 LLM 精简；失败则本地截断兜底。
# 对端 DB 的 string 字段上限按 512 处理，留足安全余量。
ISSUE_TRACKER_FIELD_MAX_CHARS = int(
    _env("ANVIL_ISSUE_TRACKER_FIELD_MAX_CHARS", "512"))
ISSUE_TRACKER_COMPACT_TIMEOUT_SECONDS = float(
    _env("ANVIL_ISSUE_TRACKER_COMPACT_TIMEOUT", "60"))

# 常驻角色运行时（persistent daemon）：默认随生产服务启用；测试环境（create_app 未开
# scheduler）不启用，保持 one-shot 假进程路径可测。
PERSISTENT_RUNTIME = _env("ANVIL_PERSISTENT_RUNTIME", "auto")  # auto | 1 | 0
ROLE_IDLE_RELEASE_MINUTES = float(_env("ANVIL_IDLE_RELEASE_MINUTES", "15"))
# 角色进程非零退出后，下一次唤醒是否丢弃旧 session 上下文新建会话。
RESET_SESSION_ON_FAILURE = _env("ANVIL_RESET_SESSION_ON_FAILURE", "1") == "1"
ROLE_SESSION_TOKEN_DAYS = int(_env("ANVIL_ROLE_TOKEN_DAYS", "30"))

# ---- v1.60 执行沙箱 ---------------------------------------------------------
# enforce：按平台首选后端（macOS Seatbelt / Windows 命名 Job）；host-unsafe：显式关闭沙箱。
SANDBOX_MODE = _env("ANVIL_SANDBOX", "enforce").strip().lower()
if SANDBOX_MODE not in ("enforce", "host-unsafe"):
    raise RuntimeError("ANVIL_SANDBOX must be 'enforce' or 'host-unsafe'")
SANDBOX_ENABLED = _env("ANVIL_SANDBOX_ENABLED", "1") == "1"
SANDBOX_STANDARD_TIMEOUT = float(_env("ANVIL_SANDBOX_STANDARD_TIMEOUT", "3600"))
SANDBOX_STANDARD_RSS_MIB = int(_env("ANVIL_SANDBOX_STANDARD_RSS_MIB", "4096"))
SANDBOX_STANDARD_PIDS = int(_env("ANVIL_SANDBOX_STANDARD_PIDS", "256"))
SANDBOX_STANDARD_DISK_MIB = int(_env("ANVIL_SANDBOX_STANDARD_DISK_MIB", "20480"))
# 沙箱内 HOME/CLI 配置放在 cwd 下的这个目录；必须从 diff/提交中排除。
SANDBOX_HOME_DIRNAME = ".anvil/home"
SANDBOX_INSTANCES_DIR = DATA_DIR / "sandbox" / "instances"
SANDBOX_JOBS_DIR = DATA_DIR / "sandbox" / "jobs"
SANDBOX_IMAGE = _env("ANVIL_SANDBOX_IMAGE", "anvil-toolchain:1.60.0")
SANDBOX_JOB_POLL_SECONDS = float(_env("ANVIL_SANDBOX_JOB_POLL_SECONDS", "2"))

# 角色未显式配置 permission_mode 时的默认值。本地单用户环境用 bypassPermissions；
# --print 模式下没有交互授权通道，default 会让 agent 停在「等待授权」。
DEFAULT_PERMISSION_MODE = _env("ANVIL_DEFAULT_PERMISSION_MODE", "bypassPermissions")

# CIC（Constructed Initial Context）：空闲释放后若会话上下文超过阈值，不再 --resume，
# 而是新建会话并注入 5 块构造上下文（MEMORY 头/最近消息/最近动作/唤醒）。
# 0 = 自动：按模型上下文窗口 × CIC_RESET_RATIO；>0 = 绝对阈值覆盖。
CIC_THRESHOLD_TOKENS = int(_env("ANVIL_CIC_THRESHOLD_TOKENS", "0"))
# 冷唤醒 CIC 首屏字符预算（Raft 借鉴：只注入有限摘要，其余走拉模式）。
CIC_COLD_BUDGET_CHARS = int(_env("ANVIL_CIC_COLD_BUDGET_CHARS", "32000"))
CIC_RESET_RATIO = float(_env("ANVIL_CIC_RESET_RATIO", "0.8"))
# 模型上下文窗口：优先 ANVIL_MODEL_CONTEXT_WINDOWS（JSON: {"model-id": 1000000}），
# 否则按模型名中的 1m/200k/128k 推断，最后退回默认值。
DEFAULT_CONTEXT_WINDOW = int(_env("ANVIL_DEFAULT_CONTEXT_WINDOW", "200000"))
# 全局保守下限：若角色可能路由到更小窗口的模型，用它兜底（0=不设）。
CONTEXT_WINDOW_FLOOR = int(_env("ANVIL_CONTEXT_WINDOW_FLOOR", "0"))
try:
    _model_windows_raw = json.loads(_env("ANVIL_MODEL_CONTEXT_WINDOWS", "{}") or "{}")
except json.JSONDecodeError:
    _model_windows_raw = {}
MODEL_CONTEXT_WINDOWS: dict[str, int] = {}
for _k, _v in _model_windows_raw.items():
    try:
        _n = int(_v)
    except (TypeError, ValueError):
        continue
    if _n > 0:
        MODEL_CONTEXT_WINDOWS[str(_k)] = _n
# 唤醒时向角色提示“上下文将满”的比例；达到该比例时角色可自行决定是否 reset-context。
CONTEXT_HINT_RATIO = float(_env("ANVIL_CONTEXT_HINT_RATIO", "0.5"))
# 单轮工具循环内的硬保护：assistant 实测上下文达到该比例时，Anvil 主动终止本次会话，
# 下次唤醒用 CIC 新会话重投当前 run（0=关闭）。之所以需要它，是因为一次唤醒可以连续
# 跑几百次工具调用（实测 1 小时 714 次），模型/CLI 的自动压缩只在轮次边界生效，
# 期间上下文会一路涨到 API 的“prompt is too long”。
CONTEXT_HARD_RESET_RATIO = float(_env("ANVIL_CONTEXT_HARD_RESET_RATIO", "0.9"))
# 给单次响应预留的 output 额度；API 的上下文上限是 input+max_tokens，
# 只按窗口算会留下正好一个 max_tokens 的溢出空间。
CONTEXT_OUTPUT_RESERVE_TOKENS = int(_env("ANVIL_CONTEXT_OUTPUT_RESERVE", "32000"))
# 单次 API 请求体上限（按图片/附件 base64 字符数累计估算）。图片/附件不计 token，
# 可能先撞上 “request too large (max 32mb)”；超过该值时主动换新会话。
# 0 = 关闭。默认 24MB，给 JSON/文本留 8MB 余量。
PAYLOAD_HARD_RESET_CHARS = int(_env("ANVIL_PAYLOAD_HARD_RESET_CHARS", "24000000"))
# 附件预算提示：达到该字符数时在唤醒提示里提醒角色“不要再携带大图/大文件”，
# 先固化结论、必要时主动 reset-context。默认 8MB（硬阈值的 1/3）。
PAYLOAD_HINT_CHARS = int(_env("ANVIL_PAYLOAD_HINT_CHARS", "8000000"))
# 同一 run 因上下文溢出自动换会话的最大次数；超过则交给步骤自动恢复队列
# （指数退避重排队，不 @human）。主动水位轮换不计入此预算。
CONTEXT_RESET_MAX_RETRIES = int(_env("ANVIL_CONTEXT_RESET_MAX_RETRIES", "3"))
# 连续上下文轮换（主动水位/硬阈值）上限：CIC 本身超过硬阈值时防止无限
# kill→CIC→spawn。正常轮换不计入失败预算，但连续超过该值转技术性失败。
CONTEXT_ROTATION_MAX_CONSECUTIVE = int(
    _env("ANVIL_CONTEXT_ROTATION_MAX_CONSECUTIVE", "5"))
# 常驻进程已死亡但 run 仍卡在 running 的宽限期（秒）后强制转为 suspended，
# 让步骤看门狗可以续跑/判失败，避免永久占用该角色。
RUN_DEAD_GRACE_SECONDS = float(_env("ANVIL_RUN_DEAD_GRACE_SECONDS", "60"))
# run 卡在 projecting 的看门狗：编排器在 project→spawn 之间崩溃时，定期重新投递。
RUN_PHASE_STALL_SECONDS = float(_env("ANVIL_RUN_PHASE_STALL_SECONDS", "60"))

# 数据库日常备份：启动/调度器每天做一次 SQLite 在线备份，保留最近 N 天。
BACKUP_ENABLED = _env("ANVIL_BACKUP", "1") != "0"
BACKUP_KEEP_DAYS = int(_env("ANVIL_BACKUP_KEEP_DAYS", "7"))

# Cleaner：本地检查角色 MEMORY.md 体积（零 token），超限时给该角色排一次自修剪提醒（每天至多一次）。
CLEANER_ENABLED = _env("ANVIL_CLEANER", "1") != "0"
CLEANER_MAX_BYTES = int(_env("ANVIL_CLEANER_MAX_BYTES", "12000"))

# 工作流步骤停滞看门狗：步骤 run 挂起且无活动超过阈值 → 唤醒角色续跑；超过次数后交给自动恢复。
STEP_NUDGE_SECONDS = float(_env("ANVIL_STEP_NUDGE_SECONDS", "600"))
STEP_MAX_NUDGES = int(_env("ANVIL_STEP_MAX_NUDGES", "3"))

# 自动可靠性恢复：进程崩溃/超时/上下文溢出/步骤停滞等技术性失败，先按指数退避自动
# 重排队重试，不 @human（人类可能不在场）；超过预算才判失败并留非阻塞通知。
# 人工确认点（评审/gate/审批）不受影响，仍走 HITL。
AUTO_RECOVER_ENABLED = _env("ANVIL_AUTO_RECOVER", "1") != "0"
AUTO_RECOVER_MAX_ATTEMPTS = int(_env("ANVIL_AUTO_RECOVER_MAX_ATTEMPTS", "10"))

# 派遣子任务：最多沿 source_task_id 链嵌套几层；Agent 之间 mention 自动派遣默认关闭。
# 人类 @ 与显式 context_mode=dispatch 不受此开关限制。
DISPATCH_MAX_DEPTH = int(_env("ANVIL_DISPATCH_MAX_DEPTH", "2"))
DISPATCH_AUTO_FROM_MENTION = _env(
    "ANVIL_DISPATCH_AUTO_FROM_MENTION", "0").strip().lower() in ("1", "true", "yes", "on")
DISPATCH_RETURN_MAX_ATTEMPTS = int(
    _env("ANVIL_DISPATCH_RETURN_MAX_ATTEMPTS", "5"))
DISPATCH_RETURN_WAIT_SECONDS = float(
    _env("ANVIL_DISPATCH_RETURN_WAIT_SECONDS", "900"))
# 父 step 等待委派负责人干活时，看门狗最多静默多久（秒）；超时后恢复 nudge，
# 由父 step 明确上报/等待人工处理，避免无限期静默。
DELEGATION_WAIT_SECONDS = float(
    _env("ANVIL_DELEGATION_WAIT_SECONDS", "21600"))
# 唤醒队列在 processing 状态的租约：调度器崩溃后超过该时长重新排队。
WAKE_REQUEST_LEASE_SECONDS = float(
    _env("ANVIL_WAKE_REQUEST_LEASE_SECONDS", "120"))
# 目标仍 running/pump 失败时的最大重试次数；超过后保留 wake_note 并放弃本次请求。
WAKE_REQUEST_MAX_ATTEMPTS = int(
    _env("ANVIL_WAKE_REQUEST_MAX_ATTEMPTS", "24"))
CONTEXT_RETURN_MAX_ATTEMPTS = int(
    _env("ANVIL_CONTEXT_RETURN_MAX_ATTEMPTS", "5"))
CONTEXT_RETURN_BACKOFF_SECONDS = float(
    _env("ANVIL_CONTEXT_RETURN_BACKOFF_SECONDS", "30"))
# 回切标记最多阻塞多久（秒）；超过后 resolver/看门狗都不再等待。
CONTEXT_RETURN_MAX_WAIT_SECONDS = float(
    _env("ANVIL_CONTEXT_RETURN_MAX_WAIT_SECONDS", "900"))
AUTO_RECOVER_BACKOFF_SECONDS = float(_env("ANVIL_AUTO_RECOVER_BACKOFF_SECONDS", "120"))
AUTO_RECOVER_MAX_BACKOFF_SECONDS = float(_env("ANVIL_AUTO_RECOVER_MAX_BACKOFF_SECONDS", "1800"))

# 非 workflow 的普通任务/回复/派遣 run 失败后的自动恢复。
RUN_AUTO_RECOVER_ENABLED = _env("ANVIL_RUN_AUTO_RECOVER", "1") != "0"
RUN_AUTO_RECOVER_MAX_ATTEMPTS = int(
    _env("ANVIL_RUN_AUTO_RECOVER_MAX_ATTEMPTS", "3"))
RUN_AUTO_RECOVER_MAX_AGE_SECONDS = float(
    _env("ANVIL_RUN_AUTO_RECOVER_MAX_AGE_SECONDS", "86400"))

# Agent 自行扩展团队（Raft 风格）：继承创建者 runtime；受每日配额与总角色数限制。
AGENT_ROLE_EXPAND_DAILY = int(_env("ANVIL_AGENT_ROLE_EXPAND_DAILY", "3"))
MAX_ROLES = int(_env("ANVIL_MAX_ROLES", "20"))

# 自动派发（默认关）：todo 任务是否由调度器自动 wake 第一个角色。关闭时派发完全由「指派并执行」手动决定。
AUTO_DISPATCH = _env("ANVIL_AUTO_DISPATCH", "0") == "1"

# 任务 → 知识 repo 自动绑定（2026-10-24）。
# 执行仓库仍是单值 tasks.repo；这里是多值 knowledge repos，仅影响 lessons/notes 检索。
KNOWLEDGE_REPOS_ENABLED = _env("ANVIL_KNOWLEDGE_REPOS", "1") != "0"
KNOWLEDGE_BINDING_MODE = _env("ANVIL_KNOWLEDGE_BINDING_MODE", "auto")  # off|suggest|auto
KNOWLEDGE_BINDING_HIGH = float(_env("ANVIL_KNOWLEDGE_BINDING_HIGH", "0.62"))
KNOWLEDGE_BINDING_SUGGEST = float(_env("ANVIL_KNOWLEDGE_BINDING_SUGGEST", "0.38"))
KNOWLEDGE_BINDING_MAX_REPOS = int(_env("ANVIL_KNOWLEDGE_BINDING_MAX_REPOS", "3"))
KNOWLEDGE_BINDING_GAP = float(_env("ANVIL_KNOWLEDGE_BINDING_GAP", "0.15"))
# Phase 1：主题知识库自动识别/创建；<50% 时在 task workspace 内建任务级知识库。
KNOWLEDGE_REPO_CREATE_MODE = _env("ANVIL_KNOWLEDGE_REPO_CREATE_MODE", "auto")  # auto|suggest|off
KNOWLEDGE_REPO_CREATE_THRESHOLD = float(
    _env("ANVIL_KNOWLEDGE_REPO_CREATE_THRESHOLD", "0.5"))
# Phase 2：任务执行经验自动草稿（proposed，人工批准）；默认开启，可在「工具」页关闭。
EXPERIENCE_CAPTURE_ENABLED = _env("ANVIL_EXPERIENCE_CAPTURE", "1") == "1"
EXPERIENCE_CAPTURE_LLM = _env("ANVIL_EXPERIENCE_CAPTURE_LLM", "0") == "1"
# 任务完成后的知识收割：自动生成 when(task done) 提醒，触发 Cindy 收割任务/主题/频道知识。
KNOWLEDGE_HARVEST_REMINDER_ENABLED = _env(
    "ANVIL_KNOWLEDGE_HARVEST_REMINDER", "1") != "0"
# 知识质量：人工按需 LLM 清洗（默认关闭；不自动批准）。
KNOWLEDGE_LLM_CLEANING_ENABLED = _env(
    "ANVIL_KNOWLEDGE_LLM_CLEANING", "0") == "1"
# v1.51 Cindy 规划前澄清：首次规划阶段先澄清再规划；默认开启。
CINDY_CLARIFICATION_ENABLED = _env("ANVIL_CINDY_CLARIFICATION", "1") != "0"

# v1.50 角色 RSI：默认全关，按角色试点后开启（见 docs/v1.50-role-rsi.md）。
ROLE_RSI_ENABLED = _env("ANVIL_ROLE_RSI", "0") == "1"
ROLE_RSI_AUTONOMOUS_KNOWLEDGE = _env(
    "ANVIL_ROLE_RSI_AUTONOMOUS_KNOWLEDGE", "1") == "1"
ROLE_RSI_REFLECTION = _env("ANVIL_ROLE_RSI_REFLECTION", "1") == "1"
ROLE_RSI_EVALUATION = _env("ANVIL_ROLE_RSI_EVALUATION", "0") == "1"
ROLE_RSI_AUTO_ROLLBACK = _env("ANVIL_ROLE_RSI_AUTO_ROLLBACK", "1") == "1"
ROLE_RSI_RUBRIC_JUDGE = _env("ANVIL_ROLE_RSI_RUBRIC_JUDGE", "0") == "1"
ROLE_RSI_LOCK_STRATEGY = _env("ANVIL_ROLE_RSI_LOCK_STRATEGY", "0") == "1"
ROLE_RSI_REPLAY_L2 = _env("ANVIL_ROLE_RSI_REPLAY_L2", "0") == "1"
ROLE_RSI_MAX_GLOBAL_ACTIVE_RUNS = int(
    _env("ANVIL_ROLE_RSI_MAX_GLOBAL_ACTIVE_RUNS", "2"))
RUNTIME_PROPOSALS_ENABLED = _env("ANVIL_RUNTIME_PROPOSALS", "1") == "1"
RUNTIME_PROPOSAL_DETECTOR = _env("ANVIL_RUNTIME_PROPOSAL_DETECTOR", "0") == "1"

# 版本存档 / 导出导入（v1.50）。
ROLE_VERSION_COALESCE_SECONDS = int(
    _env("ANVIL_ROLE_VERSION_COALESCE_SECONDS", "60"))
ROLE_VERSION_KEEP_MANUAL = int(_env("ANVIL_ROLE_VERSION_KEEP_MANUAL", "30"))
ROLE_VERSION_KEEP_RSI = int(_env("ANVIL_ROLE_VERSION_KEEP_RSI", "50"))
ROLE_VERSION_KEEP_CANDIDATE = int(
    _env("ANVIL_ROLE_VERSION_KEEP_CANDIDATE", "20"))
ROLE_VERSION_REJECTED_DAYS = int(
    _env("ANVIL_ROLE_VERSION_REJECTED_DAYS", "180"))
ROLE_EVAL_ENCRYPT_HIDDEN = _env(
    "ANVIL_ROLE_EVAL_ENCRYPT_HIDDEN", "0") == "1"

# Phase 3：检索最低分 + 单任务上下文预算（0=关闭，按模型窗口 CIC 阈值）。
# 基线校准：BM25/元数据重排后的分数偏低；0.05 只过滤明显噪声，保留精确关键词命中。
KNOWLEDGE_MIN_SCORE = float(_env("ANVIL_KNOWLEDGE_MIN_SCORE", "0.05"))
TASK_CONTEXT_BUDGET_TOKENS = int(
    _env("ANVIL_TASK_CONTEXT_BUDGET_TOKENS", "200000"))
KNOWLEDGE_CATALOG_TTL = float(_env("ANVIL_KNOWLEDGE_CATALOG_TTL", "30"))
KNOWLEDGE_CATALOG_MAX_REPOS = int(_env("ANVIL_KNOWLEDGE_CATALOG_MAX_REPOS", "200"))
# tagless：无 tags 的任务走 Cindy；always：所有新任务都走；off：只做确定性匹配。
KNOWLEDGE_CINDY_MODE = _env("ANVIL_KNOWLEDGE_CINDY_MODE", "tagless")
# 无任务上下文（reminder/直聊）的知识范围：all（现状）或 shared。
KNOWLEDGE_TASKLESS_SCOPE = _env("ANVIL_KNOWLEDGE_TASKLESS_SCOPE", "all")

# v1.30 频道/任务知识作用域。频道知识默认关闭，先按试点指标验证。
CHANNEL_KNOWLEDGE_ENABLED = _env("ANVIL_CHANNEL_KNOWLEDGE", "0") == "1"
CHANNEL_KNOWLEDGE_AGENT_AUTO_APPROVE = _env(
    "ANVIL_CHANNEL_KNOWLEDGE_AGENT_AUTO_APPROVE", "0") == "1"
CHANNEL_KNOWLEDGE_CIC_CHARS = int(_env("ANVIL_CHANNEL_KNOWLEDGE_CIC_CHARS", "1200"))
CHANNEL_KNOWLEDGE_PLANNING_CHARS = int(
    _env("ANVIL_CHANNEL_KNOWLEDGE_PLANNING_CHARS", "2200"))
TASK_KNOWLEDGE_ENABLED = _env("ANVIL_TASK_KNOWLEDGE", "1") != "0"

# Phase 0：任务工作空间 / 延迟投影 / 上下文清单 / 角色准则。
TASK_WORKSPACE_ENABLED = _env("ANVIL_TASK_WORKSPACE", "1") != "0"
TASK_WORKSPACE_RETENTION_DAYS = int(
    _env("ANVIL_TASK_WORKSPACE_RETENTION_DAYS", "30"))
TASK_WORKSPACE_CLEANUP_ENABLED = _env(
    "ANVIL_TASK_WORKSPACE_CLEANUP", "1") != "0"
DEFERRED_PROJECTION = _env("ANVIL_DEFERRED_PROJECTION", "1") != "0"
CONTEXT_MANIFEST_ENABLED = _env("ANVIL_CONTEXT_MANIFEST", "1") != "0"
MEMORY_PRINCIPLES_MAX_CHARS = int(
    _env("ANVIL_MEMORY_PRINCIPLES_MAX_CHARS", "2048"))

# Runtime definitions (design §2.6 存储分层)：
#   - 内置（随包分发）：backend/anvil/runtimes/*.yaml
#   - 用户自定义/覆盖：%APPDATA%/Anvil/runtimes/（macOS 用 ~/.config/Anvil/runtimes/）
def _package_runtimes_dir() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "anvil" / "runtimes"
    return Path(__file__).resolve().parent / "runtimes"


PACKAGE_RUNTIMES_DIR = _package_runtimes_dir()


def _default_user_runtimes_dir() -> Path:
    if _platform.system() == "Windows":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "Anvil" / "runtimes"
    return Path("~/.config/Anvil/runtimes").expanduser()


USER_RUNTIMES_DIR = Path(_env("ANVIL_USER_RUNTIMES_DIR", str(_default_user_runtimes_dir()))).expanduser()

# M1 first-class runtime (design: "先只支持最常用的一个").
DEFAULT_RUNTIME = _env("ANVIL_DEFAULT_RUNTIME", "claude-code")

HOST = _env("ANVIL_HOST", "127.0.0.1")
PORT = int(_env("ANVIL_PORT", "7317"))

# 鉴权轻校验 token（P3#13）：设置后要求 X-Anvil-Session 匹配；留空则仅要求存在头（M1）。
AUTH_TOKEN = _env("ANVIL_AUTH_TOKEN", "")
AUTH_TOKEN_FILE = Path(
    _env("ANVIL_AUTH_TOKEN_FILE", str(DATA_DIR / "auth-token"))).expanduser()
_WEAK_AUTH_TOKENS = {"anvil", "anvil-web", "test", "password", "token"}


def resolve_auth_token(path: Path | None = None) -> str:
    """解析人类共享 token；未配置时生成安装级随机 secret（0600）。

    Agent 子进程的环境会在 daemon/平台层被剥离该 token，不能用它冒充 human。
    """
    import secrets

    explicit = str(AUTH_TOKEN or "").strip()
    if explicit:
        if explicit.lower() in _WEAK_AUTH_TOKENS or len(explicit) < 16:
            raise RuntimeError(
                "ANVIL_AUTH_TOKEN is too weak; use at least 16 random characters")
        return explicit
    token_path = Path(
        path or os.environ.get("ANVIL_AUTH_TOKEN_FILE") or (DATA_DIR / "auth-token"))
    try:
        existing = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing and existing.lower() not in _WEAK_AUTH_TOKENS and len(existing) >= 16:
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
        return existing
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n", encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return token


def ensure_dirs() -> None:
    for d in (DATA_DIR, USER_RUNTIMES_DIR, AGENTS_DIR, REPOS_DIR,
              TASK_WORKSPACES_DIR, KNOWLEDGE_REPOS_DIR, CHANNELS_DIR,
              ROLE_EVAL_DIR, RUNTIME_PROPOSALS_DIR, ROLE_IMPORTS_DIR,
              SANDBOX_INSTANCES_DIR, SANDBOX_JOBS_DIR, SQLITE_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
    # 日志目录不可写时由 setup_logging 回退到 DATA_DIR/log，不能阻断服务启动。
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
