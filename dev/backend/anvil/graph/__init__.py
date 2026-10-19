"""LangGraph 编排控制面：只拥有运行 8 态生命周期（design §6.2）。

CLI Agent 仍作为子进程执行（design §1.2「Agent 是驾驶员」）；LangGraph 提供持久化
状态机（SqliteSaver 检查点）+ 人机审批（interrupt/Command）。任务 5 态状态机留在
`services.tasks`，图节点只 `UPDATE runs`。
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .nodes import (
    await_review_node,
    process_exit_node,
    project_node,
    resume_node,
    review_node,
    spawn_node,
    submit_node,
)
from .orchestrator import Orchestrator
from .router import review_route, route
from .state import RunState

__all__ = ["RunState", "Orchestrator", "build_graph", "get_checkpointer"]


def build_graph(checkpointer) -> StateGraph:
    g = StateGraph(RunState)
    g.add_node("project", project_node)
    g.add_node("spawn", spawn_node)
    g.add_node("submit_node", submit_node)
    g.add_node("await_review_node", await_review_node)
    g.add_node("review_node", review_node)
    g.add_node("process_exit_node", process_exit_node)
    g.add_node("resume_node", resume_node)

    g.add_conditional_edges(START, route, ["project", "submit_node", "process_exit_node", "resume_node"])
    g.add_edge("project", "spawn")
    g.add_edge("spawn", END)
    g.add_edge("submit_node", "await_review_node")
    g.add_edge("await_review_node", "review_node")
    g.add_conditional_edges("review_node", review_route, ["project", END])
    g.add_edge("process_exit_node", END)
    g.add_edge("resume_node", "project")  # resume → 重新投影（带最新会话上下文）→ 重新 spawn
    return g.compile(checkpointer=checkpointer)


def get_checkpointer(path: str) -> SqliteSaver:
    """独立 `*-graph.db` 存检查点（与 anvil.db 隔离，避免两套 migration 打架）。

    直接持有一个 `check_same_thread=False` 的连接（`from_conn_string` 是 contextmanager，
    不适合长驻 app）。
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver
