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


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Runtime data root. Defaults to <aliceAgent>/data (sibling of dev/ and design.md).
DATA_DIR = Path(_env("ANVIL_DATA_DIR", str(ROOT_DIR.parent / "data"))).expanduser()

SQLITE_PATH = Path(_env("ANVIL_SQLITE_PATH", str(DATA_DIR / "anvil.db"))).expanduser()
AGENTS_DIR = Path(_env("ANVIL_AGENTS_DIR", str(DATA_DIR / "agents"))).expanduser()
REPOS_DIR = Path(_env("ANVIL_REPOS_DIR", str(DATA_DIR / "repos"))).expanduser()

# LangGraph 检查点独立文件（与 anvil.db 隔离，见 graph/__init__.py）。
GRAPH_DB_PATH = Path(_env("ANVIL_GRAPH_DB_PATH", str(DATA_DIR / "anvil-graph.db"))).expanduser()

# 后台调度器间隔（秒，design §6.1「每 5 秒扫描」）。
SCHEDULER_INTERVAL = float(_env("ANVIL_SCHEDULER_INTERVAL", "5.0"))

# 运行超时（秒）：monitor 对超过此时长仍 running 的 run 终止并标 failed（防 agent 卡死）。
RUN_TIMEOUT_SECONDS = float(_env("ANVIL_RUN_TIMEOUT", "600"))

# Utility LLM 调用（AI 生成画像/角色）经默认 CLI 运行时执行；同步请求的超时（秒）。
UTILITY_TIMEOUT_SECONDS = float(_env("ANVIL_UTILITY_TIMEOUT", "120"))

# 常驻角色运行时（persistent daemon）：默认随生产服务启用；测试环境（create_app 未开
# scheduler）不启用，保持 one-shot 假进程路径可测。
PERSISTENT_RUNTIME = _env("ANVIL_PERSISTENT_RUNTIME", "auto")  # auto | 1 | 0
ROLE_IDLE_RELEASE_MINUTES = float(_env("ANVIL_IDLE_RELEASE_MINUTES", "15"))
# 角色进程非零退出后，下一次唤醒是否丢弃旧 session 上下文新建会话。
RESET_SESSION_ON_FAILURE = _env("ANVIL_RESET_SESSION_ON_FAILURE", "1") == "1"
ROLE_SESSION_TOKEN_DAYS = int(_env("ANVIL_ROLE_TOKEN_DAYS", "30"))

# 角色未显式配置 permission_mode 时的默认值。本地单用户环境用 bypassPermissions；
# --print 模式下没有交互授权通道，default 会让 agent 停在「等待授权」。
DEFAULT_PERMISSION_MODE = _env("ANVIL_DEFAULT_PERMISSION_MODE", "bypassPermissions")

# CIC（Constructed Initial Context）：空闲释放后若会话上下文超过阈值，不再 --resume，
# 而是新建会话并注入 5 块构造上下文（MEMORY 头/最近消息/最近动作/唤醒）。
# 0 = 自动：按模型上下文窗口 × CIC_RESET_RATIO；>0 = 绝对阈值覆盖。
CIC_THRESHOLD_TOKENS = int(_env("ANVIL_CIC_THRESHOLD_TOKENS", "0"))
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
# 同一 run 因上下文溢出自动换会话的最大次数；超过则交给步骤自动恢复队列
# （指数退避重排队，不 @human）。主动水位轮换不计入此预算。
CONTEXT_RESET_MAX_RETRIES = int(_env("ANVIL_CONTEXT_RESET_MAX_RETRIES", "3"))
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
AUTO_RECOVER_BACKOFF_SECONDS = float(_env("ANVIL_AUTO_RECOVER_BACKOFF_SECONDS", "120"))
AUTO_RECOVER_MAX_BACKOFF_SECONDS = float(_env("ANVIL_AUTO_RECOVER_MAX_BACKOFF_SECONDS", "1800"))

# Agent 自行扩展团队（Raft 风格）：继承创建者 runtime；受每日配额与总角色数限制。
AGENT_ROLE_EXPAND_DAILY = int(_env("ANVIL_AGENT_ROLE_EXPAND_DAILY", "3"))
MAX_ROLES = int(_env("ANVIL_MAX_ROLES", "20"))

# 自动派发（默认关）：todo 任务是否由调度器自动 wake 第一个角色。关闭时派发完全由「指派并执行」手动决定。
AUTO_DISPATCH = _env("ANVIL_AUTO_DISPATCH", "0") == "1"

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


def ensure_dirs() -> None:
    for d in (DATA_DIR, USER_RUNTIMES_DIR, AGENTS_DIR, REPOS_DIR, SQLITE_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
