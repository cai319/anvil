"""图入口路由：按 event 决定新调用从哪个节点进入；review 决策决定驳回后回 project。"""
from __future__ import annotations

from langgraph.graph import END


def route(state: dict) -> str:
    ev = state.get("event") or "wake"
    if ev == "submit":
        return "submit_node"
    if ev == "process_exit":
        return "process_exit_node"
    if ev == "resume":
        return "resume_node"
    return "project"


def review_route(state: dict) -> str:
    decision = state.get("review_decision") or {}
    approve = decision.get("approve", False) if isinstance(decision, dict) else bool(decision)
    return END if approve else "project"
