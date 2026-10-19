"""检查点耐久：换全新 graph/saver（同一 graph-db 文件）仍能从 interrupt 恢复。"""
from __future__ import annotations

from anvil.graph import build_graph, get_checkpointer
from anvil.graph.orchestrator import Orchestrator

from tests.graph.conftest import make_env, open_run, seed_task_run


def test_resume_across_fresh_graph(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    run = seed_task_run(env, "T-CK")
    env["orch"].pump(env["db_path"], run["id"], "wake", {})
    env["orch"].pump(env["db_path"], run["id"], "submit", {})  # 挂起 interrupt
    assert open_run(env, run["id"])["status"] == "reviewing"

    # 模拟重启：同一 graph-db 文件上构建全新的 saver + graph + orchestrator。
    checkpointer2 = get_checkpointer(env["graph_db_path"])
    graph2 = build_graph(checkpointer2)
    orch2 = Orchestrator(graph2, checkpointer2)

    assert orch2.resume_review(env["db_path"], run["id"], True)
    assert open_run(env, run["id"])["status"] == "completed"
