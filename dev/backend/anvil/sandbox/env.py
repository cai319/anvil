"""沙箱内 HOME/TMP/缓存的重定向。

目标：agent 的会话、CLI 配置与缓存只落在任务工作区（``.anvil/home``）与实例
临时目录，不写宿主 ``~/.claude`` / ``~/.config``。模型凭证仍走继承的环境变量。
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import config

# 宿主 Claude Code/agent 会话变量：描述的是宿主会话，不是 Anvil 角色运行时。
# 带进沙箱会指向写范围之外（例如 /private/tmp/claude-<uid>）或改变嵌套会话行为。
HOST_SESSION_ENV = frozenset({
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_CHILD_NOTES_DIR",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_SHELL_PREFIX",
    "CLAUDE_PID",
    "XDG_RUNTIME_DIR",
})

# Anvil 自己注入、必须保留的 Claude Code 运行变量。
KEEP_CLAUDE_CODE = frozenset({
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    "CLAUDE_EFFORT",
})

HOST_PATH_PREFIXES = ("/Users/", "/private/", "/var/", "/tmp/")


def host_session_env(env: dict[str, str]) -> list[str]:
    """返回环境里仍存在的宿主会话标记（doctor 用于检测从 agent shell 启动）。"""
    return sorted(key for key in HOST_SESSION_ENV if key in env)


def prepare_home(workspace: str | None, role_home: str | None,
                 instance_dir: str | None) -> tuple[Path, Path]:
    """创建并返回 (home, tmp)。home 在 cwd/workspace 的 .anvil/home 下。"""
    base = Path(workspace or role_home or config.DATA_DIR)
    home = base / config.SANDBOX_HOME_DIRNAME
    tmp = Path(instance_dir or (home / "tmp")) / "tmp"
    for path in (home, home / ".claude", home / ".cache",
                 home / ".config", home / ".local" / "share", tmp):
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return home, tmp


def apply_standard_env(env: dict[str, str], *, home: Path, tmp: Path) -> dict[str, str]:
    """在已清洗的环境上覆盖 HOME/TMP/缓存；ANVIL_* 运行变量保持不变。"""
    out = dict(env)
    # 先剥掉宿主会话标记；再兜底剥掉仍指向宿主路径的 CLAUDE_CODE_*（模型凭证除外）。
    for key in HOST_SESSION_ENV:
        out.pop(key, None)
    for key in list(out):
        if (key.startswith("CLAUDE_CODE_") and key not in KEEP_CLAUDE_CODE
                and isinstance(out.get(key), str)
                and str(out[key]).startswith(HOST_PATH_PREFIXES)):
            out.pop(key, None)
    home_s = str(home)
    tmp_s = str(tmp)
    out.update({
        "HOME": home_s,
        "USERPROFILE": home_s,
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        # Claude Code 原生 CLI 的项目临时目录默认固定为 /private/tmp/claude-<uid>，
        # 不读 TMPDIR；这个变量是实测可用的覆盖方式（见 v1.62.1 兼容性矩阵）。
        "CLAUDE_CODE_TMPDIR": tmp_s,
        "XDG_RUNTIME_DIR": str(home / ".cache"),
        "TMPDIR": tmp_s,
        "TMP": tmp_s,
        "TEMP": tmp_s,
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "npm_config_cache": str(home / ".npm"),
        "PIP_CACHE_DIR": str(home / ".pip"),
        "GOCACHE": str(home / ".cache" / "go-build"),
        "CARGO_HOME": str(home / ".cargo"),
    })
    drive, tail = os.path.splitdrive(home_s)
    if drive:
        out["HOMEDRIVE"] = drive
        out["HOMEPATH"] = tail
    return out
