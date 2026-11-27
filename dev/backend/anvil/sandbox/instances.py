"""沙箱实例目录布局。

实例目录存放 profile、tmp 与诊断日志；HOME 本身在工作区的 ``.anvil/home``。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .. import config


def safe_slug(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "shared"))[:80] or "shared"


def instance_root(role_slug: str | None, instance_id: str) -> Path:
    return config.SANDBOX_INSTANCES_DIR / safe_slug(role_slug) / safe_slug(instance_id)


def prepare(role_slug: str | None, instance_id: str) -> Path:
    root = instance_root(role_slug, instance_id)
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def cleanup(role_slug: str | None, instance_id: str) -> None:
    shutil.rmtree(instance_root(role_slug, instance_id), ignore_errors=True)
