"""LangGraph run-lifecycle state + event schema（编排控制面）。

只承载「运行生命周期 8 态」的状态；任务 5 态仍在 `services.tasks`。CLI Agent
作为子进程执行（design §1.2「Agent 是驾驶员」），图节点不阻塞子进程生命周期。
"""
from __future__ import annotations

from typing import Any, TypedDict


class RunState(TypedDict, total=False):
    event: str                 # wake | submit | process_exit（新调用的入口路由依据）
    payload: dict[str, Any]    # 事件附加数据，如 {"exit_code": 1}
    review_decision: Any       # interrupt() 的 resume 值：{"approve": bool}
