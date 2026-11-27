"""沙箱内 HOME/TMP/缓存的重定向。

目标：agent 的会话、CLI 配置与缓存只落在任务工作区（``.anvil/home``）与实例
临时目录，不写宿主 ``~/.claude`` / ``~/.config``。模型凭证仍走继承的环境变量。
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import config


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
    home_s = str(home)
    tmp_s = str(tmp)
    out.update({
        "HOME": home_s,
        "USERPROFILE": home_s,
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
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
