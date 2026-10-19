"""Orchestrator：把外部事件 pump 进运行生命周期图，每 run 一把锁串行化并发。

事件驱动而非长驻图：`pump` 每次用同一 thread_id（= run_id）invoke，图从检查点续跑、
迁移状态后即返回；子进程在外部运行。`resume_review` 用 `Command(resume=...)` 恢复
`await_review_node` 里的 interrupt。
"""
from __future__ import annotations

import threading
from typing import Any

from langgraph.types import Command


class Orchestrator:
    def __init__(self, graph, checkpointer) -> None:
        self.graph = graph
        self.checkpointer = checkpointer
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _config(self, db_path: str, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id, "db_path": db_path}}

    def pump(self, db_path: str, run_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock(run_id):
            self.graph.invoke({"event": event, "payload": payload or {}}, self._config(db_path, run_id))

    def resume_review(self, db_path: str, run_id: str, approve: bool) -> bool:
        """恢复人审 interrupt。无挂起 interrupt（如 submit pump 未发生）时安全返回 False。"""
        with self._lock(run_id):
            cfg = self._config(db_path, run_id)
            try:
                if not self.graph.get_state(cfg).next:
                    return False  # 无待恢复的 interrupt
                self.graph.invoke(Command(resume={"approve": approve}), cfg)
                return True
            except Exception:  # noqa: BLE001 —— 图异常不向上抛，靠 reconcile 自愈
                return False

    def get_state(self, db_path: str, run_id: str):
        return self.graph.get_state(self._config(db_path, run_id))
