"""Anvil backend configuration.

Values come from environment variables with sensible defaults. Data directories
(agents/repos/runtimes) are kept separate from the source tree per design —
"files are the source of truth, SQLite holds state".
"""
from __future__ import annotations

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

# 常驻角色运行时（persistent daemon）：默认随生产服务启用；测试环境（create_app 未开
# scheduler）不启用，保持 one-shot 假进程路径可测。
PERSISTENT_RUNTIME = _env("ANVIL_PERSISTENT_RUNTIME", "auto")  # auto | 1 | 0
ROLE_IDLE_RELEASE_MINUTES = float(_env("ANVIL_IDLE_RELEASE_MINUTES", "15"))
ROLE_SESSION_TOKEN_DAYS = int(_env("ANVIL_ROLE_TOKEN_DAYS", "30"))

# 角色未显式配置 permission_mode 时的默认值。本地单用户环境用 bypassPermissions；
# --print 模式下没有交互授权通道，default 会让 agent 停在「等待授权」。
DEFAULT_PERMISSION_MODE = _env("ANVIL_DEFAULT_PERMISSION_MODE", "bypassPermissions")

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
