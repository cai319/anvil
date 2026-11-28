"""沙箱错误类型（API 映射用）。"""
from __future__ import annotations


class SandboxUnavailable(RuntimeError):
    """所需隔离基质不可用；严格档作业不应排队。"""


class QuotaExceeded(RuntimeError):
    """任务/角色 PoC 作业并发或排队配额已满。"""
